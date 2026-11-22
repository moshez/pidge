resource "aws_cognito_user_pool" "pidge" {
  name                = "pidge"
  user_pool_tier      = "ESSENTIALS"
  username_attributes = ["email"]
  deletion_protection = "ACTIVE"

  # The API requires PASSWORD in the list. The only user has a random
  # password nobody knows, so in practice sign-in is by emailed code.
  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD", "EMAIL_OTP"]
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 32
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Email OTP needs Cognito to send through your own SES identity.
  email_configuration {
    email_sending_account = "DEVELOPER"
    source_arn            = aws_ses_domain_identity.mail.arn
    from_email_address    = "Pidge sign-in <sign-in@${var.mail_domain}>"
  }

  depends_on = [
    aws_ses_domain_identity_verification.mail,
    aws_ses_identity_policy.cognito,
  ]
}

# Managed login (version 2) is required for email OTP and for dynamic
# client registration. The prefix domain needs no certificate or DNS.
resource "aws_cognito_user_pool_domain" "pidge" {
  domain                = var.cognito_domain_prefix
  user_pool_id          = aws_cognito_user_pool.pidge.id
  managed_login_version = 2
}

# MCP clients ask for tokens for the server's URL; the resource server's
# identifier must match it exactly.
resource "aws_cognito_resource_server" "mcp" {
  user_pool_id = aws_cognito_user_pool.pidge.id
  identifier   = "${trimsuffix(aws_lambda_function_url.pidge.function_url, "/")}/mcp"
  name         = "pidge-mcp"

  scope {
    scope_name        = "full"
    scope_description = "Read, send and tag mail"
  }
}

resource "random_password" "owner" {
  length      = 48
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
  special     = false
}

# A permanent random password makes the user CONFIRMED without a
# welcome email; nobody ever types it.
resource "aws_cognito_user" "owner" {
  user_pool_id   = aws_cognito_user_pool.pidge.id
  username       = var.owner_email
  password       = random_password.owner.result
  message_action = "SUPPRESS"

  attributes = {
    email          = var.owner_email
    email_verified = "true"
  }
}

# The AWS provider has no setting for dynamic client registration yet, so
# a script turns it on (open registration, authorization code only, the
# MCP scope allowed). It re-runs when the pool or the scope changes.
resource "terraform_data" "dcr" {
  triggers_replace = [
    aws_cognito_user_pool.pidge.id,
    aws_cognito_resource_server.mcp.scope_identifiers,
  ]

  provisioner "local-exec" {
    command = "python3 ${path.module}/../scripts/enable_dcr.py"
    environment = {
      AWS_REGION   = var.region
      USER_POOL_ID = aws_cognito_user_pool.pidge.id
      DCR_SCOPES   = join(" ", aws_cognito_resource_server.mcp.scope_identifiers)
    }
  }

  depends_on = [aws_cognito_user_pool_domain.pidge]
}
