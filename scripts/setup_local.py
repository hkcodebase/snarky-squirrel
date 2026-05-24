#!/usr/bin/env python3
"""
One-time local environment initialiser.

Run this after starting infrastructure with docker-compose.

Usage:
  python3 scripts/setup_local.py                              # reads LLM_PROVIDER from .env
  python3 scripts/setup_local.py --provider docker-model      # explicit override (recommended)
  python3 scripts/setup_local.py --provider ollama --model gemma4:12b

Providers:
  ollama        Ollama container (docker-compose --profile ollama up -d)
  docker-model  Docker Desktop Model Runner — no extra container needed
  bedrock       AWS Bedrock — no local model setup, just verifies DynamoDB
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────── helpers ─────────────────────────────────────────


def wait_for(url: str, label: str, retries: int = 30, delay: float = 2.0) -> bool:
    for attempt in range(1, retries + 1):
        try:
            requests.get(url, timeout=3)
            print(f"  ✓ {label} is ready")
            return True
        except Exception:
            print(f"  Waiting for {label} ({attempt}/{retries})...", end="\r")
            time.sleep(delay)
    print(f"\n  ✗ {label} not available after {retries} attempts")
    return False


# ─────────────────────────── DynamoDB ────────────────────────────────────────


def setup_dynamodb() -> None:
    import boto3
    from botocore.exceptions import ClientError

    endpoint = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000")
    table = os.environ.get("DYNAMODB_TABLE", "pr-review-local-memory")

    print(f"\n[DynamoDB] endpoint={endpoint}  table={table}")
    if not wait_for(endpoint, "DynamoDB Local"):
        print("  Tip: start DynamoDB with:  docker-compose up -d")
        sys.exit(1)

    client = boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )

    try:
        client.create_table(
            TableName=table,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"  ✓ Table '{table}' created")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"  ✓ Table '{table}' already exists")
        else:
            raise


# ─────────────────────────── Ollama ──────────────────────────────────────────


def setup_ollama(model: str) -> None:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    print(f"\n[Ollama] base_url={base_url}  model={model}")

    if not wait_for(f"{base_url}/api/tags", "Ollama"):
        print("  Tip: start Ollama with:  docker-compose --profile ollama up -d")
        sys.exit(1)

    # Check if model is already pulled
    try:
        tags = requests.get(f"{base_url}/api/tags", timeout=5).json()
        local_models = [m["name"] for m in tags.get("models", [])]
        if any(m.startswith(model.split(":")[0]) for m in local_models):
            print(f"  ✓ Model '{model}' already present (available: {local_models})")
            return
    except Exception:
        pass

    print(f"  Pulling '{model}' — this may take several minutes on first run ...")
    try:
        resp = requests.post(
            f"{base_url}/api/pull",
            json={"name": model},
            stream=True,
            timeout=600,
        )
        for line in resp.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            status = data.get("status", "")
            total = data.get("total", 0)
            completed = data.get("completed", 0)
            if total:
                pct = int(100 * completed / total)
                print(f"  {status}: {pct}%   ", end="\r")
            else:
                print(f"  {status}   ", end="\r")
        print(f"\n  ✓ Model '{model}' ready")
    except Exception as exc:
        print(f"\n  ✗ Pull failed: {exc}")
        print(f"    Run manually:  docker exec pr-review-ollama ollama pull {model}")
        sys.exit(1)


# ─────────────────────────── Docker Model Runner ─────────────────────────────


def setup_docker_model(model: str) -> None:
    endpoint = os.environ.get(
        "DOCKER_MODEL_ENDPOINT",
        "http://localhost:12434/engines/llama.cpp/v1",
    )
    print(f"\n[Docker Model Runner] endpoint={endpoint}  model={model}")
    print("  No extra container needed — model runs inside Docker Desktop.")

    # Retry a few times: Docker Desktop Model Runner can take a moment to respond
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(f"{endpoint}/models", timeout=5)
            resp.raise_for_status()
            available = [m["id"] for m in resp.json().get("data", [])]
            if model in available or any(model in m for m in available):
                print(f"  ✓ Model '{model}' is available")
                return
            # Reachable but model not found — no point retrying
            print(f"  ✗ Model '{model}' not found in Docker Model Runner.")
            print(f"    Available: {available or 'none'}")
            print(f"    Pull it with:  docker model pull {model}")
            sys.exit(1)
        except requests.exceptions.ConnectionError:
            if attempt < max_attempts:
                print(
                    f"  Docker Model Runner not reachable yet, retrying ({attempt}/{max_attempts})...",
                    end="\r",
                )
                time.sleep(3)
            else:
                print(f"\n  ✗ Could not reach Docker Model Runner at {endpoint}")
                print("    Check that Docker Desktop 4.40+ is running.")
                print(
                    "    Enable it: Docker Desktop → Settings"
                    " → Beta Features → Docker Model Runner"
                )
                sys.exit(1)
        except Exception as exc:
            print(f"  ✗ Unexpected error talking to Docker Model Runner: {exc}")
            sys.exit(1)


# ─────────────────────────── main ────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR Review System — local environment setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "docker-model", "bedrock"],
        help="LLM provider (overrides LLM_PROVIDER in .env)",
    )
    parser.add_argument(
        "--model",
        help="Model name (overrides LLM_MODEL in .env)",
    )
    args = parser.parse_args()

    # CLI flag takes priority over .env; fall back to explicit default
    provider = args.provider or os.environ.get("LLM_PROVIDER") or "ollama"
    model = args.model or os.environ.get("LLM_MODEL") or "gemma4:4b"

    print("PR Review System — local environment setup")
    print(f"Provider: {provider}  Model: {model}")
    print("-" * 50)

    setup_dynamodb()

    if provider == "ollama":
        setup_ollama(model)
    elif provider == "docker-model":
        setup_docker_model(model)
    elif provider == "bedrock":
        print("\n[Bedrock] Using real AWS Bedrock — no local model setup needed.")
        print("  Ensure AWS credentials and Bedrock access are configured.")
    else:
        print(f"\nUnknown provider: {provider!r}. Expected: ollama | docker-model | bedrock")
        sys.exit(1)

    print("\n" + "=" * 50)
    print("  Local environment ready!")
    print(f"  DynamoDB: {os.environ.get('DYNAMODB_ENDPOINT', 'http://localhost:8000')}")
    if provider == "ollama":
        print(f"  Ollama:   {os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')}")
    elif provider == "docker-model":
        print(
            f"  Docker Model Runner: "
            f"{os.environ.get('DOCKER_MODEL_ENDPOINT', 'http://localhost:12434/engines/llama.cpp/v1')}"
        )
    print()
    print("  Start the API server:")
    print("    python3 api.py")
    print()
    print("  Or review a PR directly:")
    print("    python3 main.py --pr-url https://github.com/org/repo/pull/42")
    print("    python3 main.py --diff tests/fixtures/sample.diff")
    print("=" * 50)


if __name__ == "__main__":
    main()
