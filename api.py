"""
Local development webhook server for the PR Review System.

Provides two endpoints:
  POST /review    — direct review via PR URL (no webhook needed)
  POST /webhook   — GitHub webhook with optional HMAC validation
  GET  /health    — liveness check + dependency status

Usage:
  python3 api.py
  # or
  uvicorn api:app --reload --port 8080

Then trigger a review:
  curl -X POST http://localhost:8080/review \
       -H 'Content-Type: application/json' \
       -d '{"pr_url": "https://github.com/org/repo/pull/42"}'
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests as _requests
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
import uvicorn

import src.auth.cognito as _cognito

from src.github.client import (
    fetch_pr_diff,
    fetch_pr_files,
    fetch_pr_metadata,
    parse_pr_url,
    post_pr_comment,
    validate_webhook_signature,
)
from src.graph.pr_review_graph import run_pr_review
from src.tools.dynamo_memory import DynamoMemoryStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────── startup checks ──────────────────────────────────


def _check_llm() -> None:
    """Verify the configured LLM backend is reachable before accepting requests."""
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    model = os.environ.get("LLM_MODEL", "gemma4:4b")

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        try:
            _requests.get(f"{base_url}/api/tags", timeout=3)
            logger.info(f"LLM  ✓  Ollama reachable at {base_url}  model={model}")
        except Exception:
            logger.error(f"LLM  ✗  Ollama not reachable at {base_url}")
            logger.error("       Start it:   docker-compose --profile ollama up -d")
            logger.error("       Or switch:  set LLM_PROVIDER=docker-model in .env")
            sys.exit(1)

    elif provider == "docker-model":
        endpoint = os.environ.get(
            "DOCKER_MODEL_ENDPOINT",
            "http://localhost:12434/engines/llama.cpp/v1",
        )
        try:
            resp = _requests.get(f"{endpoint}/models", timeout=3)
            available = [m["id"] for m in resp.json().get("data", [])]
            if not any(model in m for m in available):
                logger.warning(
                    f"LLM  ⚠  Docker Model Runner reachable but model '{model}' "
                    f"not found. Available: {available or 'none'}. "
                    f"Run: docker model pull {model}"
                )
            else:
                logger.info(
                    f"LLM  ✓  Docker Model Runner reachable at {endpoint}  model={model}"
                )
        except Exception:
            logger.error(f"LLM  ✗  Docker Model Runner not reachable at {endpoint}")
            logger.error("       Ensure Docker Desktop 4.40+ is running.")
            logger.error(
                "       Enable: Docker Desktop → Settings → Beta Features → Docker Model Runner"
            )
            sys.exit(1)

    elif provider == "bedrock":
        logger.info(f"LLM  ✓  AWS Bedrock — skipping connectivity check  model={model}")


def _check_dynamodb() -> None:
    """Verify DynamoDB is reachable and the table exists (auto-create if missing).

    Supports two modes:
      Local  — DYNAMODB_ENDPOINT=http://localhost:8000 (docker-compose default)
      AWS    — DYNAMODB_ENDPOINT= (empty/unset) → connects to real AWS DynamoDB
    """
    import boto3
    from botocore.exceptions import ClientError

    raw_endpoint = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000")
    endpoint_url: str | None = raw_endpoint if raw_endpoint else None
    table = os.environ.get("DYNAMODB_TABLE", "pr-review-local-memory")
    region = os.environ.get("AWS_REGION", "us-east-1")

    if endpoint_url:
        # Local DynamoDB — do a quick HTTP reachability check first.
        try:
            _requests.get(endpoint_url, timeout=3)
            logger.info(f"DB   ✓  DynamoDB (local) reachable at {endpoint_url}")
        except Exception:
            logger.warning(f"DB   ⚠  DynamoDB not reachable at {endpoint_url} — API endpoints will be unavailable")
            logger.warning("       Start it:  docker-compose up -d")
            return  # non-fatal: UI still served, DB endpoints return 500
        client = boto3.client(
            "dynamodb",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )
    else:
        # Real AWS DynamoDB — credentials come from env vars / profile / instance role.
        logger.info(f"DB   ✓  Using AWS DynamoDB  region={region}  table={table}")
        client = boto3.client("dynamodb", region_name=region)

    try:
        client.describe_table(TableName=table)
        logger.info(f"DB   ✓  Table '{table}' exists")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.warning(f"DB   ⚠  Table '{table}' not found — creating it now")
            client.create_table(
                TableName=table,
                AttributeDefinitions=[
                    {"AttributeName": "PK",         "AttributeType": "S"},
                    {"AttributeName": "SK",         "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
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
            logger.info(f"DB   ✓  Table '{table}' created")
        else:
            logger.warning(f"DB   ⚠  DynamoDB error: {e} — continuing without table")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_dynamodb()
    _check_llm()
    yield


# ─────────────────────────── app ─────────────────────────────────────────────


app = FastAPI(
    title="PR Review System — Local Dev",
    description="Multi-agent PR reviewer backed by local Ollama/Gemma and DynamoDB Local.",
    version="2.0.0",
    lifespan=lifespan,
)


# ─────────────────────────── auth middleware + routes ────────────────────────

# Paths always accessible without login (public read-only).
# The HTML shell ("/") is also public — auth gating is handled in the frontend.
_AUTH_PUBLIC = {
    "/",                   # main page HTML
    "/auth/login", "/auth/callback", "/auth/logout", "/auth/me",
    "/health", "/styles.css",
    # read-only API endpoints (no GitHub token, no writes)
    "/lineage", "/lineage/detail",
    "/review/detail",
    "/eval/metrics",
    "/user/settings",
    "/invite/request",
}


def _current_user_id(request: Request) -> str:
    """Return 'user:{email}' for authenticated users, 'anonymous' otherwise."""
    if not _cognito.ENABLED:
        return "anonymous"
    token = request.cookies.get(_cognito.COOKIE_NAME)
    claims = _cognito.validate_token(token) if token else None
    if not claims:
        return "anonymous"
    return f"user:{claims.get('email') or claims.get('sub', 'unknown')}"


def _current_email(request: Request) -> str | None:
    """Return the authenticated user's email, or None."""
    if not _cognito.ENABLED:
        return None
    token = request.cookies.get(_cognito.COOKIE_NAME)
    claims = _cognito.validate_token(token) if token else None
    return claims.get("email") if claims else None


