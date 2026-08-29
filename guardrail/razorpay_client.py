"""Deterministic mock Razorpay client used by Guardrail's automated purchase flow (agent-driven
buys, the test suite, and all batch scripts) -- always mock, regardless of what's in .env.

This is deliberate, not a placeholder waiting to be "upgraded": a real Razorpay payment can
only be captured by a human going through actual Checkout (card entry, OTP, etc.) -- there is
no way for a headless backend call to complete one (direct server-to-server card payment needs
a special Razorpay-support-arranged account, confirmed via their docs; see
razorpay_mcp_client.py's module docstring for the full explanation). An earlier version of this
module auto-switched to making real REST calls whenever real-looking credentials showed up in
.env -- which meant create_and_capture_order() would create a genuine Razorpay order but
get_settlement() could never find a captured payment against it, so every automated purchase
silently started failing verification the moment real credentials were added. That's a
misleading, hard-to-diagnose way to fail, so this module no longer does it.

The real Razorpay integration is exercised deliberately and separately by
verify_real_transaction.py, via razorpay_mcp_client.py -- that path creates one real test-mode
order through the official MCP server and walks a human through completing it via Razorpay's
actual Checkout UI. That's the only way to get a genuinely captured, verifiable payment.
"""
import random
import string

# In-memory store: order_id -> amount charged (paise-free, INR), so get_settlement can echo
# back a consistent figure across calls within one process.
_MOCK_ORDERS = {}


def _mock_id(prefix: str) -> str:
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=14))
    return f"{prefix}_{suffix}"


def create_and_capture_order(product_id: str, amount_inr: int) -> dict:
    """Simulates creating a test-mode order and capturing payment for it."""
    order_id = _mock_id("order")
    _MOCK_ORDERS[order_id] = amount_inr
    return {
        "status": "success",
        "razorpay_order_id": order_id,
        "amount_charged_inr": amount_inr,
    }


def get_settlement(razorpay_order_id: str, simulate_mismatch: bool = False) -> dict:
    """Simulates the reconciliation lookup: what amount actually settled for this order."""
    charged = _MOCK_ORDERS.get(razorpay_order_id)
    if charged is None:
        return {"status": "not_found", "settled_amount_inr": None}
    settled = charged + random.randint(50, 150) if simulate_mismatch else charged
    return {"status": "settled", "settled_amount_inr": settled}
