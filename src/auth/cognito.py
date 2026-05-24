"""
AWS Cognito authentication for the PR Reviewer API.

Handles:
- OAuth2 Authorization Code flow with Cognito Hosted UI
- RS256 JWT validation against Cognito JWKS endpoint
- Secure HTTP-only cookie session management

Set these env vars to enable (all provided by Terraform outputs):
  COGNITO_USER_POOL_ID    - e.g. us-east-1_AbCdEfGhI
  COGNITO_CLIENT_ID       - app client ID
  COGNITO_CLIENT_SECRET   - app client secret (from SSM)
  COGNITO_DOMAIN          - hosted UI domain (no https://)
  APP_URL                 - public base URL e.g. https://snarky.hemantkumar.dev

If COGNITO_USER_POOL_ID is not set, auth is disabled (local dev mode).
"""
from __future__ import annotations

import base64
import logging
import os
from functools import lru_cache

import requests
from jose import jwk, jwt
from jose.exceptions import JWTError

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

REGION            = os.environ.get("AWS_REGION", "us-east-1")
USER_POOL_ID      = os.environ.get("COGNITO_USER_POOL_ID", "")
CLIENT_ID         = os.environ.get("COGNITO_CLIENT_ID", "")
CLIENT_SECRET     = os.environ.get("COGNITO_CLIENT_SECRET", "")
DOMAIN            = os.environ.get("COGNITO_DOMAIN", "")   # no https://
APP_URL           = os.environ.get("APP_URL", "http://localhost:8080").rstrip("/")

ENABLED           = bool(USER_POOL_ID and CLIENT_ID and CLIENT_SECRET and DOMAIN)
COOKIE_NAME       = "pr_reviewer_session"
COOKIE_MAX_AGE    = 8 * 3600   # 8 hours — matches Cognito token_validity


# ── JWKS (cached per process) ─────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    url = (
        f"https://cognito-idp.{REGION}.amazonaws.com"
        f"/{USER_POOL_ID}/.well-known/jwks.json"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── URL builders ─────────────────────────────────────────────────────────────

def login_url() -> str:
    return (
        f"https://{DOMAIN}/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&scope=email+openid+profile"
        f"&redirect_uri={APP_URL}/auth/callback"
    )


def logout_url() -> str:
    # logout_uri must point to a plain landing page, NOT the /auth/logout handler.
    # Pointing back at /auth/logout causes an infinite redirect loop because
    # Cognito redirects there after sign-out, which triggers another Cognito logout.
    return (
        f"https://{DOMAIN}/logout"
        f"?client_id={CLIENT_ID}"
        f"&logout_uri={APP_URL}/"
    )


# ── Token exchange ────────────────────────────────────────────────────────────

def exchange_code(code: str) -> dict:
    """Exchange an authorization code for tokens. Returns token dict."""
    credentials = base64.b64encode(
        f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    ).decode()
    resp = requests.post(
        f"https://{DOMAIN}/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{APP_URL}/auth/callback",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── JWT validation ────────────────────────────────────────────────────────────

def validate_token(token: str) -> dict | None:
    """
    Validate a Cognito ID token (RS256).
    Returns the claims dict on success, None if invalid/expired.
    """
    if not ENABLED:
        return None
    try:
        jwks = _get_jwks()
        headers = jwt.get_unverified_headers(token)
        kid = headers.get("kid", "")
        key_data = next(
            (k for k in jwks.get("keys", []) if k["kid"] == kid), None
        )
        if not key_data:
            logger.warning("JWT kid %r not found in Cognito JWKS", kid)
            return None
        public_key = jwk.construct(key_data)
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            options={"verify_at_hash": False},
        )
        return claims
    except JWTError as exc:
        logger.debug("JWT validation failed: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected error validating token: %s", exc)
        return None
