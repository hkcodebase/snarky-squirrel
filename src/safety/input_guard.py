"""
Input validation and sanitisation for untrusted PR data.

Before any PR content reaches an LLM prompt, it passes through this module:

  validate_diff()              — reject empty / whitespace-only diffs
  sanitize_pr_title()          — strip null bytes, truncate
  sanitize_pr_body()           — strip null bytes, truncate
  build_secure_human_message() — construct a prompt string with all untrusted
                                 fields wrapped in XML delimiters, preventing
                                 prompt injection from malicious PR metadata.

Prompt injection defence — why XML tags?
  LLMs trained on instruction-following treat XML tags as structural separators.
  Wrapping every untrusted field in a unique tag (<pr_title>…</pr_title>) makes
  it much harder for injected text to "escape" into the system-prompt context.
  We also html-escape < and > inside the values themselves so an attacker cannot
  close the wrapping tag and open a new structural element.
"""

from __future__ import annotations

import html
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────── exceptions ──────────────────────────────────────


class DiffTooShortError(ValueError):
    """Raised when a diff contains no substantive added lines."""


# ─────────────────────────── sanitisers ──────────────────────────────────────


def sanitize_pr_title(title: str | None, max_len: int = 500) -> str:
    """Strip null bytes and truncate to max_len characters.

    Returns an empty string if title is None or empty.
    """
    if not title:
        return ""
    cleaned = title.replace("\x00", "")
    if len(cleaned) > max_len:
        logger.debug("input_guard: truncated PR title from %d to %d chars", len(cleaned), max_len)
        cleaned = cleaned[:max_len]
    return cleaned


def sanitize_pr_body(body: str | None, max_len: int = 3000) -> str:
    """Strip null bytes and truncate to max_len characters.

    Returns an empty string if body is None or empty.
    """
    if not body:
        return ""
    cleaned = body.replace("\x00", "")
    if len(cleaned) > max_len:
        logger.debug("input_guard: truncated PR body from %d to %d chars", len(cleaned), max_len)
        cleaned = cleaned[:max_len]
    return cleaned


# ─────────────────────────── diff validation ─────────────────────────────────


def validate_diff(diff: str, min_added_lines: int = 1) -> str:
    """Verify that a diff contains at least min_added_lines substantive added lines.

    An "added line" is a line that:
      - Starts with "+" (unified diff format)
      - Is NOT a file header ("+++" prefix)
      - Contains at least one non-whitespace character

    Raises:
        DiffTooShortError: if the diff is empty, whitespace-only, or has no
                           substantive added lines.

    Returns:
        The original diff string unchanged (allows use in an inline guard).
    """
    if not diff or not diff.strip():
        raise DiffTooShortError("Diff is empty or contains only whitespace.")

    added = sum(
        1
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    )

    if added < min_added_lines:
        raise DiffTooShortError(
            f"Diff contains no added lines with content "
            f"(found {added}, need ≥ {min_added_lines})."
        )

    return diff


# ─────────────────────────── prompt builder ──────────────────────────────────


def wrap_untrusted(label: str, content: str) -> str:
    """Wrap content in XML tags with inner HTML-escaping.

    The HTML-escaping of < and > inside content prevents an attacker from
    closing the wrapping tag and injecting new XML structure into the prompt.

    Example:
        wrap_untrusted("pr_title", "Fix <bug>")
        → "<pr_title>Fix &lt;bug&gt;</pr_title>"
    """
    escaped = html.escape(content, quote=False)
    return f"<{label}>{escaped}</{label}>"


def build_secure_human_message(
    pr_meta: dict,
    diff_snippet: str,
    extra_fields: dict | None = None,
) -> str:
    """Build the HumanMessage content string for any specialist agent.

    Every piece of untrusted user-controlled content (PR title, body, author,
    diff) is wrapped in XML delimiters.  This makes it structurally distinct
    from the surrounding system prompt and reduces the effectiveness of prompt
    injection payloads embedded in PR metadata.

    Args:
        pr_meta:      PR metadata dict (title, body, author, base, repo, number).
        diff_snippet: The diff content to include (caller is responsible for
                      truncating to the agent's token budget before passing).
        extra_fields: Additional key → value pairs to include as XML-wrapped
                      fields (e.g. files_changed, prior_agent_findings).
                      Values are sanitized with sanitize_pr_title() before
                      wrapping (null bytes stripped, truncated to 1000 chars).

    Returns:
        A multi-line string ready to pass as the content of a HumanMessage.
    """
    parts: list[str] = []

    # Core PR metadata
    parts.append(wrap_untrusted("pr_title",  sanitize_pr_title(pr_meta.get("title", ""))))
    parts.append(wrap_untrusted("pr_body",   sanitize_pr_body(pr_meta.get("body", "") or "")))
    parts.append(wrap_untrusted("pr_author", sanitize_pr_title(pr_meta.get("author", "unknown"))))
    parts.append(wrap_untrusted("base_branch", sanitize_pr_title(pr_meta.get("base", "main"))))

    # Optional caller-supplied fields (e.g. files_changed, prior findings)
    if extra_fields:
        for label, value in extra_fields.items():
            parts.append(wrap_untrusted(label, sanitize_pr_title(str(value), max_len=1000)))

    # Diff — wrapped separately so the agent knows exactly where it starts/ends
    safe_diff = html.escape(diff_snippet, quote=False)
    parts.append(f"<diff>\n```\n{safe_diff}\n```\n</diff>")

    return "\n".join(parts)
