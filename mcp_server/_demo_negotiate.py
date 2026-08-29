"""One-off demo driver: an external AI buyer negotiating a price over the real MCP protocol --
register -> mandate -> search -> lowball offer -> counter -> accept the counter -> purchase at
the negotiated price, not the listed price. Not part of the test suite -- a live demo artifact,
mirroring verify_real_mcp_connection.py's pattern (real spawned subprocess, real MCP client)."""
import asyncio
import json
import os
import sys

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

            buyer = _content(await session.call_tool("register_ai_buyer", {"name": "Negotiator Agent"}))
            print("register_ai_buyer:", buyer)
            mandate = _content(await session.call_tool(
                "create_mandate", {"ai_buyer_id": buyer["ai_buyer_id"], "max_amount_inr": 5000}))
            print("create_mandate:", mandate["mandate_id"])

            search = _content(await session.call_tool(
                "search_catalog", {"query": "wireless earbuds", "max_budget_inr": 2000}))
            product = search["matches"][0]
            print("search_catalog:", product["name"], product["price_inr"])

            opening_offer = round(product["price_inr"] * 0.55)  # a real lowball, ~45% off
            r1 = _content(await session.call_tool(
                "negotiate_price",
                {"product_id": product["product_id"], "ai_buyer_id": buyer["ai_buyer_id"],
                 "offered_price_inr": opening_offer, "round_number": 1}))
            print(f"negotiate_price round 1 (offered Rs.{opening_offer}):", r1["verdict"], "-", r1["reason"])

            if r1["verdict"] == "counter":
                r2 = _content(await session.call_tool(
                    "negotiate_price",
                    {"product_id": product["product_id"], "ai_buyer_id": buyer["ai_buyer_id"],
                     "offered_price_inr": r1["counter_price_inr"], "round_number": 2}))
                print(f"negotiate_price round 2 (accepting counter Rs.{r1['counter_price_inr']}):", r2["verdict"], "-", r2["reason"])
                agreed_price = r2.get("agreed_price_inr", product["price_inr"])
            else:
                agreed_price = r1.get("agreed_price_inr", product["price_inr"])

            print(f"Purchasing at negotiated price Rs.{agreed_price} (listed price was Rs.{product['price_inr']})")
            outcome = _content(await session.call_tool(
                "purchase",
                {"product_id": product["product_id"], "amount_inr": agreed_price,
                 "ai_buyer_id": buyer["ai_buyer_id"], "mandate_id": mandate["mandate_id"]}))
            print("purchase:", outcome["status"], "-", outcome.get("reason"))


if __name__ == "__main__":
    asyncio.run(main())