def _is_admin(request: Request) -> bool:
    """Return True if the current user's email is in ADMIN_EMAILS."""
    email = _current_email(request)
    if not email:
        return False
    raw = os.environ.get("ADMIN_EMAILS", "")
    admins = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return email.lower() in admins


@app.middleware("http")
async def cognito_auth_middleware(request: Request, call_next):
    """
    Public read-only mode: unauthenticated users can browse reviews, lineage,
    and metrics.  Write operations and the DynamoDB tab require login.
    """
    if not _cognito.ENABLED or request.url.path in _AUTH_PUBLIC:
        return await call_next(request)

    token = request.cookies.get(_cognito.COOKIE_NAME)
    if not token or not _cognito.validate_token(token):
        # Always return JSON 401 — the frontend handles redirect/messaging.
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    return await call_next(request)


@app.get("/auth/login", include_in_schema=False)
def auth_login():
    """Redirect to Cognito Hosted UI login page."""
    return RedirectResponse(url=_cognito.login_url())


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(code: str, request: Request):
    """Handle OAuth2 callback — exchange code for tokens, set session cookie."""
    try:
        tokens = _cognito.exchange_code(code)
    except Exception as exc:
        logger.error("Token exchange failed: %s", exc)
        raise HTTPException(status_code=400, detail="Authentication failed — invalid code")

    id_token = tokens.get("id_token", "")
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=_cognito.COOKIE_NAME,
        value=id_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=_cognito.COOKIE_MAX_AGE,
    )
    return response


@app.get("/auth/logout", include_in_schema=False)
def auth_logout():
    """Clear session cookie and redirect to Cognito logout."""
    response = RedirectResponse(
        url=_cognito.logout_url() if _cognito.ENABLED else "/",
        status_code=302,
    )
    response.delete_cookie(_cognito.COOKIE_NAME)
    return response


