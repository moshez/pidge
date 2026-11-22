resource "aws_s3_bucket" "mail" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_versioning" "mail" {
  bucket = aws_s3_bucket.mail.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "mail" {
  bucket                  = aws_s3_bucket.mail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

locals {
  # Prefixes whose current objects expire, and after how many days.
  # msgs/ and index/ are never expired.
  expiring_prefixes = {
    "inbound/"          = 30 # SES's raw drop; msgs/ has the canonical copy
    "outbox/"           = 30 # audit trail once sent
    "inbound-seen/"     = 35 # ingest claim markers (hold the ULID for resume)
    "inbound-done/"     = 35 # ingest completion markers
    "inbound-failures/" = 35 # attempt counters
    "ops-sent/"         = 7  # once-a-day dedupe markers for ops mail
    "aws/"              = 30 # Lambda's async failure records, once replayed
    "failed-events/"    = 90 # parked records, kept for a look
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "mail" {
  bucket = aws_s3_bucket.mail.id

  dynamic "rule" {
    for_each = local.expiring_prefixes
    content {
      id     = "expire-${trimsuffix(rule.key, "/")}"
      status = "Enabled"
      filter {
        prefix = rule.key
      }
      expiration {
        days = rule.value
      }
    }
  }

  # Every CAS write and shard rewrite leaves a noncurrent version.
  rule {
    id     = "noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    expiration {
      expired_object_delete_marker = true
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.mail]
}

data "aws_iam_policy_document" "mail_bucket" {
  statement {
    sid       = "SESWritesInbound"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.mail.arn}/inbound/*"]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = ["arn:aws:ses:${var.region}:${local.account_id}:receipt-rule-set/${local.rule_set}:receipt-rule/${local.rule_name}"]
    }
  }
}

resource "aws_s3_bucket_policy" "mail" {
  bucket = aws_s3_bucket.mail.id
  policy = data.aws_iam_policy_document.mail_bucket.json
}

resource "aws_s3_bucket_notification" "mail" {
  bucket = aws_s3_bucket.mail.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.pidge.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "inbound/"
  }

  # Only message.eml: CAS writes to state.json must not re-trigger activation.
  lambda_function {
    lambda_function_arn = aws_lambda_function.pidge.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "outbox/"
    filter_suffix       = "/message.eml"
  }

  depends_on = [aws_lambda_permission.s3]
}
