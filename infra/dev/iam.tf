# ─── IAM policy ───────────────────────────────────────────────────────────────
#
# Grants exactly the permissions the application needs:
#   • DynamoDB — CRUD on the single memory table
#   • Bedrock  — InvokeModel on the configured model and any cross-region
#                inference profile that wraps it

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Strip the cross-region prefix (us./eu./ap.) to derive the base foundation-model ID.
  # e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0" → "anthropic.claude-haiku-4-5-20251001-v1:0"
  base_model_id = replace(var.bedrock_model_id, "/^(us|eu|ap)\\./", "")

  # Cross-region inference profiles route traffic across US regions; IAM evaluates
  # the destination foundation-model ARN in each region — list them explicitly
  # rather than using wildcards, to maintain least-privilege.
  bedrock_model_arns = [
    "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}",
    "arn:aws:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${var.bedrock_model_id}",
    "arn:aws:bedrock:us-east-1::foundation-model/${local.base_model_id}",
    "arn:aws:bedrock:us-east-2::foundation-model/${local.base_model_id}",
    "arn:aws:bedrock:us-west-2::foundation-model/${local.base_model_id}",
  ]
}

resource "aws_iam_policy" "pr_reviewer" {
  name        = "snarky-squirrel-${var.environment}"
  description = "Least-privilege policy for Snarky Squirrel PR reviewer (local dev + Lambda)."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ── DynamoDB ────────────────────────────────────────────────────────────
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DescribeTable",
          "dynamodb:CreateTable",          # needed by startup auto-create
          "dynamodb:ListTables",
        ]
        Resource = [
          aws_dynamodb_table.pr_review_memory.arn,
          "${aws_dynamodb_table.pr_review_memory.arn}/index/*",
        ]
      },

      # ── Bedrock ─────────────────────────────────────────────────────────────
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = local.bedrock_model_arns
      },

      # ── Bedrock model discovery (used by health-check / shadow eval) ────────
      {
        Sid      = "BedrockListModels"
        Effect   = "Allow"
        Action   = ["bedrock:ListFoundationModels"]
        Resource = ["*"]
      },

      # ── SSM — read secrets (GitHub token + Cognito client secret) ───────────
      {
        Sid    = "SSMReadSecrets"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter/pr-reviewer/${var.environment}/*"
        ]
      },
    ]
  })
}

# ─── Optional dedicated IAM user for local dev ───────────────────────────────

resource "aws_iam_user" "pr_reviewer" {
  count = var.create_iam_user ? 1 : 0
  name  = var.iam_user_name
}

resource "aws_iam_user_policy_attachment" "pr_reviewer" {
  count      = var.create_iam_user ? 1 : 0
  user       = aws_iam_user.pr_reviewer[0].name
  policy_arn = aws_iam_policy.pr_reviewer.arn
}

# Access key written to Terraform state — run `terraform output -json` to retrieve.
# Rotate by destroying and re-creating: terraform destroy -target aws_iam_access_key.pr_reviewer
resource "aws_iam_access_key" "pr_reviewer" {
  count = var.create_iam_user ? 1 : 0
  user  = aws_iam_user.pr_reviewer[0].name
}
