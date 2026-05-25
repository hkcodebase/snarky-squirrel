# ─── DynamoDB table ───────────────────────────────────────────────────────────
#
# Single-table design:
#   PK (hash)    = thread_id   e.g. "pr-owner-repo-42-a1b2c3d4"
#   SK (range)   = key         e.g. "lineage_run" | "__checkpoint__"
#   value        = JSON string written by each agent
#   ttl          = Unix epoch expiry (72 h default, enforced by DynamoDB TTL)
#   created_at   = ISO-8601 UTC timestamp, written on every put()
#
# SK-index GSI (hash=SK, range=created_at) lets the API efficiently list
# all records of a given type in reverse-chronological order without a full
# table scan — used by GET /lineage, GET /eval/metrics, GET /admin/invite-requests.

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

  attribute {
    name = "created_at"
    type = "S"
  }

  # GSI: query by record type (SK), sorted newest-first by created_at.
  # Replaces all scan+FilterExpression patterns.
  global_secondary_index {
    name            = "SK-index"
    hash_key        = "SK"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
