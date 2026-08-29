"""One-off demo driver: registers a fresh AI buyer over the real MCP protocol, creates a
mandate, searches, and initiates a real purchase -- prints the checkout details as JSON so a
human can complete real Razorpay Checkout in a browser, after which _demo_confirm.py (also run
over the real protocol) closes the loop. Not part of the test suite -- a one-time demo artifact."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "techbazaar_mcp_server.py")


async def main():
    params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            def _content(result):
                return json.loads(result.content[0].text)

            # A distinct name per run -- the Connected AI Agents dashboard panel shows
            # display_name directly, and a fixed "Live Demo Buyer" on every run produced
            # multiple identical-looking rows that read as copy-pasted fake data instead of
            # genuine separate activity.
            run_label = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
            buyer = _content(await session.call_tool("register_ai_buyer", {"name": f"Live Demo Buyer ({run_label})"}))
            mandate = _content(await session.call_tool(
                "create_mandate", {"ai_buyer_id": buyer["ai_buyer_id"], "max_amount_inr": 5000}))
            search = _content(await session.call_tool(
                "search_catalog", {"query": "wireless earbuds", "max_budget_inr": 2000}))
            product = search["matches"][0]
            outcome = _content(await session.call_tool(
                "purchase",
                {"product_id": product["product_id"], "amount_inr": product["price_inr"],
                 "ai_buyer_id": buyer["ai_buyer_id"], "mandate_id": mandate["mandate_id"]}))

            print(json.dumps({
                "ai_buyer_id": buyer["ai_buyer_id"], "mandate_id": mandate["mandate_id"],
                "product_id": product["product_id"], "amount_inr": product["price_inr"],
                "checkout": outcome.get("checkout"), "status": outcome["status"],
            }))


if __name__ == "__main__":
    asyncio.run(main())
