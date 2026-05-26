"""
Shared pytest fixtures for the PR Review System test suite.

Key fixtures:
  - aws_credentials   : patches env with fake creds so moto intercepts boto3
  - dynamo_table      : creates the DynamoDB memory table in moto before each test
  - memory_store      : DynamoMemoryStore wired to the moto table
  - mock_llm          : MagicMock ChatBedrock that returns configurable JSON
  - sample_diff       : realistic unified diff with known security issues
  - sample_pr_meta    : PR metadata dict
  - full_state        : complete PRReviewState dict ready to pass to agent.run()
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws


# ── point boto3 at fake AWS before any import touches it ─────────────────────
@pytest.fixture(scope="session", autouse=True)
def aws_credentials():
    os.environ.update(
        {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_SECURITY_TOKEN": "test",
            "AWS_SESSION_TOKEN": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "DYNAMODB_TABLE": "pr-review-test",
        }
    )


# ── moto DynamoDB table ───────────────────────────────────────────────────────
@pytest.fixture
def dynamo_table(aws_credentials):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="pr-review-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK",         "AttributeType": "S"},
                {"AttributeName": "SK",         "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "SK-index",
                "KeySchema": [
                    {"AttributeName": "SK",         "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def memory_store(dynamo_table):
    from src.tools.dynamo_memory import DynamoMemoryStore

    return DynamoMemoryStore(table_name="pr-review-test", region="us-east-1")


# ── mock LLM ──────────────────────────────────────────────────────────────────
@pytest.fixture
def mock_llm():
    """Returns a MagicMock whose .invoke() yields a configurable JSON string."""
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="[]")
    return llm


def make_llm_return(llm: MagicMock, findings: list[dict]) -> None:
    """Helper: configure mock_llm to return a specific findings list."""
    llm.invoke.return_value = MagicMock(content=json.dumps(findings))


# ── PR data fixtures ──────────────────────────────────────────────────────────
@pytest.fixture
def sample_pr_meta() -> dict:
    return {
        "repo": "acme/backend",
        "number": 42,
        "sha": "abc123def456",
        "title": "Add JWT login endpoint",
        "body": "Implements POST /login with JWT auth.",
        "author": "dev_alice",
        "base": "main",
        "html_url": "https://github.com/acme/backend/pull/42",
    }


@pytest.fixture
def sample_diff() -> str:
    """A diff that contains hardcoded secrets, SQL injection, and MD5 usage."""
    return (
        "--- a/auth/views.py\n"
        "+++ b/auth/views.py\n"
        "@@ -0,0 +1,20 @@\n"
        "+import jwt, hashlib\n"
        "+from django.http import JsonResponse, HttpResponse\n"
        "+\n"
        '+DB_PASS    = "admin_secret_password"\n'
        '+JWT_SECRET = "hardcoded_jwt_signing_key"\n'
        "+\n"
        "+def login(request):\n"
        '+    username = request.POST.get("username", "")\n'
        '+    password = request.POST.get("password", "")\n'
        "+    pw_hash = hashlib.md5(password.encode()).hexdigest()\n"
        "+    sql = f\"SELECT * FROM users WHERE username='{username}'\"\n"
        "+    user = db.execute(sql).fetchone()\n"
        "+    if user and user.pw_hash == pw_hash:\n"
        "+        token = jwt.encode({'id': user.id}, JWT_SECRET)\n"
        "+        return JsonResponse({'token': token})\n"
        '+    return HttpResponse("Unauthorized", status=401)\n'
    )


@pytest.fixture
def full_state(sample_pr_meta, sample_diff) -> dict:
    """Complete PRReviewState ready to pass directly to agent.run()."""
    return {
        "messages": [],
        "pr_metadata": sample_pr_meta,
        "diff_content": sample_diff,
        "file_list": ["auth/views.py"],
        "code_quality_findings": [],
        "security_findings": [],
        "pr_review_findings": [],
        "next_agent": "",
        "completed_agents": [],
        "summary_report": "",
        "overall_score": 0.0,
        "should_block": False,
    }
