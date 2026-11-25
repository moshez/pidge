data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "pidge-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "MailObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.mail.arn}/*"]
  }
  statement {
    sid       = "MailListing"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.mail.arn]
  }
  statement {
    sid       = "Send"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["*"]
  }
  statement {
    sid       = "TriggerIndexer"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn]
  }
  # Kill switch (budget.tf). SetActiveReceiptRuleSet has no resource type in
  # IAM, so it cannot be scoped to the rule set; this region is the limit.
  statement {
    sid       = "KillSwitchStopReceiving"
    actions   = ["ses:SetActiveReceiptRuleSet"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
  # clients.py: let Claude's dynamically registered clients offer email codes.
  statement {
    sid = "AllowEmailCodes"
    actions = [
      "cognito-idp:ListUserPoolClients",
      "cognito-idp:DescribeUserPoolClient",
      "cognito-idp:UpdateUserPoolClient",
    ]
    resources = [aws_cognito_user_pool.pidge.arn]
  }
  # sessions.py: the key that signs get_session_token's bearer tokens.
  statement {
    sid       = "SessionKey"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.session_key.arn]
  }
  statement {
    sid       = "KillSwitchStopFunction"
    actions   = ["lambda:PutFunctionConcurrency"]
    resources = [local.function_arn]
  }
}

# Signs the 5-minute bearer tokens that get_session_token hands to scripts
# (sessions.py). Encrypted with the account's aws/ssm key, which the Lambda
# role may use through SSM without a KMS grant of its own.
resource "random_password" "session_key" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "session_key" {
  name  = "/pidge/session-signing-key"
  type  = "SecureString"
  value = random_password.session_key.result
}

resource "aws_iam_role_policy" "lambda" {
  name   = "pidge"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_lambda_function" "pidge" {
  function_name = "pidge"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${var.image_uri}@${var.image_digest}"
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 300

  ephemeral_storage {
    size = var.ephemeral_storage_mb
  }

  environment {
    variables = {
      BUCKET            = aws_s3_bucket.mail.bucket
      MAIL_DOMAIN       = var.mail_domain
      FROM_ADDRESS      = local.from_address
      OWNER_ADDRESS     = var.owner_email
      CONFIGURATION_SET = aws_sesv2_configuration_set.pidge.configuration_set_name
      MESSAGE_ID_DOMAIN = var.message_id_domain
      ISSUER            = "https://${aws_cognito_user_pool.pidge.endpoint}"
      USER_POOL_ID      = aws_cognito_user_pool.pidge.id
      # sessions.py: SSM SecureString with the session-token signing key.
      SESSION_KEY_PARAMETER = aws_ssm_parameter.session_key.name
      # mcp.py: logged on every MCP initialize.
      PIDGE_BUILD = var.build
      # budget.tf: SNS records from this topic trip the kill switch.
      KILL_SWITCH_TOPIC_ARN = aws_sns_topic.killswitch.arn
    }
  }

  # The kill switch sets reserved concurrency to 0 out of band; an apply must
  # not quietly undo it (RECOVERY.md removes it deliberately).
  lifecycle {
    ignore_changes = [reserved_concurrent_executions]
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
    aws_cloudwatch_log_group.pidge,
  ]
}

resource "aws_cloudwatch_log_group" "pidge" {
  name              = "/aws/lambda/pidge"
  retention_in_days = 30
}

# Events that fail every async retry land in the bucket; the reconciler
# replays them, then parks them under failed-events/ and sends ops mail.
resource "aws_lambda_function_event_invoke_config" "pidge" {
  function_name          = aws_lambda_function.pidge.function_name
  maximum_retry_attempts = 2

  destination_config {
    on_failure {
      destination = aws_s3_bucket.mail.arn
    }
  }
}

resource "aws_lambda_function_url" "pidge" {
  function_name      = aws_lambda_function.pidge.function_name
  authorization_type = "NONE" # the function checks OAuth bearer tokens itself
}

resource "aws_lambda_permission" "url" {
  statement_id           = "FunctionURL"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.pidge.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "FunctionURLInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.pidge.function_name
  principal                = "*"
  invoked_via_function_url = true
}

resource "aws_lambda_permission" "s3" {
  statement_id   = "S3Notifications"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.pidge.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.mail.arn
  source_account = local.account_id
}

resource "aws_lambda_permission" "events" {
  for_each      = aws_cloudwatch_event_rule.pidge
  statement_id  = "EventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pidge.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}
