output "dynamodb_table_name" {
  description = "Name of the DynamoDB table — set as DYNAMODB_TABLE in .env."
  value       = aws_dynamodb_table.pr_review_memory.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table."
  value       = aws_dynamodb_table.pr_review_memory.arn
}

output "aws_region" {
  description = "AWS region — set as AWS_REGION in .env."
  value       = var.aws_region
}

output "bedrock_model_id" {
  description = "Bedrock model ID — set as BEDROCK_MODEL_ID in .env."
  value       = var.bedrock_model_id
}

output "iam_policy_arn" {
  description = "ARN of the IAM policy. Attach to any existing role/user if create_iam_user = false."
  value       = aws_iam_policy.pr_reviewer.arn
}

# ── Credentials (only when create_iam_user = true) ───────────────────────────

output "aws_access_key_id" {
  description = "Access key ID for the local-dev IAM user — set as AWS_ACCESS_KEY_ID in .env."
  value       = var.create_iam_user ? aws_iam_access_key.pr_reviewer[0].id : "n/a (create_iam_user = false)"
  sensitive   = false
}

output "aws_secret_access_key" {
  description = "Secret access key — set as AWS_SECRET_ACCESS_KEY in .env. Shown only once."
  value       = var.create_iam_user ? aws_iam_access_key.pr_reviewer[0].secret : "n/a (create_iam_user = false)"
  sensitive   = true
}

locals {
  _env_block_sso = <<-ENV
    # ── paste into your .env ──────────────────────────────────────────
    LLM_PROVIDER=bedrock
    BEDROCK_MODEL_ID=${var.bedrock_model_id}
    AWS_REGION=${var.aws_region}
    AWS_PROFILE=${var.aws_profile}
    DYNAMODB_TABLE=${aws_dynamodb_table.pr_review_memory.name}
    DYNAMODB_ENDPOINT=
    # ─────────────────────────────────────────────────────────────────
  ENV

  _env_block_iam = <<-ENV
    # ── paste into your .env ──────────────────────────────────────────
    LLM_PROVIDER=bedrock
    BEDROCK_MODEL_ID=${var.bedrock_model_id}
    AWS_REGION=${var.aws_region}
    AWS_ACCESS_KEY_ID=${try(aws_iam_access_key.pr_reviewer[0].id, "")}
    AWS_SECRET_ACCESS_KEY=${try(aws_iam_access_key.pr_reviewer[0].secret, "")}
    DYNAMODB_TABLE=${aws_dynamodb_table.pr_review_memory.name}
    DYNAMODB_ENDPOINT=
    # ─────────────────────────────────────────────────────────────────
  ENV
}

output "env_block" {
  description = "Ready-to-paste .env block. Uses AWS_PROFILE (SSO) when create_iam_user = false, access keys otherwise."
  sensitive   = true
  value       = var.create_iam_user ? local._env_block_iam : local._env_block_sso
}

# ── EC2 outputs (only when create_ec2 = true) ────────────────────────────────

output "ec2_instance_id" {
  description = "EC2 instance ID — use with SSM Session Manager."
  value       = var.create_ec2 ? aws_instance.pr_reviewer[0].id : "n/a (create_ec2 = false)"
}

output "ec2_ssm_command" {
  description = "SSM Session Manager command — shell access without SSH key."
  value = var.create_ec2 ? (
    "aws ssm start-session --target ${aws_instance.pr_reviewer[0].id} --profile ${var.aws_profile}"
  ) : "n/a (create_ec2 = false)"
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID — set as COGNITO_USER_POOL_ID in .env."
  value       = var.enable_cognito ? aws_cognito_user_pool.pr_reviewer[0].id : "n/a (enable_cognito = false)"
}

output "cognito_client_id" {
  description = "Cognito App Client ID — set as COGNITO_CLIENT_ID in .env."
  value       = var.enable_cognito ? aws_cognito_user_pool_client.pr_reviewer[0].id : "n/a (enable_cognito = false)"
}

output "cognito_hosted_ui_domain" {
  description = "Full Cognito Hosted UI domain — set as COGNITO_DOMAIN in .env."
  value = var.enable_cognito ? (
    "${aws_cognito_user_pool_domain.pr_reviewer[0].domain}.auth.${var.aws_region}.amazoncognito.com"
  ) : "n/a (enable_cognito = false)"
}

output "cognito_create_user_cmd" {
  description = "CLI command to create a user. Self-registration is disabled — all users must be created this way."
  value = var.enable_cognito ? (
    "aws cognito-idp admin-create-user --user-pool-id ${aws_cognito_user_pool.pr_reviewer[0].id} --username you@example.com --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true --temporary-password TempPass1! --profile ${var.aws_profile}"
  ) : "n/a (enable_cognito = false)"
}

output "app_dns_url" {
  description = "Public HTTPS URL via Route53 + Let's Encrypt (available after bootstrap ~5 min)."
  value = (
    var.create_ec2 && var.route53_zone_name != ""
    ? "https://${var.subdomain_name}.${var.route53_zone_name}"
    : "n/a (set route53_zone_name to enable)"
  )
}