@app.get("/auth/me", include_in_schema=False)
def auth_me(request: Request):
    """Return current user info from session cookie (used by UI)."""
    if not _cognito.ENABLED:
        return {"enabled": False, "user": None, "is_admin": False}
    token = request.cookies.get(_cognito.COOKIE_NAME)
    claims = _cognito.validate_token(token) if token else None
    if not claims:
        return {"enabled": True, "user": None, "is_admin": False}
    return {
        "enabled": True,
        "is_admin": _is_admin(request),
        "user": {
            "email": claims.get("email", ""),
            "name": claims.get("name", claims.get("email", "")),
            "sub": claims.get("sub", ""),
        },
    }


@app.get("/user/settings", include_in_schema=False)
def user_settings(request: Request):
    """Return persisted user settings (last_pr_url, etc.)."""
    user_id = _current_user_id(request)
    if user_id == "anonymous":
        return {"last_pr_url": None}
    store = DynamoMemoryStore()
    raw = store.get(user_id, "user_settings")
    if not raw:
        return {"last_pr_url": None}
    try:
        return json.loads(raw)
    except Exception:
        return {"last_pr_url": None}


# ─────────────────────────── admin endpoints ─────────────────────────────────


@app.delete("/review/{thread_id}")
def delete_review(thread_id: str, request: Request):
    """Delete all DynamoDB records for a review thread (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    store = DynamoMemoryStore()
    try:
        resp = store.client.query(
            TableName=store.table_name,
            KeyConditionExpression="PK = :tid",
            ExpressionAttributeValues={":tid": {"S": thread_id}},
            ProjectionExpression="SK",
        )
        keys = [item["SK"]["S"] for item in resp.get("Items", [])]
        for sk in keys:
            store.delete(thread_id, sk)
        return {"deleted": True, "records_removed": len(keys)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class InviteUserRequest(BaseModel):
    email: str


@app.get("/admin/users")
def admin_list_users(request: Request):
    """List Cognito user pool users (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    if not _cognito.ENABLED:
        raise HTTPException(status_code=503, detail="Cognito not configured")
    import boto3
    client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    try:
        users = []
        kwargs: dict = {"UserPoolId": user_pool_id, "Limit": 60}
        while True:
            resp = client.list_users(**kwargs)
            for u in resp.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
                users.append({
                    "username": u["Username"],
                    "email": attrs.get("email", ""),
                    "status": u.get("UserStatus", ""),
                    "enabled": u.get("Enabled", True),
                    "created": u.get("UserCreateDate", "").isoformat() if hasattr(u.get("UserCreateDate", ""), "isoformat") else str(u.get("UserCreateDate", "")),
                })
            token = resp.get("PaginationToken")
            if not token:
                break
            kwargs["PaginationToken"] = token
        return {"users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/users")
def admin_invite_user(req: InviteUserRequest, request: Request):
    """Create a Cognito user and send a temporary password email (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    if not _cognito.ENABLED:
        raise HTTPException(status_code=503, detail="Cognito not configured")
    import boto3
    from botocore.exceptions import ClientError as BotoClientError
    client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    try:
        client.admin_create_user(
            UserPoolId=user_pool_id,
            Username=req.email,
            UserAttributes=[{"Name": "email", "Value": req.email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],
        )
        return {"invited": True, "email": req.email}
    except BotoClientError as e:
        code = e.response["Error"]["Code"]
        if code == "UsernameExistsException":
            raise HTTPException(status_code=409, detail="User already exists")
        raise HTTPException(status_code=500, detail=e.response["Error"]["Message"])


@app.delete("/admin/users/{email:path}")
def admin_delete_user(email: str, request: Request):
    """Delete a Cognito user (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    if not _cognito.ENABLED:
        raise HTTPException(status_code=503, detail="Cognito not configured")
    import boto3
    client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    try:
        client.admin_delete_user(UserPoolId=user_pool_id, Username=email)
        return {"deleted": True, "email": email}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class InviteRequestBody(BaseModel):
    email: str


@app.post("/invite/request")
def invite_request(req: InviteRequestBody):
    """Store an access-request email so admins can invite from the Admin tab."""
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    store = DynamoMemoryStore()
    store.put(
        f"invite_request:{email}",
        "request",
        json.dumps({"email": email, "requested_at": datetime.now(tz=timezone.utc).isoformat()}),
        ttl_seconds=30 * 24 * 3600,
    )
    return {"ok": True}


@app.get("/admin/invite-requests")
def admin_invite_requests(request: Request):
    """List pending access requests (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    store = DynamoMemoryStore()
    try:
        raw_items = store.query_all_by_type("request")  # GSI query, no scan
        items = []
        for item in raw_items:
            pk = item.get("PK", {}).get("S", "")
            if pk.startswith("invite_request:"):
                try:
                    items.append(json.loads(item.get("value", {}).get("S", "{}")))
                except Exception:
                    pass
        return {"requests": items}  # already newest-first from GSI
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/admin/invite-requests/{email:path}")
def admin_dismiss_invite_request(email: str, request: Request):
    """Remove a pending invite request (admin only)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    store = DynamoMemoryStore()
    store.delete(f"invite_request:{email.strip().lower()}", "request")
    return {"ok": True}


# ─────────────────────────── models ──────────────────────────────────────────


class ReviewRequest(BaseModel):
    pr_url: str
    post_comment: bool = False  # set True to also post the review comment to GitHub
    github_token: str = ""     # user-supplied PAT; falls back to server GITHUB_TOKEN env var


class EvalRunRequest(BaseModel):
    mode: str  # "offline" | "shadow"
    pr_url: str
    shadow_provider: str = ""  # shadow only — overrides LLM_PROVIDER
    shadow_model: str = ""     # shadow only — overrides LLM_MODEL


class FeedbackRequest(BaseModel):
    thread_id: str
    thumbs: str   # "up" | "down" | "neutral"
    rating: int = 0  # 0 = unset, 1–5
    notes: str = ""


# ─────────────────────────── endpoints ───────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/styles.css", include_in_schema=False)
def styles():
    with open("templates/styles.css", encoding="utf-8") as f:
        return Response(content=f.read(), media_type="text/css")


@app.get("/health")
def health():
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    # For Bedrock, the model is BEDROCK_MODEL_ID not LLM_MODEL
    if provider == "bedrock":
        model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    else:
        model = os.environ.get("LLM_MODEL", "gemma4:4b")
    dynamo = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000")
    return {
        "status": "ok",
        "llm_provider": provider,
        "llm_model": model,
        "dynamodb_endpoint": dynamo,
    }


@app.post("/review")
async def review_pr(req: ReviewRequest, request: Request):
    """Directly review a GitHub PR — no webhook payload required."""
    github_token = req.github_token.strip() or os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise HTTPException(status_code=400, detail="No GitHub token available. Paste your token in the UI or set GITHUB_TOKEN on the server.")

    repo, pr_number = parse_pr_url(req.pr_url)
    logger.info(f"Fetching PR #{pr_number} from {repo}")

    pr_meta = fetch_pr_metadata(repo, pr_number, github_token)
    diff_content = fetch_pr_diff(repo, pr_number, github_token)
    file_list = fetch_pr_files(repo, pr_number, github_token)

    if len(diff_content) > 50_000:
        diff_content = diff_content[:50_000] + "\n\n... [diff truncated at 50 000 chars]"

    logger.info("Running multi-agent review...")
    result = run_pr_review(pr_meta, diff_content, file_list)

    if req.post_comment:
        post_pr_comment(repo, pr_number, result["summary_report"], github_token)
        logger.info(f"Comment posted to PR #{pr_number}")

    # Persist the PR URL so the user can restore it across devices / browsers.
    try:
        user_id = _current_user_id(request)
        store = DynamoMemoryStore()
        store.put(user_id, "user_settings",
                  json.dumps({"last_pr_url": req.pr_url}),
                  ttl_seconds=30 * 24 * 3600)
    except Exception as exc:
        logger.warning(f"Could not save user settings: {exc}")

    return {
        "score": result["overall_score"],
        "should_block": result["should_block"],
        "summary_report": result["summary_report"],
        "thread_id": result["pr_metadata"].get("thread_id", ""),
        "score_breakdown": result.get("score_breakdown", {}),
    }


@app.post("/webhook")
async def github_webhook(request: Request):
    """GitHub webhook — set WEBHOOK_SECRET=skip to bypass HMAC validation locally."""
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "skip")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")

    if webhook_secret != "skip":
        if not validate_webhook_signature(body, signature, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(body)
    action = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return {"message": f"Skipped — action={action!r} not handled"}

    pr = payload.get("pull_request", {})
    base_ref = pr.get("base", {}).get("ref", "")
    if base_ref not in ("main", "master"):
        return {"message": f"Skipped — base branch is {base_ref!r}"}

    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    logger.info(f"Webhook: reviewing PR #{pr_number} in {repo}")

    pr_meta = fetch_pr_metadata(repo, pr_number, github_token)
    diff_content = fetch_pr_diff(repo, pr_number, github_token)
    file_list = fetch_pr_files(repo, pr_number, github_token)

    if len(diff_content) > 50_000:
        diff_content = diff_content[:50_000] + "\n\n... [diff truncated at 50 000 chars]"

    result = run_pr_review(pr_meta, diff_content, file_list)

    if github_token:
        post_pr_comment(repo, pr_number, result["summary_report"], github_token)

    return {
        "score": result["overall_score"],
        "should_block": result["should_block"],
    }


# ─────────────────────────── evaluation helpers ──────────────────────────────


def _format_eval_result(result: dict, duration_ms: int) -> dict:
    all_findings = (
        result.get("security_findings", [])
        + result.get("code_quality_findings", [])
        + result.get("pr_review_findings", [])
    )
    non_pos = [f for f in all_findings if f.get("category") != "positive"]
    sev = {s: sum(1 for f in non_pos if f.get("severity") == s)
           for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}
    return {
        "thread_id": result["pr_metadata"].get("thread_id", ""),
        "score": result.get("overall_score", 0),
        "should_block": result.get("should_block", False),
        "summary_report": result.get("summary_report", ""),
        "finding_counts": sev,
        "total_findings": len(non_pos),
        "duration_ms": duration_ms,
    }


def _compare_findings(primary: dict, shadow: dict) -> dict:
    def _titles(r: dict) -> set[str]:
        findings = (
            r.get("security_findings", [])
            + r.get("code_quality_findings", [])
            + r.get("pr_review_findings", [])
        )
        return {f.get("title", "").lower().strip()
                for f in findings if f.get("category") != "positive"}

    pt, st = _titles(primary), _titles(shadow)
    union = pt | st
    both = pt & st
    return {
        "score_delta": round(
            (shadow.get("overall_score") or 0) - (primary.get("overall_score") or 0), 1
        ),
        "duration_delta_ms": (shadow.get("_duration_ms") or 0) - (primary.get("_duration_ms") or 0),
        "block_agreement": primary.get("should_block") == shadow.get("should_block"),
        "overlap_pct": round(len(both) / max(len(union), 1) * 100, 1),
        "in_both": sorted(both),
        "primary_only": sorted(pt - st),
        "shadow_only": sorted(st - pt),
    }


# ─────────────────────────── evaluation endpoints ────────────────────────────


@app.post("/eval/run")
async def eval_run(req: EvalRunRequest):
    """Run offline or shadow evaluation against a GitHub PR."""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise HTTPException(status_code=400, detail="GITHUB_TOKEN not set in .env")

    repo, pr_number = parse_pr_url(req.pr_url)
    pr_meta = fetch_pr_metadata(repo, pr_number, github_token)
    diff_content = fetch_pr_diff(repo, pr_number, github_token)
    file_list = fetch_pr_files(repo, pr_number, github_token)

    if len(diff_content) > 50_000:
        diff_content = diff_content[:50_000] + "\n\n... [diff truncated at 50 000 chars]"

    if req.mode == "offline":
        t0 = time.monotonic()
        result = run_pr_review(pr_meta, diff_content, file_list)
        ms = int((time.monotonic() - t0) * 1000)
        return {"mode": "offline", "primary": _format_eval_result(result, ms)}

    if req.mode == "shadow":
        # ── primary run ──────────────────────────────
        t0 = time.monotonic()
        primary = run_pr_review(pr_meta, diff_content, file_list)
        primary_ms = int((time.monotonic() - t0) * 1000)

        # ── shadow run — temporarily override env vars ───────────────────────
        # NOTE: env-var swap is not thread-safe; safe for single-user local dev.
        s_provider = req.shadow_provider or os.environ.get("LLM_PROVIDER", "ollama")
        s_model = req.shadow_model or os.environ.get("LLM_MODEL", "gemma4:4b")
        saved = {k: os.environ.get(k) for k in ("LLM_PROVIDER", "LLM_MODEL")}
        os.environ["LLM_PROVIDER"] = s_provider
        os.environ["LLM_MODEL"] = s_model
        shadow_error: str | None = None
        shadow_ms = 0
        try:
            t1 = time.monotonic()
            shadow = run_pr_review(dict(pr_meta), diff_content, file_list)
            shadow_ms = int((time.monotonic() - t1) * 1000)
        except Exception as exc:
            shadow = {}
            shadow_error = str(exc)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        pf = _format_eval_result(primary, primary_ms)
        sf = _format_eval_result(shadow, shadow_ms) if not shadow_error else {"error": shadow_error}
        cmp = _compare_findings(
            {**primary, "_duration_ms": primary_ms},
            {**shadow, "_duration_ms": shadow_ms},
        ) if not shadow_error else {}

        return {
            "mode": "shadow",
            "primary": pf,
            "shadow": sf,
            "shadow_config": {"provider": s_provider, "model": s_model},
            "comparison": cmp,
        }

    raise HTTPException(status_code=400, detail=f"Unknown mode: {req.mode!r}. Use 'offline' or 'shadow'.")


@app.post("/eval/feedback")
def eval_feedback(req: FeedbackRequest):
    """Store human feedback (thumbs + optional rating/notes) for a review run."""
    store = DynamoMemoryStore()
    store.put(req.thread_id, "eval_feedback", json.dumps({
        "thread_id": req.thread_id,
        "thumbs": req.thumbs,
        "rating": req.rating,
        "notes": req.notes,
        "submitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }))
    return {"ok": True}


@app.get("/eval/metrics")
def eval_metrics():
    """Aggregate metrics from all completed reviews plus any stored feedback."""
    store = DynamoMemoryStore()
    try:
        # ── all lineage_run records (GSI query, no scan) ──────────────────────
        runs: list[dict] = []
        for item in store.query_all_by_type("lineage_run"):
            try:
                d = json.loads(item.get("value", {}).get("S", "{}"))
                d["thread_id"] = item.get("PK", {}).get("S", "")
                runs.append(d)
            except Exception:
                pass

        # ── all feedback records (GSI query, no scan) ─────────────────────────
        feedbacks: dict[str, dict] = {}
        for item in store.query_all_by_type("eval_feedback"):
            tid = item.get("PK", {}).get("S", "")
            try:
                feedbacks[tid] = json.loads(item.get("value", {}).get("S", "{}"))
            except Exception:
                pass

        scores = [r["final_score"] for r in runs if r.get("final_score") is not None]
        durations = [r["total_duration_ms"] for r in runs if r.get("total_duration_ms")]

        # Score distribution buckets: 9-10, 7-8, 5-6, 0-4
        buckets = {"9-10": 0, "7-8": 0, "5-6": 0, "0-4": 0}
        for s in scores:
            if s >= 9:   buckets["9-10"] += 1
            elif s >= 7: buckets["7-8"]  += 1
            elif s >= 5: buckets["5-6"]  += 1
            else:        buckets["0-4"]  += 1

        metrics = {
            "total_runs": len(runs),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
            "min_score": round(min(scores), 1) if scores else None,
            "max_score": round(max(scores), 1) if scores else None,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "block_rate_pct": round(
                sum(1 for r in runs if r.get("should_block")) / max(len(runs), 1) * 100, 1
            ),
            "thumbs_up": sum(1 for f in feedbacks.values() if f.get("thumbs") == "up"),
            "thumbs_down": sum(1 for f in feedbacks.values() if f.get("thumbs") == "down"),
            "score_buckets": buckets,
        }

        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        for r in runs:
            r["feedback"] = feedbacks.get(r["thread_id"])

        return {"runs": runs, "metrics": metrics}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────── dynamo browser endpoints ────────────────────────


@app.get("/db/records")
def list_db_records():
    """Scan and return all records from the DynamoDB memory table."""
    store = DynamoMemoryStore()
    try:
        items = []
        kwargs: dict = {"TableName": store.table_name}
        now = int(time.time())
        while True:
            resp = store.client.scan(**kwargs)
            for item in resp.get("Items", []):
                ttl_val = int(item.get("ttl", {}).get("N", 0))
                items.append(
                    {
                        "thread_id": item.get("PK", {}).get("S", ""),
                        "key": item.get("SK", {}).get("S", ""),
                        "value": item.get("value", {}).get("S", ""),
                        "ttl": ttl_val,
                        "expires_in_s": max(0, ttl_val - now),
                    }
                )
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return {"table": store.table_name, "count": len(items), "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/db/records/{thread_id}/{key}")
def delete_db_record(thread_id: str, key: str):
    """Delete a single record from the DynamoDB memory table."""
    store = DynamoMemoryStore()
    store.delete(thread_id, key)
    return {"deleted": True}


# ─────────────────────────── pagination helpers ───────────────────────────────

_PAGE_SIZE = 20


def _encode_cursor(last_evaluated_key: dict | None) -> str | None:
    if not last_evaluated_key:
        return None
    return base64.b64encode(json.dumps(last_evaluated_key).encode()).decode()


def _decode_cursor(cursor: str) -> dict | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.b64decode(cursor).decode())
    except Exception:
        return None


# ─────────────────────────── lineage endpoints ───────────────────────────────


@app.get("/lineage")
def list_lineage_runs(limit: int = _PAGE_SIZE, cursor: str = ""):
    """List PR review run summaries, newest first. Supports cursor pagination."""
    store = DynamoMemoryStore()
    try:
        items, next_key = store.query_by_type(
            "lineage_run",
            limit=min(limit, 100),
            cursor=_decode_cursor(cursor),
            newest_first=True,
        )
        runs = []
        for item in items:
            val = item.get("value", {}).get("S", "{}")
            try:
                data = json.loads(val)
            except Exception:
                data = {}
            runs.append({"thread_id": item.get("PK", {}).get("S", ""), **data})
        return {"runs": runs, "next_cursor": _encode_cursor(next_key)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/lineage/detail")
def get_lineage_detail(thread_id: str):
    """Get full lineage for a specific review run (all lineage_* keys)."""
    store = DynamoMemoryStore()
    try:
        resp = store.client.query(
            TableName=store.table_name,
            KeyConditionExpression="PK = :tid",
            ExpressionAttributeValues={":tid": {"S": thread_id}},
        )
        lineage: dict = {}
        for item in resp.get("Items", []):
            sk = item.get("SK", {}).get("S", "")
            if sk.startswith("lineage_"):
                val = item.get("value", {}).get("S", "{}")
                try:
                    lineage[sk.removeprefix("lineage_")] = json.loads(val)
                except Exception:
                    lineage[sk.removeprefix("lineage_")] = val
        return {"thread_id": thread_id, "lineage": lineage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/review/detail")
def get_review_detail(thread_id: str):
    """Fetch the stored report + score for a past review run."""
    store = DynamoMemoryStore()
    try:
        summary_report = store.get(thread_id, "summary_report") or ""

        sb_raw = store.get(thread_id, "score_breakdown")
        score_breakdown = json.loads(sb_raw) if sb_raw else {}

        lr_raw = store.get(thread_id, "lineage_run")
        run = json.loads(lr_raw) if lr_raw else {}

        return {
            "thread_id": thread_id,
            "summary_report": summary_report,
            "score_breakdown": score_breakdown,
            "final_score": run.get("final_score"),
            "should_block": run.get("should_block", False),
            "pr_title": run.get("pr_title", ""),
            "pr_repo": run.get("pr_repo", ""),
            "pr_number": run.get("pr_number"),
            "started_at": run.get("started_at", ""),
            "total_duration_ms": run.get("total_duration_ms"),
            "files_count": run.get("files_count"),
            "diff_chars": run.get("diff_chars"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────── entry point ─────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 8080))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
