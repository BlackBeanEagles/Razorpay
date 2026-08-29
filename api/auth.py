"""Admin authentication for the merchant/audit dashboard.

Single demo admin account (credentials from .env, never hardcoded/committed). The password
is never stored or compared in plaintext -- it's hashed with PBKDF2-HMAC-SHA256 and a random
per-process salt at startup, and login attempts are hashed the same way and compared with a
constant-time comparison. Sessions are HMAC-signed tokens (same pattern as Guardrail's
mandate tokens) carried in an HttpOnly cookie, so client-side JS can never read the session
value even if it could reach it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()  # idempotent -- see guardrail/mandate.py for why every entry point loads this

from api import session_revocation

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme-admin-password")
SESSION_SIGNING_SECRET = os.environ.get("SESSION_SIGNING_SECRET", "dev-only-insecure-session-secret")

# Derived, not the raw shared secret -- api/customer_auth.py reads the exact same
# SESSION_SIGNING_SECRET env var for its own sessions. Signing admin tokens with the raw
# shared secret meant a customer's own validly-signed customer_session token would ALSO verify
# correctly here (same secret, same HMAC scheme), since decode_session_token only checked the
# signature and expiry -- not which system actually issued the token. Any logged-in customer
# could copy their customer_session cookie's value into a cookie named admin_session and pass
# every check, reaching the admin dashboard with zero admin credentials. Domain-separating the
# key (hashing the shared secret together with a role-specific label) means a token signed for
# one role can never verify under the other's key, even with identical payload shapes -- on
# top of, not instead of, the explicit username check in require_admin below.
_SIGNING_KEY = hashlib.sha256(f"{SESSION_SIGNING_SECRET}|admin_session".encode()).digest()

SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours

_ADMIN_SALT = secrets.token_bytes(16)


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


_ADMIN_PASSWORD_HASH = _hash_password(ADMIN_PASSWORD, _ADMIN_SALT)


def verify_credentials(username: str, password: str) -> bool:
    if not secrets.compare_digest(username, ADMIN_USERNAME):
        return False
    candidate_hash = _hash_password(password, _ADMIN_SALT)
    return secrets.compare_digest(candidate_hash, _ADMIN_PASSWORD_HASH)


def _sign(payload_b64: str) -> str:
    sig = hmac.new(_SIGNING_KEY, payload_b64.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def create_session_token(username: str) -> str:
    now = time.time()
    # jti (a random, unique-per-token id) is what makes a SPECIFIC session revocable -- without
    # it, logout has nothing to name; every token signed for this username would look identical
    # for revocation purposes, so logging out on one device would have to invalidate all of them.
    payload = {
        "username": username, "issued_at": now, "expires_at": now + SESSION_TTL_SECONDS,
        "jti": secrets.token_hex(16),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{payload_b64}.{_sign(payload_b64)}"


def decode_session_token(token: str) -> dict | None:
    try:
        payload_b64, signature = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(payload_b64)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return None
    if time.time() > data.get("expires_at", 0):
        return None
    if session_revocation.is_revoked(data.get("jti")):
        return None
    return data


def require_admin(request: Request) -> str:
    """FastAPI dependency: raises 401 unless a valid admin session cookie is present.

    Checks the decoded username against ADMIN_USERNAME explicitly -- defense in depth on top
    of the domain-separated signing key above. Neither check alone should be relied on as the
    only barrier; together, a token has to both verify under the admin-specific key AND claim
    to actually be the admin account."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    data = decode_session_token(token)
    if data is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired.")
    if not secrets.compare_digest(data.get("username", ""), ADMIN_USERNAME):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return data["username"]
