locals {
  event_rules = {
    ses = {
      description = "SES sending events for the outbox"
      schedule    = null
      pattern = jsonencode({
        source        = ["aws.ses"]
        "detail-type" = ["Email Sent", "Email Delivered", "Email Bounced", "Email Rejected"]
      })
      input = null
    }
    reconcile = {
      description = "Reconciler: inbound sweep, stale outbox, failed events, ops mail"
      schedule    = "rate(15 minutes)"
      pattern     = null
      input       = jsonencode({ action = "reconcile" })
    }
    index = {
      description = "Index backstop"
      schedule    = "rate(1 day)"
      pattern     = null
      input       = jsonencode({ action = "index" })
    }
  }
}

resource "aws_cloudwatch_event_rule" "pidge" {
  for_each            = local.event_rules
  name                = "pidge-${each.key}"
  description         = each.value.description
  schedule_expression = each.value.schedule
  event_pattern       = each.value.pattern
}

resource "aws_cloudwatch_event_target" "pidge" {
  for_each = local.event_rules
  rule     = aws_cloudwatch_event_rule.pidge[each.key].name
  arn      = aws_lambda_function.pidge.arn
  input    = each.value.input
}
