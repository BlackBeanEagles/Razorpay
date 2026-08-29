"""Server-side session revocation -- makes logout actually invalidate a session instead of just
deleting the client's cookie.

Stateless HMAC-signed tokens (api/auth.py, api/customer_auth.py) verify themselves by signature
and expiry alone, which means logout is otherwise purely cosmetic: a token captured before
logout (XSS, a shared/public computer, a compromised device) stays fully valid -- and in this
app, that means still authorized to spend against a mandate -- for its entire remaining TTL,
regardless of whether the legitimate user logged out. This maintains an explicit list of
revoked session ids (a "jti" embedded in each token at issuance) that decode_session_token
checks on every request, on top of the existing signature/expiry check.

Persisted to a small JSON file (write-temp-then-rename, the same durability pattern used
elsewhere in this codebase for catalog.json/customers_auth.json/customer_profiles.json) so a
logout survives a server restart -- the one thing that would actually matter to a real user.
Guarded by a cross-process file lock (api/file_lock.py), not just a threading.Lock -- a plain
in-process lock gives zero protection between multiple worker processes (`uvicorn ...
--workers N`, a standard way to scale for real traffic), and read-modify-write-ing this exact
file without one is the identical race already found and fixed for Guardrail's mandate store.
This is still a single-machine store, though; a real multi-instance (multi-server) deployment
would want a shared store (Redis, a database) instead.
"""
import json
import os
import time

from api.file_lock import file_lock

_PATH = os.path.join(os.path.dirname(__file__), "revoked_sessions.json")
_LOCK_PATH = _PATH + ".lock"


def _load() -> dict:
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    tmp_path = f"{_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, _PATH)


def revoke(jti: str, expires_at: float) -> None:
    """Marks one specific session id as revoked. expires_at (the token's own natural expiry)
    lets old entries be pruned once they'd be rejected by the expiry check anyway -- there's no
    reason to remember a revocation forever once the token it applies to could never verify
    again regardless."""
    if not jti:
        return
    with file_lock(_LOCK_PATH):
        data = _load()
        data[jti] = expires_at
        now = time.time()
        data = {k: v for k, v in data.items() if v > now}
        _save(data)


def is_revoked(jti: str) -> bool:
    if not jti:
        return False
    with file_lock(_LOCK_PATH):
        data = _load()
    return jti in data
