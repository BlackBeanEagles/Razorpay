"""Acceptance tests for the customer support-request log."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit import support_log


def setup():
    support_log._LOG_PATH = _TEST_PATH
    if os.path.exists(_TEST_PATH):
        os.remove(_TEST_PATH)


_TEST_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_support_requests.jsonl")


def teardown_module():
    if os.path.exists(_TEST_PATH):
        os.remove(_TEST_PATH)
    support_log._LOG_PATH = os.path.join(os.path.dirname(__file__), "support_requests.jsonl")


def test_logged_request_is_open_by_default():
    # pytest 8 dropped nose-style auto-invocation of a bare setup() -- it's never called
    # automatically, only when a test calls it itself (same convention as
    # guardrail/test_guardrail.py's tests, each calling setup() as their own first line).
    setup()
    req = support_log.log_support_request("c001", "guardrail", "blocked: exceeds mandate", "why was I blocked?")
    assert req["status"] == "open"
    assert req["customer_id"] == "c001"
    all_reqs = support_log.read_all()
    assert len(all_reqs) == 1
    assert all_reqs[0]["request_id"] == req["request_id"]


def test_two_requests_get_distinct_ids():
    setup()
    r1 = support_log.log_support_request("c001", "shelf", "", "no match found")
    r2 = support_log.log_support_request("c002", "shelf", "", "also no match")
    assert r1["request_id"] != r2["request_id"]


def test_mark_resolved_updates_status_and_returns_the_entry():
    setup()
    req = support_log.log_support_request("c001", "parity", "flagged", "price seems wrong")
    updated = support_log.mark_resolved(req["request_id"])
    assert updated is not None
    assert updated["status"] == "resolved"
    assert updated["customer_id"] == "c001"
    assert updated["message"] == "price seems wrong"
    on_disk = [r for r in support_log.read_all() if r["request_id"] == req["request_id"]][0]
    assert on_disk["status"] == "resolved"


def test_mark_resolved_unknown_id_returns_none():
    setup()
    assert support_log.mark_resolved("does_not_exist") is None


def test_new_request_has_no_confirmation_or_resolution_email_yet():
    setup()
    req = support_log.log_support_request("c001", "general", "", "hello")
    assert req["confirmation_email"] is None
    assert req["resolution_email"] is None


def test_update_confirmation_email_records_the_real_outcome():
    setup()
    req = support_log.log_support_request("c001", "general", "", "hello")
    result = {"sent": True, "provider_id": "abc123", "detail": "Sent via Resend."}
    assert support_log.update_confirmation_email(req["request_id"], result) is True
    updated = [r for r in support_log.read_all() if r["request_id"] == req["request_id"]][0]
    assert updated["confirmation_email"] == result


def test_update_confirmation_email_unknown_id_returns_false():
    setup()
    assert support_log.update_confirmation_email("does_not_exist", {}) is False


def test_update_resolution_email_records_the_real_outcome_independently():
    setup()
    req = support_log.log_support_request("c001", "general", "", "hello")
    confirm_result = {"sent": True, "provider_id": "abc123", "detail": "Sent via Resend."}
    support_log.update_confirmation_email(req["request_id"], confirm_result)
    resolve_result = {"sent": False, "provider_id": None, "detail": "Resend rejected the send."}
    assert support_log.update_resolution_email(req["request_id"], resolve_result) is True
    updated = [r for r in support_log.read_all() if r["request_id"] == req["request_id"]][0]
    # The two fields are independent -- a failed resolution email must not clobber (or be
    # clobbered by) the earlier, separately-tracked confirmation email outcome.
    assert updated["confirmation_email"] == confirm_result
    assert updated["resolution_email"] == resolve_result


def test_update_resolution_email_unknown_id_returns_false():
    setup()
    assert support_log.update_resolution_email("does_not_exist", {}) is False
