resource "aws_ses_domain_identity" "mail" {
  domain = var.mail_domain
}

resource "aws_ses_domain_dkim" "mail" {
  domain = aws_ses_domain_identity.mail.domain
}

# Waits until SES sees the verification record (minutes, sometimes longer).
# Cognito's email configuration needs a verified identity.
resource "aws_ses_domain_identity_verification" "mail" {
  domain     = aws_ses_domain_identity.mail.id
  depends_on = [cloudflare_dns_record.ses_verification]

  timeouts {
    create = "60m"
  }
}

resource "aws_ses_domain_mail_from" "mail" {
  count                  = var.enable_mail_from ? 1 : 0
  domain                 = aws_ses_domain_identity.mail.domain
  mail_from_domain       = "bounce.${var.mail_domain}"
  behavior_on_mx_failure = "UseDefaultValue"
}

# While the account is in the SES sandbox, mail can only go to verified
# addresses. SES emails a link to this address; click it (docs/SETUP.md).
resource "aws_ses_email_identity" "owner" {
  email = var.owner_email
}

# Cognito sends sign-in codes through this identity.
data "aws_iam_policy_document" "cognito_sends" {
  statement {
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = [aws_ses_domain_identity.mail.arn]
    principals {
      type        = "Service"
      identifiers = ["cognito-idp.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_ses_identity_policy" "cognito" {
  identity = aws_ses_domain_identity.mail.arn
  name     = "cognito-sign-in-codes"
  policy   = data.aws_iam_policy_document.cognito_sends.json
}

resource "aws_ses_receipt_rule_set" "pidge" {
  rule_set_name = local.rule_set
}

# Only one rule set can be active per region; this replaces any other.
resource "aws_ses_active_receipt_rule_set" "pidge" {
  rule_set_name = aws_ses_receipt_rule_set.pidge.rule_set_name
}

resource "aws_ses_receipt_rule" "inbound" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.pidge.rule_set_name
  recipients    = [var.mail_domain]
  enabled       = true
  scan_enabled  = true
  tls_policy    = "Optional"

  s3_action {
    bucket_name       = aws_s3_bucket.mail.bucket
    object_key_prefix = "inbound/"
    position          = 1
  }

  depends_on = [aws_s3_bucket_policy.mail]
}

resource "aws_sesv2_configuration_set" "pidge" {
  configuration_set_name = "pidge"
}

resource "aws_sesv2_configuration_set_event_destination" "events" {
  configuration_set_name = aws_sesv2_configuration_set.pidge.configuration_set_name
  event_destination_name = "eventbridge"

  event_destination {
    enabled              = true
    matching_event_types = ["SEND", "DELIVERY", "BOUNCE", "REJECT"]
    event_bridge_destination {
      event_bus_arn = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default"
    }
  }
}
