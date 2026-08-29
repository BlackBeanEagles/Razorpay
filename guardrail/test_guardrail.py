"""Acceptance tests for Guardrail (spec section 6)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail
from guardrail.razorpay_client import _MOCK_ORDERS

# Isolated from the real guardrail/mandates.json and guardrail/ledger.json -- reset_mandate_store()
# below rebuilds the mandate store from seed fixtures, discarding anything not in that seed. Run
# against the real store (this file's LEDGER_PATH used to point straight at the real ledger.json),
# every test run would wipe any actual in-progress mandate -- a real customer's, or an external AI
# buyer's mid-checkout purchase over MCP -- and any real purchase in the ledger. This happened for
# real during development (see guardrail.use_isolated_store's docstring).
MANDATE_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_mandates.json")
LEDGER_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_ledger.json")


def setup_module():
    # Re-asserted here (an execution-phase hook), not just at module import time (a collection-
    # phase event) -- when several test files each call use_isolated_store() at import time,
    # pytest's collection pass imports ALL of them before ANY test executes, so whichever file
    # was collected LAST silently wins for everyone. That's not hypothetical: it broke this
    # exact suite (idempotency tests saw stale ledger entries from mcp_server's isolated files)
    # the first time this fix was written with the call at module level. setup_module() runs
    # right before THIS file's tests execute, so it always re-points isolation back correctly
    # regardless of collection order.
    guardrail.use_isolated_store(MANDATE_PATH, LEDGER_PATH)


def setup():
    guardrail.reset_mandate_store()
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    _MOCK_ORDERS.clear()


def test_clean_purchase_success():
    setup()
    token = guardrail.get_mandate_token("m_default")
    r = guardrail.execute_purchase(token, "p001", 1799)
    assert r["status"] == "success"
    assert r["verification"]["all_match"] is True


def test_exceeds_mandate_blocked_before_razorpay():
    setup()
    token = guardrail.get_mandate_token("m_default")
    orders_before = dict(_MOCK_ORDERS)
    r = guardrail.execute_purchase(token, "p007", 4499)  # exceeds max_amount_inr=2000
    assert r["status"] == "blocked"
    assert r["razorpay_order_id"] is None
    assert _MOCK_ORDERS == orders_before  # no Razorpay call made


def test_expired_mandate_blocked():
    setup()
    token = guardrail.get_mandate_token("m_expired")
    r = guardrail.execute_purchase(token, "p001", 1799)
    assert r["status"] == "blocked"
    assert "expired" in r["reason"].lower()


def test_single_use_mandate_reused_blocked():
    setup()
    token = guardrail.get_mandate_token("m_single_use_spent")
    r = guardrail.execute_purchase(token, "p001", 1799)
    assert r["status"] == "blocked"
    assert "single-use" in r["reason"].lower()


def test_seeded_settlement_mismatch_not_silently_success():
    setup()
    token = guardrail.get_mandate_token("m_default")
    r = guardrail.execute_purchase(token, "p010", 1199, simulate_settlement_mismatch=True)
    assert r["status"] == "failed_verification"
    assert r["verification"]["all_match"] is False


def test_cumulative_spend_second_purchase_blocked():
    setup()
    token = guardrail.get_mandate_token("m_cumulative_test")
    r1 = guardrail.execute_purchase(token, "p001", 1799)
    assert r1["status"] == "success"
    token2 = guardrail.get_mandate_token("m_cumulative_test")  # re-fetch updated token
    r2 = guardrail.execute_purchase(token2, "p006", 2499)
    assert r2["status"] == "blocked"
    assert "exceed" in r2["reason"].lower()


def test_concurrent_purchases_never_exceed_mandate_limit():
    # Real race-condition regression test. Without the lock around read-check-write, firing
    # several purchases at the same mandate at once could let more than 4 of these succeed
    # (each reading a stale pre-update spend figure and independently deciding it's within
    # limit) -- exceeding the 2000 limit despite every individual check having "passed".
    import threading
    setup()
    mandate_id = "m_cumulative_test"  # limit 2000, starts at 0
    results = []
    results_lock = threading.Lock()

    def buy():
        token = guardrail.get_mandate_token(mandate_id)
        r = guardrail.execute_purchase(token, "p001", 500)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=buy) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r["status"] == "success"]
    assert len(successes) == 4  # exactly 2000 / 500 -- never more, regardless of thread interleaving
    final_state = guardrail.get_mandate_state(mandate_id)
    assert final_state["amount_spent_so_far_inr"] == 2000  # never over the limit


def test_confirm_purchase_idempotent_does_not_double_count():
    # A double form-submit or page reload after a successful confirm must not apply the same
    # real payment's spend against the mandate twice.
    from guardrail import razorpay_rest
    setup()
    mandate_id = "m_cumulative_test"
    original = razorpay_rest.captured_amount_inr
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    try:
        r1 = guardrail.confirm_purchase(mandate_id, "p001", 500, "order_test_idempotent_1")
        assert r1["status"] == "success"
        assert guardrail.get_mandate_state(mandate_id)["amount_spent_so_far_inr"] == 500

        r2 = guardrail.confirm_purchase(mandate_id, "p001", 500, "order_test_idempotent_1")  # same order_id again
        assert r2["status"] == "success"
        assert guardrail.get_mandate_state(mandate_id)["amount_spent_so_far_inr"] == 500  # not 1000
    finally:
        razorpay_rest.captured_amount_inr = original


def test_confirm_purchase_refunds_when_mandate_cant_absorb_captured_payment():
    # Simulates the exact race the fix targets: the advisory pre-check (check_mandate, run
    # against a token that could be a step stale) says allowed, but a purchase against the
    # SAME mandate lands first and fills it before the atomic, freshly-reloaded reserve runs.
    # Money is genuinely captured by this point -- it must be refunded, not left taken.
    from guardrail import razorpay_rest
    setup()
    mandate_id = "m_cumulative_test"  # limit 2000
    orig_check_mandate = guardrail.check_mandate
    orig_captured = razorpay_rest.captured_amount_inr
    orig_payment_id = razorpay_rest.captured_payment_id
    orig_refund = razorpay_rest.refund_payment

    guardrail.check_mandate = lambda token, amount: {"allowed": True, "reason": "stale pre-check (simulated race)"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    razorpay_rest.captured_payment_id = lambda order_id: "pay_test_123"
    refund_calls = []
    razorpay_rest.refund_payment = lambda payment_id, amount_inr: refund_calls.append((payment_id, amount_inr)) or {"id": "rfnd_test_999"}
    try:
        # Another confirm (or a mock purchase) already filled the mandate to its limit.
        guardrail._atomic_reserve_spend(mandate_id, 2000)
        assert guardrail.get_mandate_state(mandate_id)["amount_spent_so_far_inr"] == 2000

        r2 = guardrail.confirm_purchase(mandate_id, "p001", 500, "order_B")
        assert r2["status"] == "refunded"
        assert r2["refund"]["refunded"] is True
        assert refund_calls == [("pay_test_123", 500)]
        assert guardrail.get_mandate_state(mandate_id)["amount_spent_so_far_inr"] == 2000  # unchanged by order_B
    finally:
        guardrail.check_mandate = orig_check_mandate
        razorpay_rest.captured_amount_inr = orig_captured
        razorpay_rest.captured_payment_id = orig_payment_id
        razorpay_rest.refund_payment = orig_refund


def test_confirm_purchase_reports_refund_failure_honestly():
    # If the refund API call itself fails, this must not be silently swallowed or reported as
    # success -- the customer and any operator need to know manual reconciliation is required.
    from guardrail import razorpay_rest
    setup()
    mandate_id = "m_cumulative_test"
    orig_check_mandate = guardrail.check_mandate
    orig_captured = razorpay_rest.captured_amount_inr
    orig_payment_id = razorpay_rest.captured_payment_id
    orig_refund = razorpay_rest.refund_payment

    guardrail.check_mandate = lambda token, amount: {"allowed": True, "reason": "stale pre-check (simulated race)"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    razorpay_rest.captured_payment_id = lambda order_id: "pay_test_123"

    def failing_refund(payment_id, amount_inr):
        raise RuntimeError("simulated Razorpay refund API failure")
    razorpay_rest.refund_payment = failing_refund
    try:
        guardrail._atomic_reserve_spend(mandate_id, 2000)
        r2 = guardrail.confirm_purchase(mandate_id, "p001", 500, "order_B")
        assert r2["status"] == "blocked"
        assert r2["refund"]["refunded"] is False
        assert "manual reconciliation" in r2["reason"].lower()
    finally:
        guardrail.check_mandate = orig_check_mandate
        razorpay_rest.captured_amount_inr = orig_captured
        razorpay_rest.captured_payment_id = orig_payment_id
        razorpay_rest.refund_payment = orig_refund


def test_shared_mandate_usable_by_any_customer():
    # m_default has no owner (owner_customer_id is None) -- deliberately shared across many
    # customers, e.g. every default browser session and the whole batch test dataset.
    setup()
    token = guardrail.get_mandate_token("m_default", requesting_customer_id="c_anyone_at_all")
    r = guardrail.execute_purchase(token, "p001", 1799, requesting_customer_id="c_anyone_at_all")
    assert r["status"] == "success"


def test_owned_mandate_only_usable_by_its_owner():
    setup()
    issued = guardrail.issue_and_store_mandate("merchant_test_1", 2000, 3600, False, owner_customer_id="c_owner")

    # The owner can use it normally.
    token = guardrail.get_mandate_token(issued["mandate_id"], requesting_customer_id="c_owner")
    r = guardrail.execute_purchase(token, "p001", 1799, requesting_customer_id="c_owner")
    assert r["status"] == "success"


def test_owned_mandate_rejected_for_a_different_customer():
    setup()
    issued = guardrail.issue_and_store_mandate("merchant_test_1", 2000, 3600, False, owner_customer_id="c_owner")

    # A different, genuinely-authenticated customer must not be able to look it up or spend
    # against it -- reported identically to "doesn't exist", not a distinguishable 403.
    try:
        guardrail.get_mandate_token(issued["mandate_id"], requesting_customer_id="c_intruder")
        assert False, "expected KeyError"
    except KeyError:
        pass
    assert guardrail.get_mandate_state(issued["mandate_id"], requesting_customer_id="c_intruder") is None

    # Defense-in-depth: even a directly-supplied token (bypassing get_mandate_token) for
    # someone else's mandate must be rejected inside execute_purchase itself.
    owner_token = guardrail.get_mandate_token(issued["mandate_id"], requesting_customer_id="c_owner")
    r = guardrail.execute_purchase(owner_token, "p001", 1799, requesting_customer_id="c_intruder")
    assert r["status"] == "blocked"
    assert "does not belong" in r["reason"].lower()


def test_owned_mandate_visible_to_its_owner_via_get_mandate_state():
    setup()
    issued = guardrail.issue_and_store_mandate("merchant_test_1", 2000, 3600, False, owner_customer_id="c_owner")
    state = guardrail.get_mandate_state(issued["mandate_id"], requesting_customer_id="c_owner")
    assert state is not None
    assert state["owner_customer_id"] == "c_owner"


def test_concurrent_mandate_issuance_never_loses_a_mandate():
    # Same class of race _atomic_reserve_spend was built for, but for issue_and_store_mandate
    # -- two concurrent POST /api/mandates calls must not let one silently clobber the other.
    import threading
    setup()
    issued_by_owner = {}
    lock = threading.Lock()

    def issue(n):
        owner = f"c_{n}"
        result = guardrail.issue_and_store_mandate("merchant_test_1", 2000, 3600, False, owner_customer_id=owner)
        with lock:
            issued_by_owner[owner] = result["mandate_id"]

    threads = [threading.Thread(target=issue, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(issued_by_owner) == 8
    assert len(set(issued_by_owner.values())) == 8  # all unique ids -- none clobbered
    for owner, mandate_id in issued_by_owner.items():
        assert guardrail.get_mandate_state(mandate_id, requesting_customer_id=owner) is not None


def test_concurrent_confirm_of_the_same_order_never_double_counts_spend():
    # Real concurrency gap found and fixed this session: the earlier idempotency check (a
    # plain, separately-locked read before the spend reservation) protected against a
    # SEQUENTIAL double-confirm (one finishes, then another starts) but not a genuinely
    # CONCURRENT one -- two truly simultaneous confirms of the SAME razorpay_order_id could
    # both pass the idempotency check before either had committed spend, then both
    # independently reserve spend for one real payment. A threading.Barrier forces both
    # threads to reach the critical section at the same instant, maximizing the chance this
    # race would show up if the fix weren't real.
    #
    # Amount is deliberately small relative to the mandate limit (500 against a 2000 cap,
    # 2x500=1000 well under it) -- with a LARGE amount, the ordinary cumulative-spend-limit
    # check would reject the second reservation anyway and mask whether idempotency
    # specifically was ever the thing preventing double-counting. Confirmed by hand-simulating
    # the old (separately-locked) pattern with this exact amount: it let both confirms succeed
    # and double-counted to 1000 for one real 500 payment.
    import threading
    from guardrail import razorpay_rest
    setup()
    mandate_id = "m_cumulative_test"  # limit 2000
    order_id = "order_concurrent_confirm_test"
    amount = 500
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.captured_amount_inr = lambda oid: amount
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def confirm():
        barrier.wait()  # both threads start the real work at the same instant
        r = guardrail.confirm_purchase(mandate_id, "p001", amount, order_id)
        with results_lock:
            results.append(r)

    try:
        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r["status"] == "success" for r in results)  # both report success (one real, one cached)
        final_state = guardrail.get_mandate_state(mandate_id)
        assert final_state["amount_spent_so_far_inr"] == amount  # NOT 1000 -- counted once, not twice
    finally:
        razorpay_rest.captured_amount_inr = orig_captured


def test_get_pending_purchase_returns_none_for_unknown_order():
    setup()
    assert guardrail.get_pending_purchase("order_does_not_exist") is None


def test_initiate_purchase_records_a_pending_purchase_for_the_webhook_to_find():
    # The whole point of pending_purchases.json: if the browser that opened Checkout never
    # comes back to call confirm_purchase itself, the payment webhook needs everything --
    # mandate_id, product_id, amount, customer -- to independently confirm the same purchase.
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_pending_test_1"}
    try:
        token = guardrail.get_mandate_token("m_default")
        result = guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c777")
        assert result["status"] == "checkout_required"
        pending = guardrail.get_pending_purchase("order_pending_test_1")
        assert pending is not None
        assert pending["mandate_id"] == "m_default"
        assert pending["product_id"] == "p001"
        assert pending["amount_inr"] == 500
        assert pending["requesting_customer_id"] == "c777"
    finally:
        razorpay_rest.create_order = orig_create_order


def test_confirm_purchase_clears_the_pending_record_on_success():
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr
    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_pending_test_2"}
    razorpay_rest.captured_amount_inr = lambda order_id: 500
    try:
        token = guardrail.get_mandate_token("m_default")
        guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c777")
        assert guardrail.get_pending_purchase("order_pending_test_2") is not None

        result = guardrail.confirm_purchase("m_default", "p001", 500, "order_pending_test_2", requesting_customer_id="c777")
        assert result["status"] == "success"
        assert guardrail.get_pending_purchase("order_pending_test_2") is None
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


def test_pending_purchase_survives_a_transient_verification_failure():
    # If confirm_purchase can't even reach Razorpay to ask what was captured, that's exactly the
    # transient case worth retrying later (a webhook redelivery, or the browser retrying) --
    # clearing the pending record here would make that retry impossible.
    from guardrail import razorpay_rest
    setup()
    orig_create_order = razorpay_rest.create_order
    orig_captured = razorpay_rest.captured_amount_inr

    def _raise(order_id):
        raise ConnectionError("simulated network failure")

    razorpay_rest.create_order = lambda amount_inr, receipt: {"id": "order_pending_test_3"}
    razorpay_rest.captured_amount_inr = _raise
    try:
        token = guardrail.get_mandate_token("m_default")
        guardrail.initiate_purchase(token, "p001", 500, requesting_customer_id="c777")

        result = guardrail.confirm_purchase("m_default", "p001", 500, "order_pending_test_3", requesting_customer_id="c777")
        assert result["status"] == "failed_verification"
        assert guardrail.get_pending_purchase("order_pending_test_3") is not None  # still there, retryable
    finally:
        razorpay_rest.create_order = orig_create_order
        razorpay_rest.captured_amount_inr = orig_captured


if __name__ == "__main__":
    test_clean_purchase_success()
    test_exceeds_mandate_blocked_before_razorpay()
    test_expired_mandate_blocked()
    test_single_use_mandate_reused_blocked()
    test_seeded_settlement_mismatch_not_silently_success()
    test_cumulative_spend_second_purchase_blocked()
    test_concurrent_purchases_never_exceed_mandate_limit()
    test_confirm_purchase_idempotent_does_not_double_count()
    test_confirm_purchase_refunds_when_mandate_cant_absorb_captured_payment()
    test_confirm_purchase_reports_refund_failure_honestly()
    test_shared_mandate_usable_by_any_customer()
    test_owned_mandate_only_usable_by_its_owner()
    test_owned_mandate_rejected_for_a_different_customer()
    test_owned_mandate_visible_to_its_owner_via_get_mandate_state()
    test_concurrent_mandate_issuance_never_loses_a_mandate()
    test_concurrent_confirm_of_the_same_order_never_double_counts_spend()
    print("All Guardrail tests passed.")
