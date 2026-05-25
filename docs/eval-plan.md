# Evaluation Plan — Step-by-Step Checklist

Work through these steps in order. Each step is self-contained. Check off as you go.

---

## Phase 1 — Fix existing gaps (do these first)

### Step 1 — Add GSI to the conftest DynamoDB table

**File:** `tests/conftest.py`

The `dynamo_table` fixture creates the table without the `created_at` attribute or
`SK-index` GSI, so any test that calls `query_by_type()` or `query_all_by_type()` silently
returns nothing. Update the fixture to match the real schema.

Replace the `create_table` call in the `dynamo_table` fixture with:

```python
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
```

Run `pytest tests/` — all existing tests should still pass.

---

### Step 2 — Fix the shadow mode race condition

**File:** `api.py`, function `eval_run`, mode `"shadow"` branch (~line 705)

The current code swaps `os.environ["LLM_PROVIDER"]` and `os.environ["LLM_MODEL"]` to
run the shadow model. This is not thread-safe — two simultaneous shadow requests corrupt
each other's env vars.

**Fix:** Instead of swapping env vars, instantiate the shadow LLM directly from the
request parameters and pass it straight into the graph.

1. In `src/graph/pr_review_graph.py`, add a parameter to `build_pr_review_graph` and
   `run_pr_review` that accepts an optional pre-built `llm` instance:

   ```python
   def run_pr_review(pr_metadata, diff_content, file_list, llm=None):
       ...
       app = build_pr_review_graph(llm=llm)
   
   def build_pr_review_graph(use_dynamo_checkpointer=True, llm=None):
       llm = llm or get_llm()
       ...
   ```

2. In `api.py` shadow branch, build the shadow LLM directly:

   ```python
   # build shadow LLM from request params — no env-var swap needed
   saved_provider = os.environ.get("LLM_PROVIDER")
   saved_model    = os.environ.get("LLM_MODEL")
   os.environ["LLM_PROVIDER"] = s_provider
   os.environ["LLM_MODEL"]    = s_model
   shadow_llm = get_llm()                  # reads the overridden vars once
   os.environ["LLM_PROVIDER"] = saved_provider or ""
   os.environ["LLM_MODEL"]    = saved_model    or ""
   
   shadow = run_pr_review(dict(pr_meta), diff_content, file_list, llm=shadow_llm)
   ```

   Or better — expose a `get_llm(provider, model)` signature so no env mutation is needed.

3. Verify the fix: run two shadow requests simultaneously in the browser and confirm
   both return results with the correct provider in `shadow_config`.

---

### Step 3 — Add score formula unit tests

**File:** `tests/test_agents.py`, inside `TestSummaryAgent`

Add these parameterized cases. They are pure arithmetic — no mocks needed:

```python
import pytest
from src.agents.summary_agent import compute_score, compute_score_breakdown

@pytest.mark.parametrize("findings,expected", [
    # Empty → perfect score
    ([], 10.0),
    # One CRITICAL → 10 - 3.0 = 7.0
    ([{"severity": "CRITICAL", "category": "secrets"}], 7.0),
    # One HIGH → 10 - 1.5 = 8.5
    ([{"severity": "HIGH", "category": "other"}], 8.5),
    # One MEDIUM → 10 - 0.5 = 9.5
    ([{"severity": "MEDIUM", "category": "other"}], 9.5),
    # One LOW → 10 - 0.1 = 9.9
    ([{"severity": "LOW", "category": "other"}], 9.9),
    # Clamp at 0 — four CRITICALs = 10 - 12 = -2 → clamped to 0
    ([{"severity": "CRITICAL", "category": "secrets"}] * 4, 0.0),
    # Positives are excluded from deductions
    ([{"severity": "CRITICAL", "category": "positive"}], 10.0),
    # Positive boost: 1 positive adds 0.1
    ([{"severity": "INFO", "category": "positive"}], 10.0),  # no deduction, no boost beyond 10
])
def test_score_formula(findings, expected):
    assert compute_score(findings) == expected
```

