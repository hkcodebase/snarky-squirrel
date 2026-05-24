"""GitHub API client utilities for the PR Review System."""
from __future__ import annotations

import hashlib
import hmac
import re

import requests

GITHUB_API = "https://api.github.com"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def parse_pr_url(pr_url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into (owner/repo, pr_number)."""
    match = re.match(r"https://github\.com/(.+?)/(.+?)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url!r}")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def validate_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """Validate a GitHub webhook HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def fetch_pr_metadata(repo: str, pr_number: int, token: str) -> dict:
    """Return PR metadata dict."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    resp = requests.get(url, headers=_github_headers(token), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "repo": repo,
        "number": pr_number,
        "sha": data["head"]["sha"],
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "author": data["user"]["login"],
        "base": data["base"]["ref"],
        "html_url": data.get("html_url", ""),
    }


def fetch_pr_diff(repo: str, pr_number: int, token: str) -> str:
    """Fetch the unified diff for a PR."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    resp = requests.get(
        url,
        headers={**_github_headers(token), "Accept": "application/vnd.github.diff"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def fetch_pr_files(repo: str, pr_number: int, token: str) -> list[str]:
    """Return a list of filenames changed in the PR."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    resp = requests.get(url, headers=_github_headers(token), timeout=30)
    resp.raise_for_status()
    return [f["filename"] for f in resp.json()]


def post_pr_comment(repo: str, pr_number: int, body: str, token: str) -> None:
    """Post (or update) the AI review comment on the PR."""
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    existing = requests.get(url, headers=_github_headers(token), timeout=30).json()
    bot_comment_id = next(
        (c["id"] for c in existing
         if c["user"]["login"] == "github-actions[bot]" and "🤖 AI PR Review" in c["body"]),
        None,
    )
    if bot_comment_id:
        requests.patch(
            f"{GITHUB_API}/repos/{repo}/issues/comments/{bot_comment_id}",
            headers=_github_headers(token),
            json={"body": body},
            timeout=30,
        ).raise_for_status()
    else:
        requests.post(
            url, headers=_github_headers(token), json={"body": body}, timeout=30
        ).raise_for_status()


def set_commit_status(
    repo: str, sha: str, state: str, description: str, token: str
) -> None:
    """Set a GitHub commit status check (success | failure | pending | error)."""
    requests.post(
        f"{GITHUB_API}/repos/{repo}/statuses/{sha}",
        headers=_github_headers(token),
        json={
            "state": state,
            "description": description[:140],
            "context": "AI PR Review",
            "target_url": f"https://github.com/{repo}",
        },
        timeout=30,
    ).raise_for_status()


def request_changes(repo: str, pr_number: int, body: str, token: str) -> None:
    """Request changes on the PR (blocks merge)."""
    requests.post(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
        headers=_github_headers(token),
        json={"event": "REQUEST_CHANGES", "body": body},
        timeout=30,
    ).raise_for_status()
