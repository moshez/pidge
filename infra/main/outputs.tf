output "mcp_url" {
  description = "Add this URL to your MCP client."
  value       = "${trimsuffix(aws_lambda_function_url.pidge.function_url, "/")}/mcp"
}

output "issuer" {
  description = "The Cognito issuer (the OAuth authorization server)."
  value       = "https://${aws_cognito_user_pool.pidge.endpoint}"
}

output "sign_in_domain" {
  description = "The managed-login domain."
  value       = "https://${aws_cognito_user_pool_domain.pidge.domain}.auth.${var.region}.amazoncognito.com"
}

output "user_pool_id" {
  value = aws_cognito_user_pool.pidge.id
}

output "mail_bucket" {
  value = aws_s3_bucket.mail.bucket
}

output "from_address" {
  value = local.from_address
}
