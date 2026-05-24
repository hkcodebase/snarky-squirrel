"""
PR Reviewer Agent — high-level review focusing on:
  - Does the PR accomplish its stated goal?
  - Are there missing test cases for edge conditions?
  - Are there better architectural approaches?
  - Backwards compatibility concerns
  - Missing documentation / changelog entry
  - PR size and atomicity

Reads code quality and security findings from shared memory for context.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from src.tools.dynamo_memory import DynamoMemoryStore

SYSTEM_PROMPT = """You are a principal engineer performing a high-level pull request review.
You focus on the *why* and *what*, not just the *how*.

Evaluate:
1. Goal alignment — does the diff match the PR title/description?
2. Completeness — are edge cases handled? are error paths tested?
3. Architecture — is this the right approach? is there a simpler solution?
4. Backwards compatibility — could this break existing callers or contracts?
5. Observability — are new code paths logged / metered / traced appropriately?
6. PR hygiene — is the PR focused? should it be split? are commit messages clear?
7. Migration concerns — schema changes, feature flags, deployment order?

For EVERY finding output a JSON object:
{
  "severity": "HIGH|MEDIUM|LOW|INFO",
  "category": "goal_alignment|completeness|architecture|compatibility|observability|hygiene|migration",
  "file": "<filename or 'general'>",
  "line": <line_number_or_null>,
  "title": "<short title>",
  "detail": "<explanation in 1-2 sentences>",
  "recommendation": "<concrete actionable fix or question for the author>"
}

Return a JSON array. If everything looks good, return [{"severity":"INFO","category":"hygiene","file":"general","line":null,"title":"PR looks well-scoped","detail":"No major concerns.","recommendation":""}].
Do NOT include any text outside the JSON array.
"""


class PRReviewerAgent:
    def __init__(self, llm, memory_store: DynamoMemoryStore):
        self.llm = llm
        self.memory = memory_store

    def run(self, state: dict) -> dict:
        started_at = datetime.now(tz=timezone.utc).isoformat()
        t0 = time.monotonic()

        diff = state["diff_content"]
        pr_meta = state["pr_metadata"]
        thread_id = pr_meta.get("thread_id") or f"pr-{pr_meta['repo']}-{pr_meta['number']}"

        # Pull prior findings from shared memory for richer context
        prior_context_parts = []
        for key in ("security_findings", "code_quality_findings"):
            try:
                val = self.memory.get(thread_id, key)
                if val:
                    prior_context_parts.append(f"{key}: {val[:600]}")
            except Exception:
                pass
        prior_context = "\n".join(prior_context_parts)

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"PR title: {pr_meta.get('title', '')}\n"
                    f"PR description: {pr_meta.get('body', 'No description provided')}\n"
                    f"Author: {pr_meta.get('author', 'unknown')}\n"
                    f"Base branch: {pr_meta.get('base', 'main')}\n"
                    f"Files changed: {', '.join(state.get('file_list', []))}\n\n"
                    f"Earlier agent findings (for context):\n{prior_context}\n\n"
                    f"Diff:\n```\n{diff[:10000]}\n```"
                )
            ),
        ]
        response = self.llm.invoke(messages)

        findings: list[dict] = []
        try:
            raw = response.content.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            findings = json.loads(raw)
        except (json.JSONDecodeError, AttributeError):
            findings = []

        self.memory.put(thread_id, "pr_review_findings", json.dumps(findings))

        # Record lineage
        sev_counts = {s: sum(1 for f in findings if f.get("severity") == s)
                      for s in ["HIGH", "MEDIUM", "LOW"]}
        self.memory.put(thread_id, "lineage_pr_reviewer", json.dumps({
            "agent": "pr_reviewer",
            "started_at": started_at,
            "completed_at": datetime.now(tz=timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "reads_from": ["security_findings", "code_quality_findings"],
            "writes_to": ["pr_review_findings"],
            "input_summary": {
                "diff_chars": len(diff),
                "files_count": len(state.get("file_list", [])),
                "prior_context_chars": len(prior_context),
            },
            "output_summary": {"total_findings": len(findings), **sev_counts},
        }))

        completed = list(state.get("completed_agents", []))
        if "pr_reviewer" not in completed:
            completed.append("pr_reviewer")

        return {"pr_review_findings": findings, "completed_agents": completed}
