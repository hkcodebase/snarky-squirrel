"""
End-to-end regression tests using mocked LLM and moto DynamoDB.

These tests run the full LangGraph graph compile → invoke cycle without
real LLM calls or AWS credentials.  They catch graph topology regressions
(broken edges, missing state keys, DynamoDB write failures).

Run with:
    pytest tests/test_regression.py -v -m integration
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws


# ─────────────────────────── helpers ─────────────────────────────────────────


def _make_llm(responses: list[str]):
    """LLM mock whose .invoke() returns responses in sequence (cycles on exhaustion)."""
    llm = MagicMock()
    # Cycle the list so the mock never raises StopIteration
    from itertools import cycle
    llm.invoke.side_effect = [MagicMock(content=r) for r in responses * 20]
    return llm


def _critical_llm():
    """LLM that always returns a CRITICAL finding."""
    finding = json.dumps([{
        "severity": "CRITICAL",
        "category": "secrets",
        "file": "app.py",
        "line": 1,
        "title": "Hardcoded API secret",
        "detail": "A literal secret was committed.",
        "recommendation": "Move to environment variable.",
    }])
    return _make_llm([finding])


def _empty_llm():
    """LLM that always returns an empty findings list."""
    return _make_llm(["[]"])


# ─────────────────────────── shared DynamoDB fixture ──────────────────────────


@pytest.fixture
def graph_table(aws_credentials):
    """Fresh moto DynamoDB table with the full production schema for each test."""
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


# ─────────────────────────── tests ───────────────────────────────────────────


@pytest.mark.integration
class TestFullGraph:

    def test_graph_completes_with_all_required_keys(self, graph_table, full_state):
        """The final state must contain all keys the API and UI depend on."""
        with patch("src.graph.pr_review_graph.get_llm", return_value=_empty_llm()):
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )

        assert isinstance(result.get("overall_score"), float)
        assert isinstance(result.get("should_block"), bool)
        assert isinstance(result.get("summary_report"), str)
        assert isinstance(result.get("score_breakdown"), dict)
        assert set(result.get("completed_agents", [])) >= {
            "security", "code_quality", "pr_reviewer", "summary"
        }

    def test_critical_finding_sets_should_block(self, graph_table, full_state):
        """Any CRITICAL finding from any agent must propagate should_block=True."""
        with patch("src.graph.pr_review_graph.get_llm", return_value=_critical_llm()):
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )

        assert result["should_block"] is True
        assert result["overall_score"] < 10.0

    def test_empty_findings_gives_score_10(self, graph_table, full_state):
        """When every agent returns no findings, the score must be 10.0."""
        with patch("src.graph.pr_review_graph.get_llm", return_value=_empty_llm()):
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )

        assert result["overall_score"] == pytest.approx(10.0, abs=0.01)
        assert result["should_block"] is False

    def test_lineage_run_written_to_dynamodb(self, graph_table, full_state):
        """run_pr_review must write a lineage_run record to DynamoDB."""
        with patch("src.graph.pr_review_graph.get_llm", return_value=_empty_llm()):
            from src.graph.pr_review_graph import run_pr_review
            from src.tools.dynamo_memory import DynamoMemoryStore
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )

        thread_id = result["pr_metadata"]["thread_id"]
        store = DynamoMemoryStore(table_name="pr-review-test", region="us-east-1")
        lr_raw = store.get(thread_id, "lineage_run")
        assert lr_raw is not None, "lineage_run record should exist in DynamoDB"

        lr = json.loads(lr_raw)
        assert "final_score" in lr
        assert "total_duration_ms" in lr
        assert "agent_order" in lr
        assert isinstance(lr["final_score"], float)

    def test_large_diff_does_not_crash(self, graph_table, full_state):
        """A diff exceeding the 50k truncation limit must complete without error."""
        large_diff = "+x = 1\n" * 9_000   # ~63k chars
        with patch("src.graph.pr_review_graph.get_llm", return_value=_empty_llm()):
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                large_diff,
                full_state["file_list"],
            )

        assert "overall_score" in result

    def test_malformed_llm_json_does_not_crash(self, graph_table, full_state):
        """If every LLM call returns invalid JSON, the graph should recover
        and return a valid (empty-findings) result rather than raising."""
        with patch("src.graph.pr_review_graph.get_llm",
                   return_value=_make_llm(["not valid json !!!", "also not json"])):
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )

        # Should fall back to empty findings — not raise
        assert isinstance(result.get("overall_score"), float)

    def test_explicit_llm_param_used(self, graph_table, full_state):
        """run_pr_review(llm=...) should use the provided LLM, not call get_llm()."""
        explicit_llm = _critical_llm()

        # get_llm should NOT be called when an explicit llm is passed
        with patch("src.graph.pr_review_graph.get_llm") as mock_get_llm:
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
                llm=explicit_llm,
            )
            mock_get_llm.assert_not_called()

        assert result["should_block"] is True


@pytest.mark.integration
class TestDynamoGSI:
    """Verify query_by_type and query_all_by_type work with the full GSI schema."""

    def test_query_by_type_returns_written_items(self, graph_table):
        from src.tools.dynamo_memory import DynamoMemoryStore
        store = DynamoMemoryStore(table_name="pr-review-test", region="us-east-1")

        store.put("thread-a", "lineage_run", json.dumps({"final_score": 8.5}))
        store.put("thread-b", "lineage_run", json.dumps({"final_score": 6.0}))

        items, cursor = store.query_by_type("lineage_run", limit=10)
        assert len(items) == 2
        assert cursor is None

    def test_query_all_by_type_paginates(self, graph_table):
        from src.tools.dynamo_memory import DynamoMemoryStore
        store = DynamoMemoryStore(table_name="pr-review-test", region="us-east-1")

        for i in range(5):
            store.put(f"thread-{i}", "eval_feedback", json.dumps({"thumbs": "up"}))

        all_items = store.query_all_by_type("eval_feedback")
        assert len(all_items) == 5