Also assert that `compute_score_breakdown` returns all expected keys:
```python
def test_score_breakdown_keys():
    bd = compute_score_breakdown([{"severity": "HIGH", "category": "x"}])
    for key in ("severity_counts", "severity_deductions", "total_deduction",
                "positive_count", "final_score", "raw_score"):
        assert key in bd
```

Run: `pytest tests/test_agents.py::TestSummaryAgent -v`

---

## Phase 2 — Extend the regex prescan tests

### Step 4 — Add prescan false-positive tests

**File:** `tests/test_agents.py`, inside `TestSecurityAgent`

Add cases for patterns that should **not** trigger the prescan:

```python
@pytest.mark.parametrize("line,should_flag", [
    # Should flag (added lines with real secrets)
    ("+AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'",   True),
    ("+password = 'super_secret_password_123'",         True),
    ("+secret_key = 'my-signing-key-value'",            True),
    ("+conn = 'postgres://user:pass@localhost/db'",      True),
    # Should NOT flag (removed lines)
    ("-password = 'old_password'",                      False),
    # Should NOT flag (reading from env)
    ("+api_key = os.environ.get('API_KEY')",            False),  # currently may flag — fix if so
    ("+secret = os.getenv('SECRET_KEY')",               False),
    # Should NOT flag (context lines, no + prefix)
    (" password = 'some_value'",                        False),
])
def test_prescan_parametrized(self, line, should_flag):
    from src.agents.security_agent import regex_prescan
    findings = regex_prescan(line + "\n")
    if should_flag:
        assert len(findings) > 0, f"Expected a finding for: {line}"
    else:
        assert len(findings) == 0, f"Expected no finding for: {line}"
```

If `os.environ.get(...)` lines are currently flagging as false positives, add a
guard in `regex_prescan`: skip lines that match `os\.environ\.get|os\.getenv|environ\[`.

---

### Step 5 — Add a test for the diff truncation boundary

**File:** `tests/test_agents.py`, new class `TestDiffHandling`

Verify that a diff right at and above the 50,000-char limit is handled gracefully:

```python
class TestDiffHandling:
    def test_diff_at_truncation_boundary(self, sample_pr_meta, mock_dynamo_store, mock_llm):
        """A 50,001-char diff should truncate cleanly — no index error, review completes."""
        from src.agents.security_agent import SecurityAgent

        mock_llm.invoke.return_value = MagicMock(content="[]")
        large_diff = "+x = 1\n" * 8000   # ~56 000 chars
        agent = SecurityAgent(mock_llm, mock_dynamo_store)
        result = agent.run({
            "diff_content": large_diff,
            "pr_metadata": sample_pr_meta,
            "completed_agents": [],
        })
        assert isinstance(result["security_findings"], list)

    def test_secret_near_truncation_boundary_may_be_missed(self, sample_pr_meta, mock_dynamo_store, mock_llm):
        """Document the known limitation: regex catches secrets at any position,
        LLM only sees the first 12,000 chars."""
        from src.agents.security_agent import SecurityAgent, regex_prescan

        # Place a secret at char 45,000 — LLM won't see it, regex will
        padding = "+x = 1\n" * 6000           # ~42 000 chars
        secret_line = "+AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'\n"
        diff = padding + secret_line

        regex_hits = regex_prescan(diff)
        assert len(regex_hits) > 0, "Regex should catch secrets beyond LLM window"
```

---

## Phase 3 — Build the golden fixture set

### Step 6 — Create the golden fixture directory

Create the folder structure. Each scenario is a subdirectory with three files:

```
tests/fixtures/golden/
├── 01_sql_injection/
│   ├── diff.patch
│   ├── pr_meta.json
│   └── expected.json
├── 02_hardcoded_aws_key/
├── 03_hardcoded_password/
├── 04_md5_password_hash/
├── 05_private_key_committed/
├── 06_stripe_live_key/
├── 07_path_traversal/
├── 08_missing_auth_check/
├── 09_n_plus_one_query/
├── 10_missing_tests/
├── 11_god_class/
├── 12_backwards_incompatible/
├── 13_clean_refactor/       ← should score ≥ 9, no block
├── 14_env_var_read/         ← should NOT flag as secret
└── 15_large_diff/           ← diff near 50k chars
```

