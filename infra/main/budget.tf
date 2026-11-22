# Cost guardrail. The account has no hard spend cap, so:
#
#   50% actual, 100% forecast  -> email
#   100% actual                -> email, and the kill switch (SNS -> this
#                                 function): SES receiving off, the function's
#                                 reserved concurrency set to 0
#   200% actual                -> budget action attaches deny-all to the
#                                 function's role and the deploy role
#
# The budget action attaches its policy out of band, so neither target role's
# attachments may be managed exclusively here (no managed_policy_arns, no
# aws_iam_role_policy_attachments_exclusive), or the next apply would detach
# the deny. Once the deny is on the deploy role, CI cannot apply; that is
# intended. RECOVERY.md undoes each step.

data "aws_iam_role" "deploy" {
  name = var.deploy_role_name
}

resource "aws_budgets_budget" "monthly" {
  name         = "pidge-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.owner_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.owner_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.owner_email]
    subscriber_sns_topic_arns  = [aws_sns_topic.killswitch.arn]
  }

  lifecycle {
    precondition {
      condition     = !endswith(lower(var.owner_email), "@${lower(var.mail_domain)}")
      error_message = "owner_email must be outside ${var.mail_domain}: alerts must not land in the mailbox they are about."
    }
  }

  depends_on = [aws_sns_topic_policy.killswitch]
}

# -- Kill switch (100% actual) -------------------------------------------------

resource "aws_sns_topic" "killswitch" {
  name = "budget-killswitch"
}

data "aws_iam_policy_document" "killswitch_topic" {
  statement {
    sid       = "BudgetsPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.killswitch.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${local.account_id}:*"]
    }
  }
}

resource "aws_sns_topic_policy" "killswitch" {
  arn    = aws_sns_topic.killswitch.arn
  policy = data.aws_iam_policy_document.killswitch_topic.json
}

resource "aws_sns_topic_subscription" "killswitch" {
  topic_arn = aws_sns_topic.killswitch.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.pidge.arn
}

resource "aws_lambda_permission" "killswitch" {
  statement_id  = "BudgetKillSwitch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pidge.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.killswitch.arn
}

# -- Backstop (200% actual): deny everything ------------------------------------

resource "aws_iam_policy" "deny_all" {
  name        = "budget-deny-all"
  description = "Attached by the pidge budget action at 200% of budget. See RECOVERY.md."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "BudgetExceeded"
      Effect   = "Deny"
      Action   = "*"
      Resource = "*"
    }]
  })
}

data "aws_iam_policy_document" "budget_action_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${local.account_id}:budget/*"]
    }
  }
}

resource "aws_iam_role" "budget_action" {
  name               = "pidge-budget-action"
  assume_role_policy = data.aws_iam_policy_document.budget_action_trust.json
}

data "aws_iam_policy_document" "budget_action" {
  statement {
    sid       = "AttachDenyAllToTargets"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = [aws_iam_role.lambda.arn, data.aws_iam_role.deploy.arn]
    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [aws_iam_policy.deny_all.arn]
    }
  }
}

resource "aws_iam_role_policy" "budget_action" {
  name   = "attach-deny-all"
  role   = aws_iam_role.budget_action.id
  policy = data.aws_iam_policy_document.budget_action.json
}

resource "aws_budgets_budget_action" "backstop" {
  budget_name        = aws_budgets_budget.monthly.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 200
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.deny_all.arn
      roles      = [aws_iam_role.lambda.name, data.aws_iam_role.deploy.name]
    }
  }

  subscriber {
    address           = var.owner_email
    subscription_type = "EMAIL"
  }

  depends_on = [aws_iam_role_policy.budget_action]
}
