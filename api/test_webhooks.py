"""Acceptance tests for POST /api/webhooks/razorpay (api/routes/webhooks.py). Calls the route
function directly with a real, genuinely-computed HMAC signature over a fake Request (same
"direct call, not a live HTTP round-trip" convention already used by api/test_auth.py's
FakeRequest for require_admin) -- signature verification over the raw request body is exactly
the part worth proving works, and this proves it without needing a live server."""
import asyncio
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from api.routes import webhooks
from guardrail import guardrail
from disputes import dispute_response

_TEST_SECRET = "test_webhook_secret_12345"

# Isolated from the real guardrail/mandates.json, ledger.json, and pending_purchases.json -- see
# guardrail.use_isolated_store's docstring. This file drives initiate_purchase/confirm_purchase
# for real, so without this it would reset and pollute the same real store every other test file
# already takes care to avoid touching.
_MANDATE_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_webhook_mandates.json")
_LEDGER_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_webhook_ledger.json")
_DISPUTES_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_webhook_disputes.json")


def setup_module():
    guardrail.use_isolated_store(_MANDATE_PATH, _LEDGER_PATH)
    dispute_response.DISPUTES_PATH = _DISPUTES_PATH


def setup():
    webhooks.RAZORPAY_WEBHOOK_SECRET = _TEST_SECRET
    webhooks.WEBHOOK_VERIFICATION_AVAILABLE = True
    guardrail.reset_mandate_store()
    # reset_mandate_store() only rebuilds mandates, not the ledger -- without clearing this too,
    # a leftover "success" ledger entry from an earlier run (same order_id reused across test
    # runs) makes confirm_purchase's own idempotency check return a stale cached "success"
    # WITHOUT re-applying spend to the freshly-reset mandate, which looks identical to a real
    # write failure (found the hard way: spend read back as 0 despite a "success" response).
    if os.path.exists(_LEDGER_PATH):
        os.remove(_LEDGER_PATH)
    if os.path.exists(guardrail.PENDING_PURCHASES_PATH):
        os.remove(guardrail.PENDING_PURCHASES_PATH)
    if os.path.exists(_DISPUTES_PATH):
        os.remove(_DISPUTES_PATH)
    # No real Razorpay call attempted for this file's dispute tests -- webhook signature
    # verification and the local drafting/status-sync logic are what's under test here, not the
    # real Disputes REST call (that's covered against a mocked client in
    # disputes/test_dispute_response.py).
    dispute_response.razorpay_rest.REAL_CHECKOUT_AVAILABLE = False


def _sign(body: bytes) -> str:
    return hmac.new(_TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payment_captured_event(order_id: str, amount_paise: int = 50000) -> bytes:
    return json.dumps({
        "entity": "event", "event": "payment.captured", "contains": ["payment"],
        "payload": {"payment": {"entity": {"id": "pay_test_123", "order_id": order_id, "amount": amount_paise, "status": "captured"}}},
    }).encode()


def _dispute_created_event(dispute_id: str, order_id: str, payment_id: str = "pay_disputed_1", amount_paise: int = 50000) -> bytes:
    return json.dumps({
        "entity": "event", "event": "payment.dispute.created", "contains": ["payment", "dispute"],
        "payload": {
            "payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": amount_paise, "status": "captured"}},
            "dispute": {"entity": {"id": dispute_id, "payment_id": payment_id, "amount": amount_paise,
                                    "reason_code": "chargeback", "respond_by": 1999999999, "status": "open"}},
        },
    }).encode()


def _dispute_status_event(event_name: str, dispute_id: str) -> bytes:
    return json.dumps({
        "entity": "event", "event": event_name, "contains": ["payment", "dispute"],
        "payload": {"dispute": {"entity": {"id": dispute_id, "status": event_name.rsplit(".", 1)[-1]}}},
    }).encode()


class FakeRequest:
    """Just enough of Starlette's Request interface for the route handler: async body() and a
    dict-like headers object -- same minimal-fake convention as api/test_auth.py's FakeRequest."""
    def __init__(self, body: bytes, signature: str = None):
        self._body = body
        self.headers = {"x-razorpay-signature": signature} if signature is not None else {}

    async def body(self):
        return self._body


def _call_webhook(body: bytes, signature: str = None) -> dict:
    return asyncio.run(webhooks.razorpay_webhook(FakeRequest(body, signature)))


def test_unconfigured_secret_refuses_every_webhook():
    setup()
    webhooks.WEBHOOK_VERIFICATION_AVAILABLE = False
    body = _payment_captured_event("order_whatever")
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is False


def test_wrong_signature_is_rejected():
    setup()
    body = _payment_captured_event("order_whatever")
    data = _call_webhook(body, "0" * 64)
    assert data["ok"] is False


def test_missing_signature_header_is_rejected():
    setup()
    body = _payment_captured_event("order_whatever")
    data = _call_webhook(body, None)
    assert data["ok"] is False


def test_unhandled_event_type_is_a_no_op():
    setup()
    body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is True


def test_no_pending_purchase_on_file_is_an_honest_no_op():
    # Not an error -- could be a mock-mode order Guardrail never tracked, or a redelivery of an
    # already-confirmed event. Razorpay must still get an ok response or it keeps retrying.
    setup()
    body = _payment_captured_event("order_nobody_initiated_this")
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is True
    assert "no pending purchase" in data["detail"].lower()