**`pr_meta.json` schema:**
```json
{
  "repo": "acme/backend",
  "number": 1,
  "sha": "abc123",
  "title": "Add login endpoint",
  "body": "Implements POST /login",
  "author": "dev_alice",
  "base": "main"
}
```

**`expected.json` schema:**
```json
{
  "must_find": ["sql injection", "hardcoded password"],
  "must_not_flag": [],
  "should_block": true,
  "score_range": [0, 5],
  "min_security_findings": 2,
  "notes": "Classic login endpoint with SQL injection and hardcoded credentials"
}
```

For `must_find`, use lowercase substrings that should appear in at least one finding
`title` or `detail`. Case-insensitive match is fine.

---

### Step 7 — Write the `diff.patch` files for each scenario

Write realistic unified diffs for each scenario. Some starting points:

**01_sql_injection/diff.patch:**
```diff
--- a/auth/views.py
+++ b/auth/views.py
@@ -0,0 +1,8 @@
+def login(request):
+    username = request.POST.get("username")
+    sql = f"SELECT * FROM users WHERE username='{username}'"
+    user = db.execute(sql).fetchone()
+    if user:
+        return JsonResponse({"status": "ok"})
+    return HttpResponse(status=401)
```

**13_clean_refactor/diff.patch** (should score high — no issues):
```diff
--- a/utils/formatters.py
+++ b/utils/formatters.py
@@ -1,10 +1,12 @@
-def fmt(x):
-    return str(x).strip().lower()
+def format_slug(text: str) -> str:
+    """Normalise a string to a URL-safe slug."""
+    return str(text).strip().lower().replace(" ", "-")
```

Write all 15 diffs. Aim for realistic code — copy patterns from real CVEs or your
own codebase for the vulnerability scenarios.

---

### Step 8 — Write the golden dataset batch runner

**New file:** `scripts/eval_golden.py`

```python
#!/usr/bin/env python3
"""
Run all golden fixture scenarios through the agent pipeline and report pass/fail.

Usage:
    python scripts/eval_golden.py                     # run all scenarios
    python scripts/eval_golden.py 01_sql_injection    # run one scenario
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

GOLDEN_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "golden"


def load_scenario(scenario_dir: Path):
    diff      = (scenario_dir / "diff.patch").read_text()
    pr_meta   = json.loads((scenario_dir / "pr_meta.json").read_text())
    expected  = json.loads((scenario_dir / "expected.json").read_text())
    return diff, pr_meta, expected


def all_findings(result: dict) -> list[dict]:
    return (
        result.get("security_findings", [])
        + result.get("code_quality_findings", [])
        + result.get("pr_review_findings", [])
    )


def evaluate_scenario(name: str, diff: str, pr_meta: dict, expected: dict) -> dict:
    from src.graph.pr_review_graph import run_pr_review

    file_list = list({
        line[6:].split("\t")[0]
        for line in diff.splitlines()
        if line.startswith("+++ b/")
    })

    result   = run_pr_review(pr_meta, diff, file_list)
    findings = all_findings(result)
    titles   = " ".join((f.get("title", "") + " " + f.get("detail", "")).lower()
                        for f in findings)

    checks = {}

    # must_find
    for term in expected.get("must_find", []):
        checks[f"must_find:{term}"] = term.lower() in titles

    # must_not_flag
    for term in expected.get("must_not_flag", []):
        checks[f"must_not_flag:{term}"] = term.lower() not in titles

    # should_block
    if "should_block" in expected:
        checks["should_block"] = result["should_block"] == expected["should_block"]

    # score_range
    if "score_range" in expected:
        lo, hi = expected["score_range"]
        checks["score_range"] = lo <= result["overall_score"] <= hi

    # min_security_findings
    if "min_security_findings" in expected:
        sec = result.get("security_findings", [])
        checks["min_security_findings"] = len(sec) >= expected["min_security_findings"]

    passed  = all(checks.values())
    return {"name": name, "passed": passed, "checks": checks,
            "score": result["overall_score"], "should_block": result["should_block"]}


def main():
    filter_name = sys.argv[1] if len(sys.argv) > 1 else None
    scenarios   = sorted(GOLDEN_DIR.iterdir()) if GOLDEN_DIR.exists() else []

    results, failures = [], []
    for s in scenarios:
        if not s.is_dir():
            continue
        if filter_name and filter_name not in s.name:
            continue
        print(f"  Running {s.name} ...", end=" ", flush=True)
        diff, pr_meta, expected = load_scenario(s)
        r = evaluate_scenario(s.name, diff, pr_meta, expected)
        results.append(r)
        if r["passed"]:
            print("✓ PASS")
        else:
            print("✗ FAIL")
            failures.append(r)

    print(f"\n{'='*60}")
    print(f"  {len(results) - len(failures)}/{len(results)} scenarios passed")

    if failures:
        print("\nFailed checks:")
        for f in failures:
            bad = [k for k, v in f["checks"].items() if not v]
            print(f"  {f['name']}: {bad}")
        sys.exit(1)
    else:
        print("  All golden scenarios passed.")


if __name__ == "__main__":
    main()
```

