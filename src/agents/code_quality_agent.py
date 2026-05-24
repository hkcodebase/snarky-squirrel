"""
Code Quality Agent — reviews the PR diff for:
  - Code style and readability issues
  - Cyclomatic complexity hotspots
  - Anti-patterns and code smells
  - Missing or inadequate tests
  - Documentation gaps
  - Performance red flags (N+1 queries, large allocations in hot paths)

Reads security findings from shared memory to avoid duplicating recommendations.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from src.tools.dynamo_memory import DynamoMemoryStore

SYSTEM_PROMPT = """You are a senior software engineer performing a thorough code quality review.
Analyse the unified diff provided.

Evaluate across these dimensions:
1. Readability — naming, function length, magic numbers, comments
2. Complexity — deeply nested logic, large functions, high cyclomatic complexity
3. Design patterns — SOLID violations, coupling, cohesion issues
4. Error handling — uncaught exceptions, silent failures, broad except clauses
5. Test coverage — are new code paths tested? are edge cases covered?
6. Performance — obvious N+1 queries, unnecessary loops, large in-memory datasets
7. Documentation — missing docstrings for public APIs, unclear parameter names

For EVERY finding output a JSON object:
{
  "severity": "HIGH|MEDIUM|LOW|INFO",
  "category": "readability|complexity|design|error_handling|test_coverage|performance|documentation",
  "file": "<filename>",
  "line": <line_number_or_null>,
  "title": "<short title>",
  "detail": "<explanation in 1-2 sentences>",
  "recommendation": "<concrete actionable fix>",
  "code_suggestion": "<optional: a corrected code snippet, max 8 lines>"
}

Return a JSON array of findings. If no issues, return [].
Do NOT include any text outside the JSON array.
"""

POSITIVE_PROMPT = """Also identify 1-3 things done particularly well in this PR.
Return as JSON array with schema: [{"title": "...", "detail": "..."}]
These will be highlighted in the review to encourage good practices.
"""


class CodeQualityAgent:
    def __init__(self, llm, memory_store: DynamoMemoryStore):
        self.llm = llm
        self.memory = memory_store

    def run(self, state: dict) -> dict:
        started_at = datetime.now(tz=timezone.utc).isoformat()
        t0 = time.monotonic()

        diff = state["diff_content"]
        pr_meta = state["pr_metadata"]
        thread_id = pr_meta.get("thread_id") or f"pr-{pr_meta['repo']}-{pr_meta['number']}"

        # Read security findings from shared memory to avoid overlap
        security_context = ""
        try:
            sec = self.memory.get(thread_id, "security_findings")
            if sec:
                security_context = f"\n\nNote: Security agent already flagged these files — skip security issues: {sec[:500]}"
        except Exception:
            pass

        messages = [
            SystemMessage(content=SYSTEM_PROMPT + security_context),
            HumanMessage(
                content=(
                    f"PR: {pr_meta.get('title', '')}\n"
                    f"Author: {pr_meta.get('author', 'unknown')}\n"
                    f"Files changed: {', '.join(state.get('file_list', []))}\n\n"
                    f"Diff:\n```\n{diff[:12000]}\n```"
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

        # Fetch positive highlights in a second LLM call
        pos_messages = [
            SystemMessage(content=POSITIVE_PROMPT),
            HumanMessage(content=f"Diff:\n```\n{diff[:8000]}\n```"),
        ]
        pos_response = self.llm.invoke(pos_messages)
        positives: list[dict] = []
        try:
            raw = pos_response.content.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            positives = json.loads(raw)
        except Exception:
            positives = []

        # Attach positives as INFO-level findings
        for p in positives:
            findings.append(
                {
                    "severity": "INFO",
                    "category": "positive",
                    "file": "general",
                    "line": None,
                    "title": f"✓ {p.get('title', '')}",
                    "detail": p.get("detail", ""),
                    "recommendation": "",
                }
            )

        # Write to shared memory
        self.memory.put(thread_id, "code_quality_findings", json.dumps(findings))

        # Record lineage
        non_pos = [f for f in findings if f.get("category") != "positive"]
        sev_counts = {s: sum(1 for f in non_pos if f.get("severity") == s)
                      for s in ["HIGH", "MEDIUM", "LOW"]}
        self.memory.put(thread_id, "lineage_code_quality", json.dumps({
            "agent": "code_quality",
            "started_at": started_at,
            "completed_at": datetime.now(tz=timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "reads_from": ["security_findings"],
            "writes_to": ["code_quality_findings"],
            "input_summary": {
                "diff_chars": len(diff),
                "files_count": len(state.get("file_list", [])),
            },
            "output_summary": {
                "total_findings": len(non_pos),
                **sev_counts,
                "positives": len(findings) - len(non_pos),
            },
        }))

        completed = list(state.get("completed_agents", []))
        if "code_quality" not in completed:
            completed.append("code_quality")

        return {"code_quality_findings": findings, "completed_agents": completed}
