"""
LLM output validation and normalisation.

Every agent in the pipeline asks an LLM to return a JSON array of finding objects.
This module provides:

  strip_markdown_and_parse()  — centralised fence-strip + json.loads with safe fallback
  normalize_severity()        — map unknown/typo'd severities to known values
  normalize_finding()         — fill in missing required fields with safe defaults
  validate_findings()         — validate + normalise a list of raw finding dicts

Usage in any agent:

    from src.safety.output_validator import strip_markdown_and_parse, validate_findings
    findings = validate_findings(
        strip_markdown_and_parse(response.content),
        agent_name="security",
    )
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ─────────────────────────── constants ───────────────────────────────────────

VALID_SEVERITIES: frozenset[str] = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"})

# Common LLM typos / alternate names mapped to canonical values.
SEVERITY_ALIASES: dict[str, str] = {
    "CRITCAL":   "CRITICAL",
    "CRITICIAL": "CRITICAL",
    "CRIT":      "CRITICAL",
    "BLOCKER":   "CRITICAL",
    "BLOCKING":  "CRITICAL",
    "ERROR":     "HIGH",
    "ERR":       "HIGH",
    "MAJOR":     "HIGH",
    "WARNING":   "MEDIUM",
    "WARN":      "MEDIUM",
    "MINOR":     "LOW",
    "NOTE":      "INFO",
    "NOTICE":    "INFO",
    "SUGGESTION":"INFO",
}

# Default cap applied by validate_findings().
# Large finding lists slow down the dedup LLM call and produce noisy GitHub comments.
MAX_FINDINGS_PER_AGENT: int = 50


# ─────────────────────────── public helpers ───────────────────────────────────


def normalize_severity(raw: str) -> str:
    """Normalise a severity string to one of VALID_SEVERITIES.

    Steps:
      1. Strip whitespace and uppercase.
      2. Check VALID_SEVERITIES — return as-is if already valid.
      3. Check SEVERITY_ALIASES for known typos.
      4. Fall back to "LOW" — never drops a finding, but logs a warning.
    """
    if not raw:
        return "LOW"
    s = raw.strip().upper()
    if s in VALID_SEVERITIES:
        return s
    if s in SEVERITY_ALIASES:
        return SEVERITY_ALIASES[s]
    logger.warning("output_validator: unknown severity %r — normalised to LOW", raw)
    return "LOW"


def normalize_finding(raw: dict) -> dict:
    """Return a new dict with all required fields present.

    Never mutates the input dict. Missing fields receive sane defaults so that
    downstream code can safely call `.get("severity")`, `.get("title")`, etc.
    without hitting None.
    """
    severity = normalize_severity(raw.get("severity", ""))
    # Annotate normalisation in the title so reviewers can see it happened.
    title = raw.get("title") or "[untitled finding]"
    if severity != raw.get("severity", "").strip().upper() and raw.get("severity"):
        title = f"{title} [severity normalised from: {raw['severity']}]"

    return {
        "severity":       severity,
        "category":       raw.get("category") or "other",
        "file":           raw.get("file")     or "unknown",
        "line":           raw.get("line"),          # None is valid
        "title":          title,
        "detail":         raw.get("detail")         or "",
        "recommendation": raw.get("recommendation") or "",
        "code_suggestion":raw.get("code_suggestion") or "",
        # Pass through internal metadata fields without validation.
        **{k: v for k, v in raw.items()
           if k in ("_source", "html_url", "pr_url")},
    }


def validate_findings(
    raw_findings: list,
    agent_name: str = "",
    cap: int = MAX_FINDINGS_PER_AGENT,
) -> list[dict]:
    """Validate and normalise a raw findings list from an LLM response.

    Steps:
      1. Drop entries that are not dicts (LLM sometimes returns strings or null).
      2. Normalise each remaining finding via normalize_finding().
      3. Cap the list at `cap` entries (sorted by severity so CRITICAL items
         are kept first when the cap is applied).
      4. Log a warning if anything was dropped or capped.

    Returns a list of clean finding dicts.
    """
    prefix = f"[{agent_name}] " if agent_name else ""

    # Drop non-dicts
    dicts = [f for f in raw_findings if isinstance(f, dict)]
    dropped = len(raw_findings) - len(dicts)
    if dropped:
        logger.warning("%soutput_validator: dropped %d non-dict entries", prefix, dropped)

    # Normalise
    normalised = [normalize_finding(f) for f in dicts]

    # Sort by severity weight so the most important findings survive a cap
    _weight = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    normalised.sort(key=lambda f: _weight.get(f["severity"], 5))

    # Cap
    if len(normalised) > cap:
        logger.warning(
            "%soutput_validator: capped %d findings to %d",
            prefix, len(normalised), cap,
        )
        normalised = normalised[:cap]

    return normalised


def strip_markdown_and_parse(content: str) -> list:
    """Strip markdown code fences from an LLM response and parse as JSON.

    Handles the three common LLM response styles:
      - Raw JSON array: [{"severity": ...}]
      - Fenced with language tag: ```json\\n[...]\\n```
      - Fenced without language tag: ```\\n[...]\\n```

    Returns a list (possibly empty) on any parse error — never raises.
    This centralises the copy-pasted 6-line try/except block that was
    duplicated identically in every agent.
    """
    if not content:
        return []
    try:
        raw = content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        logger.debug("output_validator: JSON parse failed: %s", exc)
        return []
