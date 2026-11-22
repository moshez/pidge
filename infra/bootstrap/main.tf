# The bootstrap stack: applied once, by hand, with admin credentials.
#
# It creates what the deploy workflow needs before it can run: the state
# bucket, GitHub's OIDC provider, the deploy role, and the image repository.
# Applied by infra/setup.py (see docs/SETUP.md).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "The AWS region for everything (SES receiving must be supported there)."
  type        = string
  default     = "us-west-2"
}

variable "state_bucket" {
  description = "Name of the S3 bucket that holds the main stack's state (globally unique)."
  type        = string
}

variable "ecr_repository" {
  description = "Name of the ECR repository for the Lambda image."
  type        = string
  default     = "pidge"
}

variable "github_owner" {
  description = "GitHub owner (user or organization) of the repository."
  type        = string
  default     = "moshez"
}

variable "github_owner_id" {
  description = "Numeric ID of the owner: gh api users/OWNER --jq .id"
  type        = number
  default     = 422703
}

variable "github_repository" {
  description = "GitHub repository name."
  type        = string
  default     = "pidge"
}

variable "github_repository_id" {
  description = "Numeric ID of the repository: gh api repos/OWNER/REPO --jq .id"
  type        = number
  default     = 1467341512
}

variable "deploy_branch" {
  description = "The only branch allowed to deploy."
  type        = string
  default     = "trunk"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  # GitHub's sub claim for new repositories names the owner and repository
  # by immutable ID as well as by name, so a renamed or re-created repository
  # with the same name cannot assume this role.
  github_subject = format(
    "repo:%s@%d/%s@%d:ref:refs/heads/%s",
    var.github_owner,
    var.github_owner_id,
    var.github_repository,
    var.github_repository_id,
    var.deploy_branch,
  )
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_subject]
    }
  }
}

# The main stack creates IAM roles, so this role is effectively an
# administrator of the account. Only pushes to the deploy branch can use it.
resource "aws_iam_role" "deploy" {
  name               = "pidge-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_trust.json
}

resource "aws_iam_role_policy_attachment" "deploy_admin" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_ecr_repository" "lambda" {
  name                 = var.ecr_repository
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "ecr_lambda" {
  statement {
    sid     = "LambdaPull"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:*"]
    }
  }
}

resource "aws_ecr_repository_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name
  policy     = data.aws_iam_policy_document.ecr_lambda.json
}

output "state_bucket" {
  description = "Set as the GitHub variable TF_STATE_BUCKET."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Set as the GitHub variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}

output "ecr_repository_url" {
  description = "Where the workflow pushes images."
  value       = aws_ecr_repository.lambda.repository_url
}

output "github_subject" {
  description = "The OIDC subject the deploy role trusts."
  value       = local.github_subject
}
