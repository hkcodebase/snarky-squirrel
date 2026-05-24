# ═══════════════════════════════════════════════════════════════════════════════
# Cognito — User Pool + Hosted UI for PR Reviewer authentication
# ═══════════════════════════════════════════════════════════════════════════════
#
# Creates (when enable_cognito = true):
#   • Cognito User Pool  — email/password sign-in
#   • User Pool Domain   — Hosted UI at <prefix>.auth.<region>.amazoncognito.com
#   • App Client         — Authorization Code flow, 8-hour token validity
#   • SSM SecureString   — client secret, retrieved by EC2 at runtime
#
# Usage (terraform.tfvars):
#   enable_cognito = true
#   app_url        = "https://snarky.hemantkumar.dev"
# ═══════════════════════════════════════════════════════════════════════════════

# ── User Pool ─────────────────────────────────────────────────────────────────

resource "aws_cognito_user_pool" "pr_reviewer" {
  count = var.enable_cognito ? 1 : 0
  name  = "pr-reviewer-${var.environment}"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 8
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # Disable self-registration — only admins can create users via the AWS console
  # or CLI: aws cognito-idp admin-create-user ...
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = { Environment = var.environment, ManagedBy = "terraform" }
}

# ── Hosted UI Domain ──────────────────────────────────────────────────────────
# Domain prefix must be globally unique — we include the account ID for that.

resource "aws_cognito_user_pool_domain" "pr_reviewer" {
  count        = var.enable_cognito ? 1 : 0
  domain       = "pr-reviewer-${var.environment}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.pr_reviewer[0].id
}

# ── App Client ────────────────────────────────────────────────────────────────

resource "aws_cognito_user_pool_client" "pr_reviewer" {
  count        = var.enable_cognito ? 1 : 0
  name         = "pr-reviewer-web"
  user_pool_id = aws_cognito_user_pool.pr_reviewer[0].id

  generate_secret                      = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  supported_identity_providers         = ["COGNITO"]
  prevent_user_existence_errors        = "ENABLED"

  # Callback/logout URLs — include both the real domain and localhost for dev.
  callback_urls = compact([
    var.app_url != "" ? "${var.app_url}/auth/callback" : "",
    "http://localhost:8080/auth/callback",
  ])
  # logout_uri must be the root page, not /auth/logout.
  # Pointing at /auth/logout causes an infinite redirect loop (Cognito redirects
  # there after sign-out, which immediately redirects back to Cognito again).
  logout_urls = compact([
    var.app_url != "" ? "${var.app_url}/" : "",
    "http://localhost:8080/",
  ])

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
  access_token_validity  = 8
  id_token_validity      = 8
  refresh_token_validity = 30
}

# ── SSM SecureString — client secret ──────────────────────────────────────────
# Never embed the client secret in user_data; retrieve from SSM at runtime.

resource "aws_ssm_parameter" "cognito_client_secret" {
  count       = var.enable_cognito ? 1 : 0
  name        = "/pr-reviewer/${var.environment}/cognito-client-secret"
  description = "Cognito app client secret for PR Reviewer (retrieved at EC2 startup)"
  type        = "SecureString"
  value       = aws_cognito_user_pool_client.pr_reviewer[0].client_secret

  tags = { Environment = var.environment, ManagedBy = "terraform" }
}
