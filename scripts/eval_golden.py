"""
Golden-fixture batch evaluator for the PR review pipeline.

Each fixture lives in tests/fixtures/golden/<scenario_name>/ and contains:
  diff.txt              — raw unified diff
  pr_meta.json          — {"repo":..., "number":..., "title":..., "body":..., "author":..., "base":...}
  expected_findings.json — [{"title_contains": "...", "severity": "CRITICAL"}, ...]

Matching is case-insensitive substring on `title`.
Reports precision/recall per scenario; exits 1 if any recall < 0.8.

Usage:
    python scripts/eval_golden.py              # run all scenarios
    python scripts/eval_golden.py sql_inj      # name-filter (substring match)
    python scripts/eval_golden.py --threshold 0.7   # override recall threshold
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

# Ensure project root is on sys.path when run directly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures" / "golden"
DEFAULT_RECALL_THRESHOLD = 0.8


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


class ExpectedFinding(NamedTuple):
    title_contains: str
    severity: str


class ScenarioResult(NamedTuple):
    name: str
    expected: list[ExpectedFinding]
    matched: list[str]          # title_contains strings that were satisfied
    missed: list[str]           # title_contains strings that were not satisfied
    precision: float            # matched / total_actual_findings (0.0–1.0)
    recall: float               # matched / total_expected (0.0–1.0)
    duration_ms: int
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Fixture loading
# ─────────────────────────────────────────────────────────────────────────────


def load_fixture(fixture_dir: Path) -> tuple[dict, str, list[ExpectedFinding]]:
    """Load pr_meta, diff text, and expected findings from a fixture directory."""
    pr_meta_path = fixture_dir / "pr_meta.json"
    diff_path = fixture_dir / "diff.txt"
    expected_path = fixture_dir / "expected_findings.json"

    for p in (pr_meta_path, diff_path, expected_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing fixture file: {p}")

    pr_meta = json.loads(pr_meta_path.read_text(encoding="utf-8"))
    diff_text = diff_path.read_text(encoding="utf-8")
    raw_expected = json.loads(expected_path.read_text(encoding="utf-8"))

    expected = [
        ExpectedFinding(
            title_contains=e["title_contains"].lower().strip(),
            severity=e.get("severity", "").upper(),
        )
        for e in raw_expected
    ]
    return pr_meta, diff_text, expected


# ─────────────────────────────────────────────────────────────────────────────
# Matching helpers
# ─────────────────────────────────────────────────────────────────────────────


def _collect_all_findings(result: dict) -> list[dict]:
    """Flatten findings from all agent keys in the graph result."""
    all_findings: list[dict] = []
    for key in ("security_findings", "code_quality_findings", "pr_review_findings"):
        val = result.get(key)
        if isinstance(val, list):
            all_findings.extend(val)
    # Also accept top-level "findings" dict (from /review/detail format)
    findings_dict = result.get("findings")
    if isinstance(findings_dict, dict):
        for items in findings_dict.values():
            if isinstance(items, list):
                all_findings.extend(items)
    return all_findings


def _matches_expected(finding: dict, expected: ExpectedFinding) -> bool:
    """Return True if this finding satisfies the expected entry."""
    title = finding.get("title", "").lower()
    sev = finding.get("severity", "").upper()
    title_ok = expected.title_contains in title
    sev_ok = (not expected.severity) or sev == expected.severity
    return title_ok and sev_ok


def evaluate_findings(
    actual_findings: list[dict],
    expected: list[ExpectedFinding],
) -> tuple[list[str], list[str]]:
    """Return (matched_title_contains, missed_title_contains)."""
    matched: list[str] = []
    missed: list[str] = []

    for exp in expected:
        if any(_matches_expected(f, exp) for f in actual_findings):
            matched.append(exp.title_contains)
        else:
            missed.append(exp.title_contains)

    return matched, missed


# ─────────────────────────────────────────────────────────────────────────────
# Fixture runner
# ─────────────────────────────────────────────────────────────────────────────


def run_fixture(name: str, fixture_dir: Path) -> ScenarioResult:
    """Run one golden fixture and return its ScenarioResult."""
    t0 = time.monotonic()

    try:
        pr_meta, diff_text, expected = load_fixture(fixture_dir)
    except Exception as exc:
        return ScenarioResult(
            name=name,
            expected=[],
            matched=[],
            missed=[],
            precision=0.0,
            recall=0.0,
            duration_ms=0,
            error=f"load error: {exc}",
        )

    # Patch get_llm so the test can run without real LLM credentials.
    # If EVAL_GOLDEN_USE_REAL_LLM=1 is set we skip the patch and hit the
    # configured provider directly.
    use_real = os.environ.get("EVAL_GOLDEN_USE_REAL_LLM", "").strip() == "1"

    try:
        if use_real:
            from src.graph.pr_review_graph import run_pr_review
            result = run_pr_review(pr_meta, diff_text, pr_meta.get("files", []))
        else:
            # Import inside try so missing deps surface as an error, not a crash
            from src.graph.pr_review_graph import run_pr_review, get_llm as _get_llm  # noqa: F401
            from src.safety.output_validator import strip_markdown_and_parse
            from unittest.mock import MagicMock

            # Build a passthrough mock that returns empty findings — allows
            # graph topology to be validated without a real LLM.
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content="[]")

            with patch("src.graph.pr_review_graph.get_llm", return_value=mock_llm):
                result = run_pr_review(pr_meta, diff_text, pr_meta.get("files", []))

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return ScenarioResult(
            name=name,
            expected=expected,
            matched=[],
            missed=[e.title_contains for e in expected],
            precision=0.0,
            recall=0.0,
            duration_ms=duration_ms,
            error=f"run error: {exc}",
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    actual_findings = _collect_all_findings(result)

    matched, missed = evaluate_findings(actual_findings, expected)

    total_expected = len(expected)
    total_actual = len(actual_findings)

    recall = len(matched) / total_expected if total_expected else 1.0
    # Precision: of all actual findings, how many satisfy at least one expectation
    # (avoids penalising extra legitimate findings)
    true_positives = sum(
        1 for f in actual_findings
        if any(_matches_expected(f, exp) for exp in expected)
    )
    precision = true_positives / total_actual if total_actual else 1.0

    return ScenarioResult(
        name=name,
        expected=expected,
        matched=matched,
        missed=missed,
        precision=precision,
        recall=recall,
        duration_ms=duration_ms,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

_GREEN = "\033[32m"
_RED   = "\033[31m"
_YELLOW= "\033[33m"
_BOLD  = "\033[1m"
_RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    if sys.stdout.isatty():
        return f"{code}{text}{_RESET}"
    return text


def print_result(r: ScenarioResult, threshold: float) -> None:
    status = _color("PASS", _GREEN) if r.recall >= threshold else _color("FAIL", _RED)
    if r.error:
        status = _color("ERROR", _RED)

    print(f"\n{'─'*60}")
    print(f"  Scenario : {_color(r.name, _BOLD)}")
    print(f"  Status   : {status}")
    print(f"  Duration : {r.duration_ms} ms")

    if r.error:
        print(f"  Error    : {r.error}")
        return

    pct_recall    = f"{r.recall*100:.1f}%"
    pct_precision = f"{r.precision*100:.1f}%"
    recall_color  = _GREEN if r.recall >= threshold else _RED
    print(f"  Recall   : {_color(pct_recall, recall_color)}  (threshold {threshold*100:.0f}%)")
    print(f"  Precision: {pct_precision}")

    if r.matched:
        print(f"  Matched  :")
        for t in r.matched:
            print(f"    {_color('✓', _GREEN)} {t!r}")
    if r.missed:
        print(f"  Missed   :")
        for t in r.missed:
            print(f"    {_color('✗', _RED)} {t!r}")


def print_summary(results: list[ScenarioResult], threshold: float) -> None:
    passed  = sum(1 for r in results if not r.error and r.recall >= threshold)
    errored = sum(1 for r in results if r.error)
    failed  = len(results) - passed - errored

    print(f"\n{'═'*60}")
    print(f"  {_color('SUMMARY', _BOLD)}")
    print(f"  Total scenarios : {len(results)}")
    print(f"  Passed          : {_color(str(passed), _GREEN)}")
    if failed:
        print(f"  Failed          : {_color(str(failed), _RED)}")
    if errored:
        print(f"  Errors          : {_color(str(errored), _YELLOW)}")
    avg_recall = sum(r.recall for r in results if not r.error) / max(
        sum(1 for r in results if not r.error), 1
    )
    print(f"  Avg recall      : {avg_recall*100:.1f}%")
    print(f"{'═'*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run golden-fixture evaluation for the PR review pipeline.",
    )
    parser.add_argument(
        "filter",
        nargs="?",
        default="",
        help="Optional substring filter on scenario name.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_RECALL_THRESHOLD,
        metavar="T",
        help=f"Minimum recall to pass a scenario (default: {DEFAULT_RECALL_THRESHOLD}).",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=FIXTURES_DIR,
        metavar="DIR",
        help="Path to the golden fixtures directory.",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit a JSON report to stdout instead of human-readable output.",
    )
    args = parser.parse_args(argv)

    fixtures_dir: Path = args.fixtures_dir
    if not fixtures_dir.exists():
        print(f"Fixtures directory not found: {fixtures_dir}", file=sys.stderr)
        return 2

    scenario_dirs = sorted(
        d for d in fixtures_dir.iterdir()
        if d.is_dir() and (not args.filter or args.filter.lower() in d.name.lower())
    )

    if not scenario_dirs:
        print(
            f"No scenarios found in {fixtures_dir}"
            + (f" matching {args.filter!r}" if args.filter else ""),
            file=sys.stderr,
        )
        return 2

    results: list[ScenarioResult] = []
    for scenario_dir in scenario_dirs:
        if not args.output_json:
            print(f"Running: {scenario_dir.name} …", end=" ", flush=True)
        r = run_fixture(scenario_dir.name, scenario_dir)
        results.append(r)
        if not args.output_json:
            tag = "OK" if (not r.error and r.recall >= args.threshold) else "FAIL"
            print(tag)

    if args.output_json:
        report = [
            {
                "name": r.name,
                "recall": round(r.recall, 4),
                "precision": round(r.precision, 4),
                "duration_ms": r.duration_ms,
                "matched": r.matched,
                "missed": r.missed,
                "error": r.error,
                "pass": not r.error and r.recall >= args.threshold,
            }
            for r in results
        ]
        print(json.dumps(report, indent=2))
    else:
        for r in results:
            print_result(r, args.threshold)
        print_summary(results, args.threshold)

    any_fail = any(r.error or r.recall < args.threshold for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
