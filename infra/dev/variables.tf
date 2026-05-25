variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment label (dev / staging / prod)."
  type        = string
  default     = "dev"
}

variable "dynamo_table_name" {
  description = "Name of the DynamoDB table used for agent memory and checkpoints."
  type        = string
  default     = "pr-review-memory"
}

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model ID (or cross-region inference profile ARN) used by the agents.
    Must be enabled in the Bedrock console under Model access before use.
    Common values:
      us.anthropic.claude-haiku-4-5-20251001-v1:0        (default — Claude Haiku 4.5 cross-region)
      anthropic.claude-sonnet-4-5-20250514-v1:0          (Claude Sonnet 4.5 — higher quality)
      us.anthropic.claude-haiku-4-5-20251001-v1:0        (cross-region inference profile)
  EOT
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "aws_profile" {
  description = "AWS CLI profile name used in the env_block output (e.g. your SSO profile). Only included when create_iam_user = false."
  type        = string
  default     = "default"
}

variable "create_iam_user" {
  description = <<-EOT
    Set to true to create a dedicated IAM user + access key (CI / no-SSO setups).
    Default false — assumes SSO or an existing role with the generated policy attached.
  EOT
  type        = bool
  default     = false
}

variable "iam_user_name" {
  description = "Name of the IAM user (only used when create_iam_user = true)."
  type        = string
  default     = "snarky-squirrel-local-dev"
}

# ── EC2 variables ─────────────────────────────────────────────────────────────

variable "create_ec2" {
  description = "Create an EC2 instance to run the PR Reviewer API server."
  type        = bool
  default     = false
}

variable "ec2_instance_type" {
  description = "EC2 instance type for the API server. t3.micro is sufficient for personal/dev use."
  type        = string
  default     = "t3.micro"
}

variable "ec2_key_name" {
  description = "Name of an existing EC2 key pair. Leave empty (default) to use SSM Session Manager only — no SSH key needed."
  type        = string
  default     = ""
}

variable "github_token" {
  description = "GitHub PAT for fetching PR metadata. Written to .env on the EC2 instance. Mark sensitive."
  type        = string
  sensitive   = true
  default     = ""
}

# ── Route53 variables ──────────────────────────────────────────────────────────

variable "route53_zone_name" {
  description = "Apex domain of an existing Route53 public hosted zone (e.g. hemantkumar.dev). Leave empty to skip DNS record creation."
  type        = string
  default     = ""

  validation {
    condition     = var.route53_zone_name == "" || can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.route53_zone_name))
    error_message = "route53_zone_name must be a valid domain name (e.g. hemantkumar.dev) or empty to skip DNS."
  }
}

variable "subdomain_name" {
  description = "Subdomain to point at the EC2 instance (e.g. 'snarky' → snarky.hemantkumar.dev)."
  type        = string
  default     = "snarky"
}

variable "certbot_email" {
  description = "Email address for Let's Encrypt certificate notifications. Required when route53_zone_name is set."
  type        = string
  default     = ""
}

# ── Admin variables ───────────────────────────────────────────────────────────

variable "admin_emails" {
  description = "Comma-separated list of email addresses with admin access to the UI (delete reviews, manage Cognito users)."
  type        = string
  default     = ""
}

# ── Cognito variables ──────────────────────────────────────────────────────────

variable "enable_cognito" {
  description = "Create a Cognito User Pool and protect the app with OAuth2 authentication."
  type        = bool
  default     = false
}

variable "app_url" {
  description = "Public base URL of the app (e.g. https://snarky.hemantkumar.dev). Used as the Cognito OAuth2 callback base URL."
  type        = string
  default     = ""
}
