"""Live verification: reconciles every real, captured live-ledger purchase against Razorpay's
OWN current record for that order, fetched fresh right now -- not the amount our ledger recorded
at confirm-time. This is a genuinely independent check: guardrail/ledger.json's
verification.amount_settled_inr is a snapshot taken the moment confirm_purchase() ran, so it
cannot see anything that happened to that payment afterward -- e.g. a refund issued by hand,
straight in the Razorpay Dashboard, bypassing this app entirely. reconciliation.py's own batch
(ledger vs. the synthetic seed) has no way to catch that, because it never asks Razorpay
anything -- it only compares files this app already wrote to itself.

Two exception types worth telling apart:
- overcharge_drift: Razorpay's real captured total is now HIGHER than this order was ever meant
  to charge. Unambiguous -- the only correct fix is a refund of the difference, so this is the
  one type remediate_overcharge() is allowed to act on.
- undercharge_drift / refund_not_reflected: Razorpay's real total is now LOWER than expected, or
  a ledger entry already marked "refunded" doesn't actually show as refunded at Razorpay. NOT
  auto-remediated -- the right fix depends on *why* (a legitimate manual refund? a chargeback?
  a dispute?), which this system has no way to know, and re-charging a customer automatically is
  not something to ever do without a human deciding to.

Uses guardrail/razorpay_rest.py -- the same real REST client the live app's actual checkout flow
already uses (see that module's docstring) -- not guardrail/razorpay_mcp_client.py, which needs a
local Docker container and is deliberately kept out of the live request path.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail, razorpay_rest
from audit.audit_log import log_event

AMOUNT_TOLERANCE_INR = 1


def verify_order_against_razorpay(razorpay_order_id: str) -> dict:
    """Fetches this order's payments straight from Razorpay right now. Returns
    {"razorpay_captured_amount_inr": float, "razorpay_payment_status": str, "captured_payment_id": str|None}.
    status is one of "captured", "partially_refunded", "refunded", "no_capture".

    Looks at both "captured" and "refunded" payment statuses -- Razorpay only flips a payment's
    own status to "refunded" once it's been refunded in FULL; a partial refund leaves status as
    "captured" with a nonzero amount_refunded. Filtering to status == "captured" alone would
    silently miss every fully-refunded payment."""
    payments = razorpay_rest.fetch_order_payments(razorpay_order_id)
    relevant = [p for p in payments if p.get("status") in ("captured", "refunded")]
    if not relevant:
        return {"razorpay_captured_amount_inr": 0.0, "razorpay_payment_status": "no_capture", "captured_payment_id": None}

    payment = relevant[0]
    amount_captured = payment["amount"] / 100
    amount_refunded = payment.get("amount_refunded", 0) / 100
    net = amount_captured - amount_refunded
    if payment["status"] == "refunded" or amount_refunded >= amount_captured:
        status = "refunded"
    elif amount_refunded > 0:
        status = "partially_refunded"
    else:
        status = "captured"
    return {"razorpay_captured_amount_inr": net, "razorpay_payment_status": status, "captured_payment_id": payment["id"]}


def check_live_drift() -> dict:
    """Checks every live-ledger order this system believes is currently holding (or has
    released) captured money -- ledger status "success" or "refunded" -- against Razorpay's real,
    current record. Returns {"checked", "clean", "exceptions", "exception_counts", ...}."""
    if not razorpay_rest.REAL_CHECKOUT_AVAILABLE:
        return {
            "checked": 0, "clean": 0, "exceptions": [], "exception_counts": {},
            "unavailable_reason": "No real Razorpay test-mode credentials configured -- live "
                                   "verification needs RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET in .env.",
        }

    checked, clean, exceptions = 0, 0, []
    for entry in guardrail.load_ledger():
        if entry.get("status") not in ("success", "refunded"):
            continue
        order_id = entry.get("razorpay_order_id")
        if not order_id:
            continue
        checked += 1
        expected_inr = entry.get("expected_amount_inr")
        live = verify_order_against_razorpay(order_id)

        if entry["status"] == "refunded":
            # Ledger already believes this was refunded -- clean only if Razorpay agrees there's
            # nothing meaningfully still captured against it.
            if live["razorpay_payment_status"] in ("refunded", "no_capture"):
                clean += 1
            else:
                exceptions.append({
                    "order_id": order_id, "type": "refund_not_reflected", "source": "razorpay_live_check",
                    "reason": f"Ledger shows this order refunded, but Razorpay currently shows "
                              f"{live['razorpay_captured_amount_inr']} INR still captured ({live['razorpay_payment_status']}).",
                    "razorpay_captured_amount_inr": live["razorpay_captured_amount_inr"],
                    "razorpay_payment_status": live["razorpay_payment_status"], "remediable": False,
                })
            continue

        diff = None if expected_inr is None else live["razorpay_captured_amount_inr"] - expected_inr
        if diff is None or abs(diff) <= AMOUNT_TOLERANCE_INR:
            clean += 1
        elif diff > AMOUNT_TOLERANCE_INR:
            exceptions.append({
                "order_id": order_id, "type": "overcharge_drift", "source": "razorpay_live_check",
                "reason": f"Razorpay currently shows {live['razorpay_captured_amount_inr']} INR captured for "
                          f"this order, but only {expected_inr} INR was ever meant to be charged (diff +{diff:.2f} INR).",
                "expected_amount_inr": expected_inr, "razorpay_captured_amount_inr": live["razorpay_captured_amount_inr"],
                "captured_payment_id": live["captured_payment_id"], "remediable": True,
            })
        else:
            exceptions.append({
                "order_id": order_id, "type": "undercharge_drift", "source": "razorpay_live_check",
                "reason": f"Razorpay currently shows only {live['razorpay_captured_amount_inr']} INR captured for this "
                          f"order ({live['razorpay_payment_status']}), but {expected_inr} INR was expected (diff {diff:.2f} INR) "
                          f"-- needs manual review, not auto-remediable.",
                "expected_amount_inr": expected_inr, "razorpay_captured_amount_inr": live["razorpay_captured_amount_inr"],
                "razorpay_payment_status": live["razorpay_payment_status"], "remediable": False,
            })

    exception_counts = {}
    for e in exceptions:
        exception_counts[e["type"]] = exception_counts.get(e["type"], 0) + 1

    result = {"checked": checked, "clean": clean, "exceptions": exceptions, "exception_counts": exception_counts}
    log_event("reconciliation", "live_drift_check", {},
              {"checked": checked, "clean": clean, "exception_counts": exception_counts},
              "ok" if not exceptions else "flagged")
    return result


def remediate_overcharge(order_id: str, admin_username: str) -> dict:
    """Issues a real (test-mode) refund for exactly the amount by which Razorpay's current
    captured total exceeds what this order was ever meant to charge. Re-verifies fresh against
    Razorpay right before acting, rather than trusting a check_live_drift() result computed even
    a few seconds earlier -- so a refund already issued by someone else in the meantime (or a
    second admin double-clicking the same button) can't double-refund the same order.

    The whole re-verify-then-refund sequence runs under guardrail's own store-wide lock: the
    re-verify alone isn't enough to make double-clicking safe, since two concurrent calls could
    both fetch the same "not yet refunded" state from Razorpay before either one's refund_payment
    call lands -- both would then see the identical diff and both would issue a real refund.
    Serializing here closes that window the same way every other money-moving path in this
    codebase (guardrail.py's _atomic_reserve_spend, _atomic_confirm_and_reserve) already does."""
    with guardrail.mandate_store_lock():
        entry = next((e for e in guardrail.load_ledger() if e.get("razorpay_order_id") == order_id), None)
        if entry is None:
            return {"status": "error", "detail": f"No ledger entry found for order {order_id!r}."}
        expected_inr = entry.get("expected_amount_inr")
        if expected_inr is None:
            return {"status": "error", "detail": "This order has no expected amount on record -- can't compute a refund."}

        live = verify_order_against_razorpay(order_id)
        diff = live["razorpay_captured_amount_inr"] - expected_inr
        if diff <= AMOUNT_TOLERANCE_INR:
            return {
                "status": "no_action_needed",
                "detail": f"Re-checked Razorpay just now -- no overcharge remains (currently "
                          f"{live['razorpay_captured_amount_inr']} INR captured, {expected_inr} INR expected). "
                          f"Someone may have already fixed this.",
            }
        if not live["captured_payment_id"]:
            return {"status": "error", "detail": "No captured payment id found on Razorpay's side to refund against."}

        refund = razorpay_rest.refund_payment(live["captured_payment_id"], diff)
        log_event(
            "reconciliation", "live_drift_remediation",
            {"order_id": order_id, "admin": admin_username, "payment_id": live["captured_payment_id"], "refund_amount_inr": diff},
            {"razorpay_refund_id": refund.get("id"), "razorpay_status": refund.get("status")}, "ok",
        )
        return {"status": "refunded", "refund_amount_inr": diff, "razorpay_refund_id": refund.get("id"), "raw": refund}
