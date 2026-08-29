"""Real client for Razorpay's official MCP Server (razorpay/razorpay-mcp-server), run locally
via Docker in stdio mode. Talks real JSON-RPC/MCP protocol over a subprocess's stdin/stdout --
this is not a REST wrapper, it goes through the same MCP tool surface an AI agent would use.

NOT used by the running app (api/server.py, guardrail/guardrail.py, or any route) -- the human-
verified real-Checkout flow now goes through the plainer guardrail/razorpay_rest.py instead
(see that module's own docstring). This module's only remaining importer is the standalone
verify_real_transaction.py script (a one-time manual proof, run directly, not part of any
request path). Kept because it's still a genuine, working demonstration of the MCP integration
route -- not because anything in the live app depends on it.

Notes on test-mode limits (see README section on the real hookup for the full explanation):
- create_order is a genuine, unrestricted MCP tool call -- the order IDs it returns are real
  and visible in the Razorpay Dashboard (Test Mode).
- Completing a payment against that order requires an actual payment method (card/UPI/etc.).
  MCP tools deliberately don't accept raw card data, and Razorpay's direct server-to-server
  card-payment API requires a special support-arranged account (confirmed via their docs) --
  it is not a plain self-serve REST call, so this module does not attempt to fake it. The real,
  documented way to complete a test-mode payment is Razorpay's actual Checkout UI with their
  published static test card; see verify_real_transaction.py for a scripted one-time proof
  that drives that real checkout flow in a browser and then re-verifies the result via MCP.
- Razorpay does not generate real bank settlements for test-mode transactions (settlement is a
  live-mode banking concept), so Guardrail's reconciliation check uses fetch_order_payments
  (a real MCP tool) to independently re-fetch the captured amount from Razorpay, instead of
  the Settlements tool the spec describes for live mode.
"""
import asyncio
import atexit
import os
import threading
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
_PLACEHOLDER_KEY = "rzp_test_xxxxxxxxxxxx"

MCP_AVAILABLE = bool(
    RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
    and RAZORPAY_KEY_ID != _PLACEHOLDER_KEY
    and RAZORPAY_KEY_ID.startswith("rzp_test_")
)

class _MCPSession:
    """Runs one long-lived `docker run ... razorpay/mcp` subprocess and MCP session
    on a background event loop, so repeated tool calls don't each pay Docker startup cost."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session = None
        self._stack = None
        self._connect_error = None
        ready = threading.Event()
        asyncio.run_coroutine_threadsafe(self._connect(ready), self._loop)
        if not ready.wait(timeout=45):
            raise TimeoutError("Timed out starting the razorpay-mcp-server Docker container.")
        if self._connect_error:
            raise self._connect_error

    async def _connect(self, ready: threading.Event):
        try:
            self._stack = AsyncExitStack()
            server_params = StdioServerParameters(
                command="docker",
                args=["run", "--rm", "-i", "-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET", "razorpay/mcp"],
                env={**os.environ, "RAZORPAY_KEY_ID": RAZORPAY_KEY_ID, "RAZORPAY_KEY_SECRET": RAZORPAY_KEY_SECRET},
            )
            read, write = await self._stack.enter_async_context(stdio_client(server_params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
        except Exception as e:  # noqa: BLE001 - surfaced to the constructing thread
            self._connect_error = e
        finally:
            ready.set()

    def call_tool(self, name: str, arguments: dict, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop)
        return future.result(timeout=timeout)

    def list_tools(self, timeout: float = 15.0):
        future = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        return future.result(timeout=timeout)

    def close(self):
        async def _close():
            await self._stack.aclose()

        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


_session_lock = threading.Lock()
_session_singleton: _MCPSession | None = None


def get_session() -> _MCPSession:
    global _session_singleton
    with _session_lock:
        if _session_singleton is None:
            _session_singleton = _MCPSession()
            atexit.register(_session_singleton.close)
        return _session_singleton


def _tool_result_to_dict(result) -> dict:
    """MCP tool results carry their payload as one or more content blocks; the Razorpay
    server returns a single JSON text block for these tools."""
    import json as _json

    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return _json.loads(block.text)
            except ValueError:
                return {"raw_text": block.text}
    return {}


def list_available_tools() -> list:
    session = get_session()
    tools = session.list_tools()
    return [t.name for t in tools.tools]


def create_order_via_mcp(product_id: str, amount_inr: int) -> dict:
    """Creates a real Razorpay test-mode order through the official MCP Server."""
    session = get_session()
    result = session.call_tool("create_order", {
        "amount": amount_inr * 100,
        "currency": "INR",
        "receipt": f"guardrail_{product_id}",
    })
    payload = _tool_result_to_dict(result)
    return payload


def fetch_order_payments_via_mcp(order_id: str) -> dict:
    session = get_session()
    result = session.call_tool("fetch_order_payments", {"order_id": order_id})
    return _tool_result_to_dict(result)


def create_and_capture_order(product_id: str, amount_inr: int) -> dict:
    """Creates a real order via MCP. Honesty note: this does NOT complete a payment --
    Razorpay has no self-serve API for that (see module docstring), so 'capture' genuinely
    requires the real Checkout flow in verify_real_transaction.py. Guardrail's own
    three-way reconciliation will correctly show a mismatch (0 captured) for any order
    created here that a human hasn't actually paid through checkout -- that's real
    verification working as designed, not a bug."""
    order_payload = create_order_via_mcp(product_id, amount_inr)
    order_id = order_payload.get("id")
    if not order_id:
        return {"status": "failed", "razorpay_order_id": None, "amount_charged_inr": None,
                "raw_order_response": order_payload}

    return {
        "status": "order_created",
        "razorpay_order_id": order_id,
        "amount_charged_inr": amount_inr,
        "raw_order_response": order_payload,
    }


def get_settlement(razorpay_order_id: str, simulate_mismatch: bool = False) -> dict:
    """Test mode has no real bank settlement cycle, so this reconciles against the order's
    actual captured payments via the real fetch_order_payments MCP tool instead."""
    payments = fetch_order_payments_via_mcp(razorpay_order_id)
    items = payments.get("items", []) if isinstance(payments, dict) else []
    captured_total = sum(p.get("amount", 0) for p in items if p.get("status") in ("captured", "authorized")) / 100
    if simulate_mismatch:
        captured_total += 1
    return {"status": "settled" if items else "not_found", "settled_amount_inr": captured_total or None}
