terraform {
  required_version = ">= 1.10"

  # The bucket comes from the bootstrap stack; pass it with
  #   tofu init -backend-config="bucket=<state bucket>"
  backend "s3" {
    key          = "pidge/main.tfstate"
    region       = "us-west-2"
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
}

# Reads CLOUDFLARE_API_TOKEN from the environment.
provider "cloudflare" {}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  bucket_name  = coalesce(var.bucket_name, "pidge-mail-${local.account_id}")
  from_address = "${var.mailbox_name} <${var.mailbox_local_part}@${var.mail_domain}>"
  rule_set     = "pidge"
  function_arn = "arn:aws:lambda:${var.region}:${local.account_id}:function:pidge"
  rule_name    = "pidge-inbound"
}
