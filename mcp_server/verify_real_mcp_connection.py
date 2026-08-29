"""One-time manual proof that techbazaar_mcp_server.py is a genuine, connectable MCP server --
spawns it as a real subprocess over stdio (exactly how an external MCP client, e.g. Claude
Desktop or another agent framework, would) and drives a full buy flow purely through the MCP
protocol's tools/list and tools/call, not by importing the server's Python functions directly.

Run: python mcp_server/verify_real_mcp_connection.py
"""
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

            tools = await session.list_tools()
            print("Tools discovered over the real MCP protocol:", [t.name for t in tools.tools])

            def _content(result):
                return json.loads(result.content[0].text)

            buyer = _content(await session.call_tool("register_ai_buyer", {"name": "Protocol Verification Agent"}))
            print("register_ai_buyer:", buyer)

            mandate = _content(await session.call_tool(
                "create_mandate", {"ai_buyer_id": buyer["ai_buyer_id"], "max_amount_inr": 2500}))
            print("create_mandate:", mandate["mandate_id"], "owner:", mandate["state"]["owner_customer_id"])

            search = _content(await session.call_tool(
                "search_catalog", {"query": "wireless earbuds", "max_budget_inr": 2000}))
            product = search["matches"][0]
            print("search_catalog:", product["name"], product["price_inr"])

            fairness = _content(await session.call_tool(
                "check_price_fairness",
                {"product_id": product["product_id"], "ai_buyer_id": buyer["ai_buyer_id"], "offered_price_inr": product["price_inr"]}))
            print("check_price_fairness:", fairness["verdict"])

            outcome = _content(await session.call_tool(
                "purchase",
                {"product_id": product["product_id"], "amount_inr": product["price_inr"],
                 "ai_buyer_id": buyer["ai_buyer_id"], "mandate_id": mandate["mandate_id"]}))
            print("purchase:", outcome["status"], "-", outcome.get("reason"))


if __name__ == "__main__":
    asyncio.run(main())
