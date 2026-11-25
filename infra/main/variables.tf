variable "region" {
  description = "AWS region. SES must support receiving there; the backend block assumes us-east-2."
  type        = string
  default     = "us-east-2"
}

variable "mail_domain" {
  description = "The domain that receives and sends mail. Its DNS zone must be on Cloudflare."
  type        = string
}

variable "owner_email" {
  description = "Your own address: the only Cognito user, an SES-verified recipient while in the sandbox, and where budget alerts go. Must be outside the mail domain. Passed from a GitHub secret so it never lands in the repository or its logs."
  type        = string
  # Personal data in a public repository's Actions logs: keep it out of plans.
  sensitive = true
}

variable "image_uri" {
  description = "The ECR repository URL (without tag or digest)."
  type        = string
}

variable "image_digest" {
  description = "The image digest to deploy (sha256:...). Pinning by digest updates the function on every new image."
  type        = string
}

variable "cognito_domain_prefix" {
  description = "Prefix for the managed-login domain <prefix>.auth.<region>.amazoncognito.com (globally unique)."
  type        = string
}

variable "bucket_name" {
  description = "Mail bucket name; defaults to pidge-mail-<account id>."
  type        = string
  default     = null
}

variable "mailbox_local_part" {
  description = "Local part of the From address for outgoing mail."
  type        = string
  default     = "mail"
}

variable "mailbox_name" {
  description = "Display name of the From address."
  type        = string
  default     = "Pidge"
}

variable "message_id_domain" {
  description = "The domain SES puts in the Message-ID of mail it sends from this region. infra/setup.py checks it after the first deploy."
  type        = string
  default     = "us-east-2.amazonses.com"
}

variable "enable_mail_from" {
  description = "Use bounce.<domain> as the custom MAIL FROM, so SPF aligns for DMARC."
  type        = bool
  default     = true
}

variable "ephemeral_storage_mb" {
  description = "Lambda /tmp size, for the cached index shards."
  type        = number
  default     = 2048
}

variable "budget_limit_usd" {
  description = "Monthly cost budget in USD. Expected spend is about $0.30 a month."
  type        = string
  default     = "5"
}

variable "deploy_role_name" {
  description = "The GitHub OIDC deploy role created by infra/bootstrap; the 200% budget action denies it everything."
  type        = string
  default     = "pidge-deploy"
}
