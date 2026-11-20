"""MCP server: lets ANY MCP-capable AI agent (Claude Desktop, Claude Code, ...) order from Bolt Burger.

MCP = Model Context Protocol, a standard way for an AI app to discover and call
external tools. The agent app launches this file as a subprocess and talks to
it over stdin/stdout (JSON-RPC). It asks "what tools do you have?", gets back
the names + descriptions + JSON schemas below, and the model decides when to call them.

The docstrings ARE the tool descriptions the model reads - so they're written for an LLM.

Register with Claude Code (from the project folder):
    claude mcp add bolt-burger -- .venv/Scripts/python -m zappd.mcp_server
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from zappd import agent_tools
from zappd.models import CartLine

server = MCPServer(
    "bolt-burger",
    instructions=(
        "Order food from Bolt Burger. Workflow: search_menu to find item IDs -> get_item to see required "
        "choices (doneness, combo side/drink, milk) -> validate_order -> confirm with the user -> place_order. "
        "Never guess a required choice; ask the user."
    ),
)


@server.tool()
def search_menu(query: str) -> list[dict[str, Any]]:
    """Search the Bolt Burger menu by name or slang (e.g. 'number 3', 'large fry', 'coke').
    Returns matching items with their item_id, price, and whether they are available right now."""
    return agent_tools.search_menu(query)


@server.tool()
def get_item(item_id: str) -> dict[str, Any]:
    """Get full details for one item_id: modifier groups (which are REQUIRED, with modifier_ids
    and prices) and, for combos, the side/drink slots and allowed item_ids."""
    return agent_tools.get_item(item_id)


@server.tool()
def validate_order(lines: list[CartLine]) -> dict[str, Any]:
    """Check a proposed order WITHOUT placing it. Returns ok, exact totals in cents, and issues.
    Each issue has a code (e.g. MISSING_REQUIRED_MODIFIER), a message saying how to fix it,
    and a question you can ask the user."""
    return agent_tools.validate_order(lines)


@server.tool()
def place_order(lines: list[CartLine], idempotency_key: str) -> dict[str, Any]:
    """Place the order with the restaurant. Only call after the user confirmed the order and its total.
    idempotency_key: any unique string for this order; reuse the SAME key if you retry, so the
    order is never placed twice. Invalid orders are rejected with issues instead of being placed."""
    return agent_tools.place_order(lines, idempotency_key, customer_id="mcp-agent")


if __name__ == "__main__":
    server.run()  # stdio transport
