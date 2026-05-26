# Eval & Safety Layer — Test Steps

Work through these one by one. Each step is self-contained. Tick off as you go.

---

## 1 — Install test dependencies

```bash
pip install pytest moto boto3
```

Confirm versions:
```bash
pytest --version
python -c "import moto; print(moto.__version__)"
```

---

## 2 — Run the output-validator unit tests

These are pure logic tests — no network, no LLM.

```bash
pytest tests/test_agents.py::TestOutputValidator -v
```

Expected: **9 tests pass**

What they cover:
- Unknown severity `"CATASTROPHIC"` → normalised to `"LOW"`
- Severity typo `"CRITCAL"` → normalised to `"CRITICAL"`
- Missing `title` field → filled with `"[untitled finding]"`
- 60 findings capped to 50, highest severity kept first
- Non-dict entries (strings, nulls) dropped silently

---

## 3 — Run the input-guard unit tests

```bash
pytest tests/test_agents.py::TestInputGuard -v
```

Expected: **8 tests pass**

What they cover:
- Empty diff → `DiffTooShortError`
- Whitespace-only diff → `DiffTooShortError`
- `--- / +++` header lines only (no added lines) → `DiffTooShortError`
- Null bytes stripped from title and body
- XML injection attempt in title → `html.escape()` neutralises it
- `build_secure_human_message` wraps each field in XML tags

---

## 4 — Run the score-formula unit tests

```bash
pytest tests/test_agents.py::TestScoreFormula -v
```

Expected: **9 parameterised cases pass**

Key cases to verify manually:
| Findings | Expected score |
|---|---|
| 1 × HIGH | 8.5 |
| 1 × CRITICAL | 7.0 |
| 1 × MEDIUM + 1 × LOW | 9.4 |
| 4 × CRITICAL | 0.0 (clamped) |
| 1 × CRITICAL category=positive | 10.0 (no deduction) |

---

## 5 — Run the prescan false-positive tests

```bash
pytest tests/test_agents.py::TestRegexPrescanFalsePositives -v
```

Expected: **13 parameterised cases pass**

Key cases:
| Input line | Should flag? |
|---|---|
| `+api_key = "sk_live_realkey1234567890"` | Yes |
| `+api_key = os.environ.get("API_KEY")` | **No** |
| `# api_key = "sk_live_..."` (comment) | **No** |
| `+config.get("api-key")` | **No** |

---

## 6 — Run all agent unit tests

```bash
pytest tests/test_agents.py -v
```

Expected: all tests pass. Note the count — it should include the four new test classes added in this sprint.

---

## 7 — Run the integration regression tests

These use `moto` (mocked AWS) and a mocked LLM — no real credentials needed.

```bash
pytest tests/test_regression.py -v -m integration
```

Expected: **9 tests pass**

| Test | What it checks |
|---|---|
| `test_graph_completes_with_all_required_keys` | All four agents run; state has `overall_score`, `should_block`, `summary_report`, `score_breakdown` |
| `test_critical_finding_sets_should_block` | CRITICAL finding → `should_block=True`, score < 10 |
| `test_empty_findings_gives_score_10` | Empty findings → `overall_score == 10.0`, `should_block=False` |
| `test_lineage_run_written_to_dynamodb` | `lineage_run` key written to DynamoDB after graph run |
| `test_large_diff_does_not_crash` | 63k-char diff completes without raising |
| `test_malformed_llm_json_does_not_crash` | Invalid JSON from LLM → falls back to `[]`, returns valid score |
| `test_explicit_llm_param_used` | `run_pr_review(llm=...)` skips `get_llm()` entirely |
| `test_query_by_type_returns_written_items` | GSI query returns both written items |
| `test_query_all_by_type_paginates` | Pagination over 5 items returns all 5 |

---

## 8 — Run the golden fixture eval runner

This runs the `hardcoded_secrets` scenario through the real pipeline with a mocked LLM.

```bash
python scripts/eval_golden.py
```

Expected output:
```
Running: hardcoded_secrets … OK

────────────────────────────────────────────────────────────
  Scenario : hardcoded_secrets
  Status   : PASS
  Duration : ... ms
  Recall   : 100.0%  (threshold 80%)
  Precision: ...
```

> **Note:** With the default mocked LLM (`"[]"`) the recall will be 0% because no findings are returned. To test with a real LLM set `EVAL_GOLDEN_USE_REAL_LLM=1`:
> ```bash
> EVAL_GOLDEN_USE_REAL_LLM=1 python scripts/eval_golden.py
> ```
> Expected recall ≥ 80% — the LLM should catch the hardcoded password and JWT secret.