def test_valid_webhook_confirms_a_real_pending_purchase():
    # The actual gap this closes: a payment genuinely captured by Razorpay, with no browser ever
    # calling /api/purchase/confirm -- the webhook alone is what makes Guardrail find out.
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_webhook_test_1"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    try:
        token = guardrail.get_mandate_token("m_default")
        initiate_result = guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c888")
        assert initiate_result["status"] == "checkout_required"
        assert guardrail.get_pending_purchase("order_webhook_test_1") is not None

        body = _payment_captured_event("order_webhook_test_1")
        data = _call_webhook(body, _sign(body))
        assert data["ok"] is True
        assert data["confirm_result"]["status"] == "success"

        # Real effect: mandate spend was actually updated, and the pending record cleared --
        # purely from the webhook, with nothing else in the system ever calling confirm.
        state = guardrail.get_mandate_state("m_default")
        assert state["amount_spent_so_far_inr"] == 500
        assert guardrail.get_pending_purchase("order_webhook_test_1") is None
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


def test_webhook_redelivery_after_frontend_already_resolved_is_a_safe_no_op():
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_webhook_test_2"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    try:
        token = guardrail.get_mandate_token("m_default")
        guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c888")

        frontend_result = guardrail.confirm_purchase("m_default", "p001", 500, "order_webhook_test_2", requesting_customer_id="c888")
        assert frontend_result["status"] == "success"

        body = _payment_captured_event("order_webhook_test_2")
        data = _call_webhook(body, _sign(body))
        assert data["ok"] is True
        assert "confirm_result" not in data  # no pending record left -- nothing to re-confirm

        state = guardrail.get_mandate_state("m_default")
        assert state["amount_spent_so_far_inr"] == 500  # not 1000 -- counted once
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


def test_genuinely_concurrent_frontend_confirm_and_webhook_never_double_count():
    # The harder, more realistic race: the browser's own confirm call and the webhook both
    # arrive before EITHER has cleared the pending record or written the ledger entry. A
    # threading.Barrier forces both to reach confirm_purchase at the same instant -- this proves
    # the idempotency guarantee (already proven for two direct concurrent confirms in
    # guardrail/test_guardrail.py) holds through the webhook as a genuinely different entry point.
    import threading
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_webhook_test_3"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def via_frontend():
        barrier.wait()
        r = guardrail.confirm_purchase("m_default", "p001", 500, "order_webhook_test_3", requesting_customer_id="c888")
        with results_lock:
            results.append(r["status"])

    def via_webhook():
        barrier.wait()
        body = _payment_captured_event("order_webhook_test_3")
        data = _call_webhook(body, _sign(body))
        with results_lock:
            results.append(data.get("confirm_result", {}).get("status") or data.get("detail"))

    try:
        token = guardrail.get_mandate_token("m_default")
        guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c888")

        threads = [threading.Thread(target=via_frontend), threading.Thread(target=via_webhook)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = guardrail.get_mandate_state("m_default")
        assert state["amount_spent_so_far_inr"] == 500  # not 1000, regardless of which path "won"
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


def test_dispute_created_webhook_drafts_evidence_from_a_real_ledger_entry():
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_disputed_webhook_1"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    try:
        token = guardrail.get_mandate_token("m_default")
        guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c888")
        confirm_body = _payment_captured_event("order_disputed_webhook_1")
        _call_webhook(confirm_body, _sign(confirm_body))  # real success ledger entry to dispute against

        dispute_body = _dispute_created_event("disp_webhook_1", "order_disputed_webhook_1")
        data = _call_webhook(dispute_body, _sign(dispute_body))
        assert data["ok"] is True
        draft = data["dispute_draft"]
        assert draft["dispute_id"] == "disp_webhook_1"
        assert draft["evidence_found"] is True
        assert "m_default" in draft["summary"]
        assert dispute_response.get_dispute_draft("disp_webhook_1")["status"] == "draft_pending"
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


def test_dispute_created_webhook_with_no_matching_order_is_still_an_honest_draft():
    setup()
    body = _dispute_created_event("disp_webhook_2", "order_nobody_placed")
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is True
    assert data["dispute_draft"]["evidence_found"] is False


def test_dispute_created_webhook_missing_dispute_id_is_rejected():
    setup()
    body = json.dumps({
        "entity": "event", "event": "payment.dispute.created", "contains": ["payment", "dispute"],
        "payload": {"payment": {"entity": {"id": "pay_x", "order_id": "order_x"}}, "dispute": {"entity": {}}},
    }).encode()
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is False


def test_dispute_status_webhook_updates_an_existing_draft():
    setup()
    created_body = _dispute_created_event("disp_webhook_3", "order_whatever")
    _call_webhook(created_body, _sign(created_body))

    won_body = _dispute_status_event("payment.dispute.won", "disp_webhook_3")
    data = _call_webhook(won_body, _sign(won_body))
    assert data["ok"] is True
    assert dispute_response.get_dispute_draft("disp_webhook_3")["status"] == "won"


def test_dispute_status_webhook_with_no_local_draft_is_a_safe_noop():
    setup()
    body = _dispute_status_event("payment.dispute.lost", "disp_never_seen")
    data = _call_webhook(body, _sign(body))
    assert data["ok"] is True
    assert dispute_response.get_dispute_draft("disp_never_seen") is None
