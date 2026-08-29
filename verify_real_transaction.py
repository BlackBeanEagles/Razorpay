"""One-time manual proof that the real Razorpay MCP Server hookup works end to end
(BUILD_SPEC.md section 10: "one manual test-mode transaction working end to end").

What this script does automatically, via real MCP tool calls:
  1. Creates a real test-mode order through the official razorpay-mcp-server (create_order).
  2. Writes a local checkout.html wired to that real order ID.

What it deliberately leaves to you, the human, in the browser:
  3. Clicking "Pay" and entering Razorpay's published test-mode card (4111 1111 1111 1111,
     any future expiry, any CVV) into Razorpay's own hosted Checkout UI. Card entry isn't
     something this script automates.

Then, back here:
  4. Calls fetch_order_payments (a real MCP tool) to confirm the payment captured, and prints
     the real order ID and payment ID -- both lookup-able in your Razorpay Dashboard (Test Mode).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from guardrail import razorpay_mcp_client as mcp_client

CHECKOUT_HTML_PATH = os.path.join(os.path.dirname(__file__), "checkout_demo.html")

DEMO_PRODUCT_ID = "p001"
DEMO_AMOUNT_INR = 1799


def write_checkout_html(order_id: str, key_id: str, amount_inr: int):
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Guardrail real-transaction demo</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:3rem auto;text-align:center">
<h2>Shelf + Parity + Guardrail</h2>
<p>Real Razorpay test-mode order: <code>{order_id}</code></p>
<p>Amount: Rs. {amount_inr}</p>
<button id="pay-btn" style="font-size:1.1rem;padding:.6rem 1.4rem;cursor:pointer">Pay with Razorpay (test mode)</button>
<p id="result" style="margin-top:2rem;font-weight:bold"></p>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay-btn').onclick = function () {{
  var options = {{
    key: "{key_id}",
    amount: {amount_inr * 100},
    currency: "INR",
    order_id: "{order_id}",
    name: "Guardrail Demo Merchant",
    description: "Wireless Earbuds X200 (test mode)",
    handler: function (response) {{
      document.getElementById('result').innerText =
        "Paid. payment_id=" + response.razorpay_payment_id;
    }},
    theme: {{ color: "#3b82f6" }}
  }};
  var rzp = new Razorpay(options);
  rzp.open();
}};
</script>
</body></html>"""
    with open(CHECKOUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    if not mcp_client.MCP_AVAILABLE:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set to real test-mode values in .env. Aborting.")
        sys.exit(1)

    print("Connecting to the real razorpay-mcp-server via Docker...")
    tools = mcp_client.list_available_tools()
    print(f"Connected. {len(tools)} tools available (e.g. {tools[:5]}...)")

    print(f"Creating a real test-mode order for {DEMO_PRODUCT_ID} at Rs.{DEMO_AMOUNT_INR} via MCP create_order...")
    order = mcp_client.create_order_via_mcp(DEMO_PRODUCT_ID, DEMO_AMOUNT_INR)
    order_id = order.get("id")
    if not order_id:
        print(f"create_order did not return an order id. Raw response: {json.dumps(order, indent=2)}")
        sys.exit(1)
    print(f"Real order created: {order_id}")

    write_checkout_html(order_id, mcp_client.RAZORPAY_KEY_ID, DEMO_AMOUNT_INR)
    print(f"Checkout page written to {CHECKOUT_HTML_PATH}")
    print("Open it, click 'Pay with Razorpay', and enter Razorpay's test-mode card:")
    print("  Card number: 4111 1111 1111 1111  |  Expiry: any future date  |  CVV: any 3 digits")
    print("Then re-run this script with --verify to confirm the payment via MCP.")


def verify():
    if not mcp_client.MCP_AVAILABLE:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. Aborting.")
        sys.exit(1)
    order_id = input("Paste the order_id from the last run: ").strip()
    payments = mcp_client.fetch_order_payments_via_mcp(order_id)
    print(json.dumps(payments, indent=2))
    items = payments.get("items", [])
    captured = [p for p in items if p.get("status") == "captured"]
    if captured:
        p = captured[0]
        print(f"\nVERIFIED via real MCP call: payment_id={p['id']} amount_inr={p['amount']/100} status=captured")
        print(f"Look this up in your Razorpay Dashboard -> Test Mode -> Orders -> {order_id}")
    else:
        print("\nNo captured payment found yet for this order.")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        main()
