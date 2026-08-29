"""Login rate limiter -- shared by admin and customer login so a fix to one can't be silently
missing from the other.

N failed attempts against the same key within a window locks that key out for a cooldown
period. Persisted to a small JSON file (write-temp-then-rename) and guarded by a cross-process
file lock (api/file_lock.py), not just an in-memory dict behind a threading.Lock -- an earlier
version of this module was purely in-memory and process-local, which is fine for a single
process but silently weakens under multiple worker processes (`uvicorn ... --workers N`, a
standard way to scale for real traffic): each worker would keep its own independent failure
counts, so an attacker whose requests get spread across N workers effectively gets N times the
intended attempt budget before any single worker's counter ever trips. Persisting to a shared
file closes that gap the same way it was already closed for Guardrail's mandate store and for
session revocation.

Without this, PBKDF2's per-guess cost is the ONLY thing slowing down a scripted attacker
guessing a password over the network -- nothing stops unlimited automated attempts.

Still a single-machine store; a real multi-instance (multi-server) deployment would want a
shared store (Redis, a database) instead.
"""
import json
import os
import time

from api.file_lock import file_lock

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 5 * 60
LOCKOUT_SECONDS = 5 * 60

_PATH = os.path.join(os.path.dirname(__file__), "rate_limit_failures.json")
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


def seconds_until_unlocked(key: str) -> float:
    """0 if key isn't currently locked out, else how many seconds remain."""
    with file_lock(_LOCK_PATH):
        data = _load()
        now = time.time()
        recent = [t for t in data.get(key, []) if now - t < WINDOW_SECONDS]
        if recent:
            data[key] = recent
        else:
            data.pop(key, None)
        _save(data)
        if len(recent) < MAX_ATTEMPTS:
            return 0.0
        oldest_counted = recent[-MAX_ATTEMPTS]
        remaining = LOCKOUT_SECONDS - (now - oldest_counted)
        return max(0.0, remaining)


def record_failure(key: str) -> None:
    with file_lock(_LOCK_PATH):
        data = _load()
        data.setdefault(key, []).append(time.time())
        _save(data)


def record_success(key: str) -> None:
    with file_lock(_LOCK_PATH):
        data = _load()
        if key in data:
            del data[key]
            _save(data)


def _clear(key: str) -> None:
    """Test-only helper -- removes a key's state entirely, regardless of window/lockout math."""
    with file_lock(_LOCK_PATH):
        data = _load()
        data.pop(key, None)
        _save(data)
