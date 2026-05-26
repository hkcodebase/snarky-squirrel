"""
Security Agent — scans the PR diff for:
  - Hardcoded API keys, tokens, passwords, credentials
  - Dangerous functions (eval, exec, deserialization)
  - Known vulnerable patterns (SQL injection, path traversal, SSRF)
  - Secrets in config / env files committed by mistake

Findings are written to shared DynamoDB memory so other agents can reference them.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from src.tools.dynamo_memory import DynamoMemoryStore
from src.safety.input_guard import build_secure_human_message
from src.safety.output_validator import (
    normalize_severity,
    strip_markdown_and_parse,
    validate_findings,
)

SYSTEM_PROMPT = """You are a senior application security engineer performing a PR security review.
Analyse the provided unified diff carefully.

Focus on:
1. Hardcoded secrets — API keys, tokens, passwords, private keys, connection strings
2. Injection vulnerabilities — SQL, command, LDAP, XPath injection
3. Dangerous function usage — eval(), exec(), pickle.loads(), yaml.load() without Loader
4. Insecure cryptography — MD5/SHA1 for passwords, ECB mode, hardcoded IVs
5. Path traversal, SSRF, open redirect patterns
6. PII in logs or error messages
7. Missing authentication / authorisation checks on new endpoints

For EVERY finding output a JSON object in this exact schema:
{
  "severity": "CRITICAL|HIGH|MEDIUM|LOW|INFO",
  "category": "secrets|injection|dangerous_function|crypto|path_traversal|auth|pii|other",
  "file": "<filename>",
  "line": <line_number_or_null>,
  "title": "<short title>",
  "detail": "<explain the risk in 1-2 sentences>",
  "recommendation": "<concrete fix in 1-2 sentences>"
}

Return a JSON array of findings. If no issues found return [].
Do NOT include any text outside the JSON array.
"""

# Regex pre-scan: fast pattern matching before sending to LLM (reduces cost + latency)
#
# Pattern design principles:
#   - Require a quoted string literal on the RHS so that env-var reads like
#     os.environ.get("API_KEY") are NOT flagged (the value is not a literal).
#   - DB connection strings are a special case — the credentials appear in the
#     URL itself, not in an assignment, so no quote requirement needed.
SECRET_PATTERNS: list[tuple[str, str]] = [
    # API / access keys — require a quoted literal value (rejects os.environ.get())
    (r"(?i)(api[_-]?key|apikey)\s*=\s*['\"][A-Za-z0-9_\-]{20,}['\"]", "API key"),
    (r"(?i)aws_access_key_id\s*=\s*['\"]([A-Z0-9]{20})['\"]", "AWS access key"),
    (r"(?i)aws_secret_access_key\s*=\s*['\"]([A-Za-z0-9+/]{40})['\"]", "AWS secret key"),
    # Passwords — matches DB_PASSWORD, db_pass, PASSWORD, passwd, pwd etc.
    (
        r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]",
        "Hardcoded password",
    ),
    # Secrets / signing keys
    (r"(?i)(secret[_-]?key|secret)\s*[:=]\s*['\"]([^'\"]{10,})['\"]", "Secret key"),
    (r"(?i)(private[_-]?key|rsa[_-]?key)\s*[:=]\s*['\"]?-----BEGIN", "Private key"),
    # Tokens
    (r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer token"),
    (
        r"(?i)(token|auth[_-]?token)\s*[:=]\s*['\"]([A-Za-z0-9_\-]{20,})['\"]",
        "Auth token",
    ),
    # Database connection strings with embedded credentials
    (r"postgres(?:ql)?://[^:]+:[^@]+@", "DB connection string with credentials"),
    (r"mysql://[^:]+:[^@]+@", "MySQL connection string with credentials"),
    (r"redis://:[^@]+@", "Redis connection string with credentials"),
    # Stripe / payment keys
    (r"(?i)(sk_live_|pk_live_)[A-Za-z0-9]{20,}", "Stripe live key"),
]

# Prefixes that indicate a line is a comment — skip these in the prescan.
_COMMENT_PREFIXES = ("#", "//", "--", "/*", "*")


def regex_prescan(diff: str) -> list[dict]:
    """Fast regex prescan to flag obvious secrets before LLM analysis.

    Only inspects added lines (starting with '+').
    Skips comment lines (Python/JS/SQL/C-style) to reduce false positives on
    documentation examples and commented-out code.
    """
    findings = []
    for line_num, line in enumerate(diff.splitlines(), start=1):
        if not line.startswith("+"):  # only added lines
            continue
        # Skip comment lines — strip the leading '+' before checking.
        stripped = line[1:].lstrip()
        if stripped.startswith(_COMMENT_PREFIXES):
            continue
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, line):
                findings.append(
                    {
                        "severity": "CRITICAL",
                        "category": "secrets",
                        "file": "unknown",
                        "line": line_num,
                        "title": f"Potential {label} detected",
                        "detail": f"Pattern matched in added line: {line.strip()[:120]}",
                        "recommendation": "Move to environment variable or secret manager (AWS Secrets Manager). Never commit credentials.",
                        "_source": "regex",
                    }
                )
    return findings


class SecurityAgent:
    def __init__(self, llm, memory_store: DynamoMemoryStore):
        self.llm = llm
        self.memory = memory_store

    def run(self, state: dict) -> dict:
        import json

        started_at = datetime.now(tz=timezone.utc).isoformat()
        t0 = time.monotonic()

        diff = state["diff_content"]
        pr_meta = state["pr_metadata"]
        thread_id = pr_meta.get("thread_id") or f"pr-{pr_meta['repo']}-{pr_meta['number']}"

        # Step 1: fast regex prescan
        regex_findings = regex_prescan(diff)

        # Step 2: LLM deep analysis
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=build_secure_human_message(pr_meta, diff[:12000])),
        ]
        response = self.llm.invoke(messages)

        llm_findings = validate_findings(
            strip_markdown_and_parse(response.content),
            agent_name="security",
        )

        all_findings = regex_findings + llm_findings
        has_critical = any(
            normalize_severity(f.get("severity", "")) == "CRITICAL"
            for f in all_findings
        )

        # Write findings to shared memory so other agents can reference
        self.memory.put(
            thread_id=thread_id,
            key="security_findings",
            value=json.dumps(all_findings),
        )

        # Record lineage
        sev_counts = {s: sum(1 for f in all_findings if f.get("severity") == s)
                      for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}
        self.memory.put(thread_id, "lineage_security", json.dumps({
            "agent": "security",
            "started_at": started_at,
            "completed_at": datetime.now(tz=timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "reads_from": [],
            "writes_to": ["security_findings"],
            "input_summary": {
                "diff_chars": len(diff),
                "regex_hits": len(regex_findings),
            },
            "output_summary": {"total_findings": len(all_findings), **sev_counts},
        }))

        completed = list(state.get("completed_agents", []))
        if "security" not in completed:
            completed.append("security")

        return {
            "security_findings": all_findings,
            "should_block": has_critical,
            "completed_agents": completed,
        }
