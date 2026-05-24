#!/usr/bin/env python3
"""
CLI entry point for the PR Review System (v2 — local development).

Usage:
  # Review a live GitHub PR
  python main.py --pr-url https://github.com/org/repo/pull/42

  # Review a local diff file (no GitHub token needed)
  python main.py --diff tests/fixtures/sample.diff

  # Save result to JSON
  python main.py --pr-url https://github.com/org/repo/pull/42 --output result.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from lambda_handler import fetch_pr_diff, fetch_pr_files, fetch_pr_metadata, parse_pr_url
from src.graph.pr_review_graph import run_pr_review


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR Review System — local runner (v2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr-url", metavar="URL", help="GitHub PR URL")
    group.add_argument("--diff", metavar="FILE", help="Path to a local .diff file")
    parser.add_argument("--output", metavar="FILE", help="Save JSON result to this file")
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN", "")

    if args.pr_url:
        if not github_token:
            print("Error: GITHUB_TOKEN not set. Set it in .env or the environment.")
            sys.exit(1)
        repo, pr_number = parse_pr_url(args.pr_url)
        print(f"Fetching PR #{pr_number} from {repo} ...")
        pr_meta = fetch_pr_metadata(repo, pr_number, github_token)
        diff_content = fetch_pr_diff(repo, pr_number, github_token)
        file_list = fetch_pr_files(repo, pr_number, github_token)
    else:
        diff_path = args.diff
        if not os.path.exists(diff_path):
            print(f"Error: diff file not found: {diff_path}")
            sys.exit(1)
        diff_content = open(diff_path).read()
        pr_meta = {
            "repo": "local/test",
            "number": 0,
            "sha": "local",
            "title": f"Local review — {os.path.basename(diff_path)}",
            "body": "",
            "author": "dev",
            "base": "main",
        }
        file_list = []

    provider = os.environ.get("LLM_PROVIDER", "ollama")
    model = os.environ.get("LLM_MODEL", "gemma4:4b")
    dynamo = os.environ.get("DYNAMODB_ENDPOINT", "real-aws")
    print(f"LLM: {provider}/{model}  |  DynamoDB: {dynamo}")
    print("Running multi-agent PR review...")

    result = run_pr_review(pr_meta, diff_content, file_list)

    divider = "=" * 60
    print(f"\n{divider}")
    print(f"  Score:  {result['overall_score']}/10")
    print(f"  Block:  {result['should_block']}")
    print(f"{divider}\n")
    print(result["summary_report"])

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nResult saved to {args.output}")


if __name__ == "__main__":
    main()