Test it locally after writing at least one scenario:
```
python scripts/eval_golden.py 01_sql_injection
```

---

## Phase 4 — Per-finding feedback

### Step 9 — Add per-finding thumbs to the UI

**File:** `templates/index.html`

In the review detail modal, each finding card should have a small feedback row.
After the finding's recommendation text, add:

```html
<div class="finding-feedback" data-thread="{{thread_id}}" data-idx="{{i}}">
  <button class="fb-btn" data-val="up"   title="Useful">👍</button>
  <button class="fb-btn" data-val="down" title="Not relevant">👎</button>
  <span class="fb-sent" style="display:none">Saved</span>
</div>
```

Wire up a JS handler:
```js
document.addEventListener("click", async e => {
  const btn = e.target.closest(".fb-btn");
  if (!btn) return;
  const row = btn.closest(".finding-feedback");
  await fetch("/eval/feedback/finding", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      thread_id: row.dataset.thread,
      finding_index: parseInt(row.dataset.idx),
      thumbs: btn.dataset.val,
    }),
  });
  row.querySelector(".fb-sent").style.display = "";
});
```

---

### Step 10 — Add the per-finding feedback endpoint

**File:** `api.py`

Add a new Pydantic model and endpoint:

```python
class FindingFeedbackRequest(BaseModel):
    thread_id: str
    finding_index: int
    thumbs: str  # "up" | "down"

@app.post("/eval/feedback/finding")
def eval_feedback_finding(req: FindingFeedbackRequest):
    """Store thumbs up/down for a specific finding within a review."""
    store = DynamoMemoryStore()
    store.put(
        req.thread_id,
        f"finding_feedback_{req.finding_index}",
        json.dumps({
            "thread_id":     req.thread_id,
            "finding_index": req.finding_index,
            "thumbs":        req.thumbs,
            "submitted_at":  datetime.now(tz=timezone.utc).isoformat(),
        }),
    )
    return {"ok": True}
```

Also update `/eval/metrics` to aggregate per-finding precision:
```python
# count all finding_feedback_* keys
confirmed = rejected = 0
for item in store.query_all_by_type_prefix("finding_feedback_"):  # needs new helper or scan SK prefix
    fb = json.loads(item["value"]["S"])
    if fb["thumbs"] == "up":   confirmed += 1
    if fb["thumbs"] == "down": rejected  += 1
metrics["finding_precision_pct"] = round(
    confirmed / max(confirmed + rejected, 1) * 100, 1
)
```

Note: querying by SK prefix requires either a `begins_with` filter on the GSI or a separate
GSI. The simplest approach for now is to store all finding feedback with `SK = "finding_feedback"`
and encode the index in the JSON `value`, then filter client-side in the metrics endpoint.

---

## Phase 5 — Calibration & consistency

### Step 11 — Collect calibration PRs

Manually select 20 real merged PRs you know well — five from each quality tier:

| Tier | Score target | Characteristics |
|---|---|---|
| Excellent | 9–10 | Clean refactor, good naming, tested, no issues |
| Good | 7–8 | Minor style issues or missing docs, nothing blocking |
| Needs work | 5–6 | Multiple HIGH findings, messy code, gaps in tests |
| Block | 0–4 | Hardcoded secret, SQL injection, or CRITICAL vuln |

For each PR:
1. Save `diff.patch` and `pr_meta.json` to `tests/fixtures/calibration/{tier}/{repo_pr_number}/`
2. Record your human-assigned tier in `expected.json`

Run the batch runner over all 20 and compute Spearman's ρ between human tier rank
and system score. Target: ρ ≥ 0.75. If lower, the severity weights need tuning.

---

### Step 12 — Score consistency test

**File:** `scripts/eval_consistency.py` (new)

Run the same PR through the pipeline five times and measure score variance.
With `temperature=0.1`, std dev should be < 0.5 across runs.

```python
from src.graph.pr_review_graph import run_pr_review

PR_URL = "https://github.com/your-org/your-repo/pull/1"  # replace with a real PR

scores = []
for i in range(5):
    result = run_pr_review(pr_meta, diff, file_list)
    scores.append(result["overall_score"])
    print(f"  Run {i+1}: {result['overall_score']}")

import statistics
print(f"\n  Mean: {statistics.mean(scores):.2f}")
print(f"  Std dev: {statistics.stdev(scores):.2f}")
assert statistics.stdev(scores) < 0.5, "Score variance too high — check temperature setting"
```

Run this once per model upgrade or temperature change.

---

## Phase 6 — Regression tests in CI

### Step 13 — Add `tests/test_regression.py`

**New file:** `tests/test_regression.py`

Full-pipeline tests with mocked LLM — these should run in CI on every push:

```python
"""
End-to-end regression tests using mocked LLM and moto DynamoDB.
These test the full request → graph → DynamoDB → response flow.
"""
from __future__ import annotations
import json, os
from unittest.mock import MagicMock, patch
import pytest
from moto import mock_aws
import boto3


CRITICAL_FINDING = [{
    "severity": "CRITICAL", "category": "secrets",
    "file": "app.py", "line": 1,
    "title": "Hardcoded secret", "detail": "x", "recommendation": "y",
}]

HIGH_FINDING = [{
    "severity": "HIGH", "category": "injection",
    "file": "db.py", "line": 5,
    "title": "SQL injection risk", "detail": "x", "recommendation": "y",
}]


@pytest.fixture(autouse=True)
def dynamo_env(aws_credentials):
    """Each regression test gets a fresh moto DynamoDB table."""
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
        yield


def make_mock_llm(responses: list[str]):
    """LLM that returns responses in sequence (wraps around)."""
    llm = MagicMock()
    llm.invoke.side_effect = [MagicMock(content=r) for r in responses * 10]
    return llm


class TestFullPipeline:

    def test_critical_finding_sets_should_block(self, full_state):
        from src.graph.pr_review_graph import run_pr_review
        llm = make_mock_llm([json.dumps(CRITICAL_FINDING), "[]"])
        with patch("src.graph.pr_review_graph.get_llm", return_value=llm):
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )
        assert result["should_block"] is True
        assert result["overall_score"] < 10.0

    def test_empty_findings_gives_perfect_score(self, full_state):
        from src.graph.pr_review_graph import run_pr_review
        llm = make_mock_llm(["[]"])
        with patch("src.graph.pr_review_graph.get_llm", return_value=llm):
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )
        assert result["overall_score"] == 10.0
        assert result["should_block"] is False

    def test_lineage_run_written_to_dynamo(self, full_state):
        from src.graph.pr_review_graph import run_pr_review
        from src.tools.dynamo_memory import DynamoMemoryStore
        llm = make_mock_llm(["[]"])
        with patch("src.graph.pr_review_graph.get_llm", return_value=llm):
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )
        thread_id = result["pr_metadata"]["thread_id"]
        store = DynamoMemoryStore(table_name="pr-review-test")
        lr_raw = store.get(thread_id, "lineage_run")
        assert lr_raw is not None
        lr = json.loads(lr_raw)
        assert "final_score" in lr
        assert "total_duration_ms" in lr

    def test_diff_over_50k_chars_does_not_crash(self, full_state):
        from src.graph.pr_review_graph import run_pr_review
        llm = make_mock_llm(["[]"])
        with patch("src.graph.pr_review_graph.get_llm", return_value=llm):
            result = run_pr_review(
                full_state["pr_metadata"],
                "+x = 1\n" * 9000,   # ~63 000 chars
                full_state["file_list"],
            )
        assert "overall_score" in result

    def test_malformed_llm_json_does_not_crash(self, full_state):
        from src.graph.pr_review_graph import run_pr_review
        llm = make_mock_llm(["not valid json at all !!!"])
        with patch("src.graph.pr_review_graph.get_llm", return_value=llm):
            result = run_pr_review(
                full_state["pr_metadata"],
                full_state["diff_content"],
                full_state["file_list"],
            )
        # Should fall back to empty findings — not raise
        assert isinstance(result.get("overall_score"), float)
```

