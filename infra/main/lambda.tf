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
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:pidge"]
  }
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
    }
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