Run with name filter:
```bash
python scripts/eval_golden.py hardcoded
```

Run and get JSON output:
```bash
python scripts/eval_golden.py --json
```

---

## 9 — Run the full test suite

```bash
pytest tests/ -v
```

All tests (unit + integration) should be green before any deployment.

---

## 10 — Manual: verify rate limiting

Start the API locally:
```bash
uvicorn api:app --reload --port 8000
```

Use a loop to fire 6 review requests in under 60 seconds (requires a valid session cookie):
```bash
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST http://localhost:8000/review \
    -H "Cookie: pr_reviewer_session=<your_session_token>" \
    -H "Content-Type: application/json" \
    -d '{"repo":"acme/backend","pr_number":1}'
done
```

Expected: first 5 return `200`, 6th returns `429 Too Many Requests`.

Check the response header on the 429:
```bash
curl -i -X POST http://localhost:8000/review ... # 6th request
```
Should include: `Retry-After: 60`

---

## 11 — Manual: verify `/db/records` admin guard

As a non-admin user:
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:8000/db/records \
  -H "Cookie: pr_reviewer_session=<non_admin_token>"
```
Expected: `403`

As an admin user (email in `ADMIN_EMAILS` env var):
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:8000/db/records \
  -H "Cookie: pr_reviewer_session=<admin_token>"
```
Expected: `200`

---

## 12 — Manual: verify per-finding feedback

Submit a thumbs-up for a finding (no auth required — endpoint is in `_AUTH_PUBLIC`):
```bash
curl -s -X POST http://localhost:8000/eval/finding-feedback \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "pr-acme-backend-1",
    "finding_hash": "a1b2c3d4",
    "thumbs": "up",
    "agent": "security",
    "notes": "Caught a real secret"
  }'
```
Expected: `{"ok": true}`

Test invalid hash format (not 8 hex chars):
```bash
curl -s -X POST http://localhost:8000/eval/finding-feedback \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"t1","finding_hash":"ZZZZZZZZ","thumbs":"up"}'
```
Expected: `422` or `400 Invalid finding_hash`

---

## 13 — Manual: verify shadow mode fix

Open two browser tabs. In each, trigger a shadow evaluation at the same time using
two different models (e.g. `gemma4:4b` vs `llama3:8b`).

Check the response in each tab — both should report their own correct `shadow_config.provider`
and `shadow_config.model`. Before the fix these would swap or collide under concurrent load.

To verify from the CLI, run two shadow requests simultaneously:
```bash
curl -s -X POST http://localhost:8000/eval/run \
  -H "Cookie: ..." -d '{"thread_id":"t1","mode":"shadow","shadow_provider":"ollama","shadow_model":"llama3:8b"}' &

curl -s -X POST http://localhost:8000/eval/run \
  -H "Cookie: ..." -d '{"thread_id":"t2","mode":"shadow","shadow_provider":"ollama","shadow_model":"gemma4:4b"}' &

wait
```

Both responses should show the model that was requested, not each other's.

---

## 14 — Manual: verify per-finding thumbs in the UI

1. Open the app and run a review that produces at least one finding.
2. Click into the review detail modal.
3. Scroll to the findings section — each finding card should show `👍 👎` buttons.
4. Click `👍` on one finding — the button should highlight and become unclickable.
5. Click `👎` on a different finding — same behaviour.
6. In DynamoDB (or via the `/db/records` admin page), confirm a `finding_feedback_<hash>` record was written.

---

## 15 — Check the eval-plan for remaining work

```bash
cat docs/eval-plan.md
```

Steps that are now complete (done in this sprint):
- Step 1: conftest GSI fix ✓
- Step 2: shadow mode race fix ✓
- Step 3: score formula tests ✓
- Step 4: prescan false-positive tests ✓
- Step 8: `scripts/eval_golden.py` batch runner ✓
- Step 9: per-finding thumbs in UI ✓
- Step 10: `/eval/finding-feedback` endpoint ✓
- Step 13: `tests/test_regression.py` ✓
- Step 14: enriched shadow comparison ✓

Still to do from the eval-plan:
- Step 5: diff truncation boundary tests
- Step 6–7: golden fixture directory with all 15 scenarios
- Step 11: collect 20 calibration PRs + Spearman ρ
- Step 12: `scripts/eval_consistency.py` consistency runner
- Step 15: weekly metrics snapshots to `docs/eval-snapshots/`