Run: `pytest tests/test_regression.py -v`

Add this to your CI pipeline so it runs on every push.

---

## Phase 7 — Shadow mode improvements

### Step 14 — Enrich the shadow comparison response

**File:** `api.py`, `_compare_findings` function

Add per-agent breakdown and a `confidence` flag:

```python
def _compare_findings(primary: dict, shadow: dict) -> dict:
    # ... existing logic ...
    
    # Per-agent overlap
    def _agent_titles(r, key):
        findings = r.get(key, [])
        return {f.get("title", "").lower().strip()
                for f in findings if f.get("category") != "positive"}

    agent_overlaps = {}
    for key in ("security_findings", "code_quality_findings", "pr_review_findings"):
        pt = _agent_titles(primary, key)
        st = _agent_titles(shadow, key)
        union = pt | st
        both  = pt & st
        agent_overlaps[key] = round(len(both) / max(len(union), 1) * 100, 1)

    return {
        # ... existing fields ...
        "agent_overlaps":    agent_overlaps,
        "low_agreement":     overlap_pct < 50,   # flag for manual review
        "latency_ratio":     round(
            (shadow.get("_duration_ms") or 1) / max(primary.get("_duration_ms") or 1, 1), 2
        ),
    }
```

---

## Phase 8 — Long-term tracking

### Step 15 — Track eval metrics over time

Once you have scores from real production reviews, export the `/eval/metrics` response
weekly and track trends:

- Average score trending up or down?
- Block rate — is it catching more CRITICALs over time?
- Finding precision (per-finding feedback) — are users confirming findings as useful?
- Thumbs ratio (thumbs_up / (thumbs_up + thumbs_down)) — target ≥ 0.75
- p95 latency — does a model upgrade change it significantly?

Store these snapshots in `docs/eval-snapshots/YYYY-MM-DD.json` so you can plot
the trend manually or in a notebook.

---

## Summary checklist

| # | Step | Area | Effort |
|---|---|---|---|
| 1 | Fix conftest DynamoDB GSI | Infrastructure | 30 min |
| 2 | Fix shadow mode race condition | Bug fix | 1–2 h |
| 3 | Add score formula unit tests | Testing | 30 min |
| 4 | Add prescan false-positive tests | Testing | 30 min |
| 5 | Add diff truncation boundary tests | Testing | 30 min |
| 6 | Create golden fixture directory structure | Data | 1 h |
| 7 | Write 15 `diff.patch` + `expected.json` files | Data | 2–3 days |
| 8 | Write `scripts/eval_golden.py` batch runner | Tooling | 3 h |
| 9 | Add per-finding thumbs to UI | Feature | 3 h |
| 10 | Add `/eval/feedback/finding` endpoint | Feature | 1 h |
| 11 | Collect 20 calibration PRs + compute Spearman ρ | Analysis | 1 day |
| 12 | Write `scripts/eval_consistency.py` | Tooling | 1 h |
| 13 | Add `tests/test_regression.py` | Testing | 3 h |
| 14 | Enrich shadow comparison response | Enhancement | 1 h |
| 15 | Set up weekly metrics tracking | Process | ongoing |
