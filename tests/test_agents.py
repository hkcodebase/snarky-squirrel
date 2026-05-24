"""
Unit tests for PR Review System agents and utilities.

Uses moto to mock AWS services — no real AWS credentials required.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

# Ensure we use mock AWS creds in tests
os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "DYNAMODB_TABLE": "test-memory",
    }
)


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture
def sample_diff() -> str:
    return """\
--- a/auth/views.py
+++ b/auth/views.py
@@ -10,0 +11,15 @@
+def login(request):
+    DB_PASSWORD = "super_secret_password_123"
+    username = request.POST.get("username")
+    user = User.objects.get(username=username)
+    if user.check_password(request.POST.get("password")):
+        token = jwt.encode({"id": user.id}, "hardcoded_jwt_secret_key_xyz")
+        return JsonResponse({"token": token})
+    return HttpResponse("Unauthorized", status=401)
"""


@pytest.fixture
def sample_pr_meta() -> dict:
    return {
        "repo": "acme/backend",
        "number": 42,
        "sha": "abc123def456",
        "title": "Add user login endpoint",
        "body": "Implements POST /login with JWT",
        "author": "dev_user",
        "base": "main",
    }


@pytest.fixture
def mock_dynamo_store():
    store = MagicMock()
    store.get.return_value = None
    store.put.return_value = None
    return store


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    return llm


# ─────────────────────────── security agent tests ────────────────────────────


class TestSecurityAgent:
    def test_regex_prescan_detects_secrets(self, sample_diff):
        from src.agents.security_agent import regex_prescan

        findings = regex_prescan(sample_diff)
        assert len(findings) > 0
        categories = [f["category"] for f in findings]
        assert "secrets" in categories

    def test_regex_prescan_detects_password(self, sample_diff):
        from src.agents.security_agent import regex_prescan

        findings = regex_prescan(sample_diff)
        titles = " ".join(f["title"] for f in findings).lower()
        assert "password" in titles or "credential" in titles

    def test_regex_prescan_only_flags_added_lines(self):
        from src.agents.security_agent import regex_prescan

        diff_with_removed = "-    DB_PASSWORD = 'super_secret'\n"
        findings = regex_prescan(diff_with_removed)
        assert len(findings) == 0, "Should not flag removed lines"

    def test_run_marks_should_block_on_critical(
        self, sample_diff, sample_pr_meta, mock_dynamo_store, mock_llm
    ):
        from src.agents.security_agent import SecurityAgent

        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "severity": "CRITICAL",
                        "category": "secrets",
                        "file": "auth/views.py",
                        "line": 12,
                        "title": "Hardcoded JWT secret",
                        "detail": "JWT signed with literal string.",
                        "recommendation": "Use environment variable.",
                    }
                ]
            )
        )

        agent = SecurityAgent(mock_llm, mock_dynamo_store)
        state = {
            "diff_content": sample_diff,
            "pr_metadata": sample_pr_meta,
            "completed_agents": [],
        }
        result = agent.run(state)

        assert result["should_block"] is True
        assert len(result["security_findings"]) > 0
        assert "security" in result["completed_agents"]

    def test_run_does_not_block_on_low_only(
        self, mock_dynamo_store, mock_llm, sample_pr_meta
    ):
        from src.agents.security_agent import SecurityAgent

        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "severity": "LOW",
                        "category": "other",
                        "file": "utils.py",
                        "line": 5,
                        "title": "Minor issue",
                        "detail": "Something minor.",
                        "recommendation": "Fix it.",
                    }
                ]
            )
        )

        agent = SecurityAgent(mock_llm, mock_dynamo_store)
        state = {
            "diff_content": "+ x = 1\n",
            "pr_metadata": sample_pr_meta,
            "completed_agents": [],
        }
        result = agent.run(state)
        assert result["should_block"] is False


# ─────────────────────────── code quality agent tests ────────────────────────


class TestCodeQualityAgent:
    def test_run_appends_to_completed_agents(
        self, mock_dynamo_store, mock_llm, sample_pr_meta, sample_diff
    ):
        from src.agents.code_quality_agent import CodeQualityAgent

        mock_llm.invoke.return_value = MagicMock(content="[]")

        agent = CodeQualityAgent(mock_llm, mock_dynamo_store)
        result = agent.run(
            {
                "diff_content": sample_diff,
                "pr_metadata": sample_pr_meta,
                "file_list": ["auth/views.py"],
                "completed_agents": ["security"],
            }
        )

        assert "code_quality" in result["completed_agents"]
        assert "security" in result["completed_agents"]

    def test_run_handles_malformed_llm_response(
        self, mock_dynamo_store, mock_llm, sample_pr_meta
    ):
        from src.agents.code_quality_agent import CodeQualityAgent

        mock_llm.invoke.return_value = MagicMock(content="This is not JSON at all!")

        agent = CodeQualityAgent(mock_llm, mock_dynamo_store)
        result = agent.run(
            {
                "diff_content": "diff",
                "pr_metadata": sample_pr_meta,
                "file_list": [],
                "completed_agents": [],
            }
        )

        # Should not raise; should return empty findings
        assert isinstance(result["code_quality_findings"], list)


# ─────────────────────────── summary agent tests ─────────────────────────────


class TestSummaryAgent:
    def test_compute_score_full_marks_for_empty(self):
        from src.agents.summary_agent import compute_score

        assert compute_score([]) == 10.0

    def test_compute_score_deducts_for_critical(self):
        from src.agents.summary_agent import compute_score

        findings = [{"severity": "CRITICAL", "category": "secrets"}]
        assert compute_score(findings) < 10.0

    def test_compute_score_clamps_to_zero(self):
        from src.agents.summary_agent import compute_score

        findings = [{"severity": "CRITICAL", "category": "secrets"}] * 20
        assert compute_score(findings) == 0.0

    def test_render_github_comment_contains_score(self):
        from src.agents.summary_agent import render_github_comment

        comment = render_github_comment(
            pr_meta={"repo": "a/b", "number": 1, "title": "Test"},
            all_findings=[],
            score=8.5,
            should_block=False,
        )
        assert "8.5/10" in comment

    def test_render_github_comment_blocked_contains_caution(self):
        from src.agents.summary_agent import render_github_comment

        comment = render_github_comment(
            pr_meta={"repo": "a/b", "number": 1, "title": "Test"},
            all_findings=[
                {
                    "severity": "CRITICAL",
                    "category": "secrets",
                    "title": "Key",
                    "detail": "x",
                    "recommendation": "y",
                    "file": "a.py",
                    "line": 1,
                }
            ],
            score=3.0,
            should_block=True,
        )
        assert "BLOCKED" in comment or "CAUTION" in comment


# ─────────────────────────── dynamo memory tests ─────────────────────────────


class TestDynamoMemoryStore:
    def test_put_and_get_roundtrip(self):
        """Integration test using moto mock DynamoDB."""
        try:
            import moto
        except ImportError:
            pytest.skip("moto not installed")

        import boto3
        from moto import mock_aws

        @mock_aws
        def _test():
            # Create table
            client = boto3.client("dynamodb", region_name="us-east-1")
            client.create_table(
                TableName="test-memory",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )

            from src.tools.dynamo_memory import DynamoMemoryStore

            store = DynamoMemoryStore(table_name="test-memory", region="us-east-1")
            store.put("thread-1", "my_key", "hello world")
            result = store.get("thread-1", "my_key")
            assert result == "hello world"

            # Missing key
            missing = store.get("thread-1", "nonexistent")
            assert missing is None

        _test()
