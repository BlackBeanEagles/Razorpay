"""Customer signup/login -- required before the AI agent can execute a purchase (Guardrail's
Razorpay call). Same hashing/signing approach as api/auth.py (admin), but a separate store,
separate session cookie, and a distinct role: a customer session resolves to a customer_id
that Shelf/Parity/Guardrail already key off of, rather than an admin capability.

New signups get appended to data/customer_profiles.json as real first-time customers, so
Parity's fairness check treats them accurately (eligible for the first-time-promo factor).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()  # idempotent -- see guardrail/mandate.py for why every entry point loads this

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit.audit_log import log_event
from api import session_revocation

SESSION_SIGNING_SECRET = os.environ.get("SESSION_SIGNING_SECRET", "dev-only-insecure-session-secret")
# Derived, not the raw shared secret -- see api/auth.py's matching comment for why: without
# domain separation, a customer token and an admin token (same underlying secret, same HMAC
# scheme) are cross-compatible with each other's verification function.
_SIGNING_KEY = hashlib.sha256(f"{SESSION_SIGNING_SECRET}|customer_session".encode()).digest()
SESSION_COOKIE_NAME = "customer_session"
# Logout can't truly revoke a stateless signed token (see require_customer's docstring) -- a
# captured cookie stays valid for its full TTL regardless. 24h (down from an earlier 7 days)
# keeps that exposure window reasonable without needing a server-side revocation store.
SESSION_TTL_SECONDS = 24 * 60 * 60

CUSTOMERS_AUTH_PATH = os.path.join(os.path.dirname(__file__), "customers_auth.json")
CUSTOMER_PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "customer_profiles.json")
_lock = threading.Lock()


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


def _load_accounts() -> dict:
    if not os.path.exists(CUSTOMERS_AUTH_PATH):
        return {}
    with open(CUSTOMERS_AUTH_PATH, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: str, data) -> None:
    """Write-temp-then-rename, so a process kill mid-write can never leave a truncated,
    corrupt JSON file behind -- os.replace is atomic on both Windows and POSIX."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _save_accounts(accounts: dict):
    _atomic_write_json(CUSTOMERS_AUTH_PATH, accounts)


def signup(username: str, password: str, name: str, email: str = "") -> dict:
    with _lock:
        accounts = _load_accounts()
        if username in accounts:
            raise ValueError("Username already taken.")

        with open(CUSTOMER_PROFILES_PATH, encoding="utf-8") as f:
            profiles = json.load(f)
        # Only "cNNN" human customer ids feed the next-number sequence -- customer_profiles.json
        # now also holds "ai_buyer_NNN" identities (mcp_server/techbazaar_mcp_server.py's
        # register_ai_buyer), a deliberately distinct namespace so the audit trail can always
        # tell an autonomous purchase from a human's. Without this filter, int(p["customer_id"][1:])
        # crashes outright on the very first "ai_buyer_..." entry (int("i_buyer_008")), which
        # broke real human signup the first time an AI buyer registered during this project's
        # own development.
        human_numbers = [
            int(p["customer_id"][1:]) for p in profiles
            if p["customer_id"].startswith("c") and p["customer_id"][1:].isdigit()
        ]
        next_num = max(human_numbers, default=0) + 1
        customer_id = f"c{next_num:03d}"
        profiles.append({
            "customer_id": customer_id,
            "loyalty_tier": "none",
            "is_first_time": True,
            "typical_order_size": "single",
        })
        _atomic_write_json(CUSTOMER_PROFILES_PATH, profiles)

        salt = secrets.token_bytes(16)
        accounts[username] = {
            "customer_id": customer_id,
            "name": name,
            "email": email,
            "salt": salt.hex(),
            "password_hash": _hash_password(password, salt).hex(),
        }
        _save_accounts(accounts)
        result = {"customer_id": customer_id, "username": username, "name": name}
        # Never log password/salt/hash -- only the account facts, matching Shelf/Parity/
        # Guardrail's audit coverage of every other write path in the app.
        log_event("customer_auth", "signup", {"username": username}, result, "ok")
        return result


def email_for_customer(customer_id: str) -> str:
    """Looks up the email on file for a given customer_id -- accounts are keyed by username,
    not customer_id, so this is a linear scan; the account list is small enough (a hackathon
    demo's customer base) that this is not worth indexing separately. Returns "" if unknown or
    never supplied (accounts created before the email field existed)."""
    accounts = _load_accounts()
    for account in accounts.values():
        if account.get("customer_id") == customer_id:
            return account.get("email", "")
    return ""


def verify_login(username: str, password: str) -> dict | None:
    accounts = _load_accounts()
    account = accounts.get(username)
    if account is None:
        return None
    salt = bytes.fromhex(account["salt"])
    candidate_hash = _hash_password(password, salt).hex()
    if not secrets.compare_digest(candidate_hash, account["password_hash"]):
        return None
    return account


def _sign(payload_b64: str) -> str:
    sig = hmac.new(_SIGNING_KEY, payload_b64.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def create_session_token(username: str, customer_id: str) -> str:
    now = time.time()
    # jti (a random, unique-per-token id) is what makes a SPECIFIC session revocable -- see
    # api/auth.py's matching comment for why this can't just be the username/customer_id.
    payload = {
        "username": username, "customer_id": customer_id, "issued_at": now,
        "expires_at": now + SESSION_TTL_SECONDS, "jti": secrets.token_hex(16),
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


def require_customer(request: Request) -> str:
    """FastAPI dependency: raises 401 unless a valid customer session is present. Returns
    the authenticated customer_id -- callers should use THIS, never a client-supplied one."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Login required before checkout.")
    data = decode_session_token(token)
    if data is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired, please log in again.")
    return data["customer_id"]
