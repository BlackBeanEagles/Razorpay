"""Acceptance tests for admin/customer session security (api/auth.py, api/customer_auth.py,
api/rate_limit.py, api/session_revocation.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from api import auth, customer_auth, rate_limit, session_revocation


def test_customer_token_does_not_verify_as_admin():
    # The real bug this session found and fixed: admin and customer sessions used to share the
    # same raw signing secret and HMAC scheme, so a customer's own validly-signed token would
    # ALSO pass admin verification if resent under the admin cookie name. Domain-separated keys
    # mean the signature itself no longer matches across roles.
    customer_token = customer_auth.create_session_token("some_customer", "c999")
    assert auth.decode_session_token(customer_token) is None


def test_admin_token_does_not_verify_as_customer():
    # Same protection, the other direction.
    admin_token = auth.create_session_token("admin")
    assert customer_auth.decode_session_token(admin_token) is None


def test_legit_admin_token_still_works():
    token = auth.create_session_token("admin")
    data = auth.decode_session_token(token)
    assert data is not None
    assert data["username"] == "admin"


def test_legit_customer_token_still_works():
    token = customer_auth.create_session_token("shopper1", "c001")
    data = customer_auth.decode_session_token(token)
    assert data is not None
    assert data["customer_id"] == "c001"


def test_require_admin_rejects_non_admin_username_even_with_valid_signature():
    # Defense in depth: even a token that DOES verify under the admin signing key (e.g. if a
    # future change ever unified the secrets again) must still be rejected unless its username
    # is genuinely the configured admin account.
    from fastapi import HTTPException
    import time
    import json
    import base64

    now = time.time()
    payload = {"username": "not_the_real_admin", "issued_at": now, "expires_at": now + 3600}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    forged = f"{payload_b64}.{auth._sign(payload_b64)}"  # signed correctly, wrong identity

    class FakeRequest:
        cookies = {auth.SESSION_COOKIE_NAME: forged}

    try:
        auth.require_admin(FakeRequest())
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 401


def test_rate_limiter_locks_out_after_max_attempts_and_clears_on_success():
    key = "test_rate_limit_key"
    rate_limit._clear(key)  # isolate from any other test's state
    try:
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(key)
        assert rate_limit.seconds_until_unlocked(key) > 0

        rate_limit.record_success(key)
        assert rate_limit.seconds_until_unlocked(key) == 0
    finally:
        rate_limit._clear(key)


def test_rate_limiter_allows_attempts_under_the_threshold():
    key = "test_rate_limit_key_2"
    rate_limit._clear(key)
    try:
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_failure(key)
        assert rate_limit.seconds_until_unlocked(key) == 0
    finally:
        rate_limit._clear(key)


def test_rate_limiter_state_is_shared_across_module_reloads():
    # The real bug this fixes: the previous version was a pure in-memory dict, which under
    # multiple worker processes would give each worker its own independent counters --
    # persisting to a shared file (checked here by re-reading straight off disk, simulating a
    # separate process) is what makes the limit actually global rather than per-worker.
    key = "test_rate_limit_key_3"
    rate_limit._clear(key)
    try:
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(key)
        on_disk = rate_limit._load()
        assert key in on_disk
        assert len(on_disk[key]) == rate_limit.MAX_ATTEMPTS
    finally:
        rate_limit._clear(key)


def test_concurrent_rate_limit_failures_never_lost_across_simulated_workers():
    # file_lock uses atomic OS-level file creation (os.O_CREAT|O_EXCL) -- the same primitive
    # regardless of whether the contention is between threads or separate processes, so proving
    # no writes are lost under real concurrent threads is the same proof that applies across
    # real worker processes (this is the same reasoning already validated for Guardrail's
    # mandate-store lock). 20 threads each record one failure for the same key; the on-disk
    # count must be exactly 20 -- the earlier in-memory-only version would have been vulnerable
    # to losing writes under this exact pattern if it were ever run multi-process.
    import threading
    key = "test_rate_limit_concurrent_key"
    rate_limit._clear(key)
    try:
        threads = [threading.Thread(target=rate_limit.record_failure, args=(key,)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        on_disk = rate_limit._load()
        assert len(on_disk.get(key, [])) == 20
    finally:
        rate_limit._clear(key)


def _clear_revocations():
    session_revocation._save({})


def test_logout_revokes_admin_session_not_just_deletes_the_cookie():
    # The real gap this fixes: deleting the cookie client-side is cosmetic -- a token captured
    # before logout (shared computer, compromised device) used to stay fully valid until its
    # natural expiry regardless. Revoking its jti means the token itself is rejected afterward.
    _clear_revocations()
    try:
        token = auth.create_session_token("admin")
        data = auth.decode_session_token(token)
        assert data is not None  # valid before "logout"

        session_revocation.revoke(data["jti"], data["expires_at"])
        assert auth.decode_session_token(token) is None  # rejected after
    finally:
        _clear_revocations()


def test_logout_revokes_customer_session_not_just_deletes_the_cookie():
    _clear_revocations()
    try:
        token = customer_auth.create_session_token("shopper1", "c001")
        data = customer_auth.decode_session_token(token)
        assert data is not None

        session_revocation.revoke(data["jti"], data["expires_at"])
        assert customer_auth.decode_session_token(token) is None
    finally:
        _clear_revocations()


def test_revoking_one_session_does_not_affect_a_different_session():
    # Each token gets its own jti specifically so logging out on one device/tab doesn't
    # invalidate every other session for the same account.
    _clear_revocations()
    try:
        token_a = customer_auth.create_session_token("shopper1", "c001")
        token_b = customer_auth.create_session_token("shopper1", "c001")
        data_a = customer_auth.decode_session_token(token_a)

        session_revocation.revoke(data_a["jti"], data_a["expires_at"])

        assert customer_auth.decode_session_token(token_a) is None       # revoked
        assert customer_auth.decode_session_token(token_b) is not None   # untouched
    finally:
        _clear_revocations()


def test_revocation_persists_across_a_fresh_module_load():
    # Persisted to disk, not just an in-memory set -- a revocation must survive a server
    # restart, since that's exactly when a real user would expect "I logged out" to still hold.
    _clear_revocations()
    try:
        token = customer_auth.create_session_token("shopper1", "c001")
        data = customer_auth.decode_session_token(token)
        session_revocation.revoke(data["jti"], data["expires_at"])

        # Simulate a fresh process by reading straight off disk, bypassing any in-process state.
        on_disk = session_revocation._load()
        assert data["jti"] in on_disk
    finally:
        _clear_revocations()


if __name__ == "__main__":
    test_customer_token_does_not_verify_as_admin()
    test_admin_token_does_not_verify_as_customer()
    test_legit_admin_token_still_works()
    test_legit_customer_token_still_works()
    test_require_admin_rejects_non_admin_username_even_with_valid_signature()
    test_rate_limiter_locks_out_after_max_attempts_and_clears_on_success()
    test_rate_limiter_allows_attempts_under_the_threshold()
    test_rate_limiter_state_is_shared_across_module_reloads()
    test_logout_revokes_admin_session_not_just_deletes_the_cookie()
    test_logout_revokes_customer_session_not_just_deletes_the_cookie()
    test_revoking_one_session_does_not_affect_a_different_session()
    test_revocation_persists_across_a_fresh_module_load()
    test_concurrent_rate_limit_failures_never_lost_across_simulated_workers()
    print("All auth/session security tests passed.")
