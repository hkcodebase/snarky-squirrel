# ─── DynamoDB table ───────────────────────────────────────────────────────────
#
# Single-table design:
#   PK (hash)  = thread_id  e.g. "pr-owner-repo-42-a1b2c3d4"
#   SK (range) = key        e.g. "security_findings" | "lineage_run" | "__checkpoint__"
#   value      = JSON string written by each agent
#   ttl        = Unix epoch expiry (72 h default, enforced by DynamoDB TTL)
#
# A GSI on SK lets the API efficiently list all lineage_run records
# without a full table scan (used by GET /lineage).

resource "aws_dynamodb_table" "pr_review_memory" {
  name         = "${var.dynamo_table_name}-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # SK-only GSI so /lineage can query FilterExpression SK = "lineage_run"
  # without scanning the whole table.
  global_secondary_index {
    name            = "SK-index"
    hash_key        = "SK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
