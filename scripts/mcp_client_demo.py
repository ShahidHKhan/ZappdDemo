"""Act like Claude Desktop: launch our MCP server as a subprocess and talk to it.

    python scripts/mcp_client_demo.py

No LLM here - we play the agent's role by hand, so you can see the raw protocol:
  1. initialize       (handshake)
  2. tools/list       (discover tools: names, descriptions, JSON schemas)
  3. tools/call       (call one with JSON arguments, get JSON back)
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "zappd.mcp_server"], cwd=str(ROOT))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = await session.list_tools()
        print("== tools/list: what the agent's model sees ==")
        for tool in tools.tools:
            print(f"\n{tool.name}: {tool.description}")
            print("  input schema:", json.dumps(tool.input_schema)[:300], "...")

        print("\n== tools/call search_menu('number 3') ==")
        result = await session.call_tool("search_menu", {"query": "number 3"})
        for block in result.content[:3]:  # a list result comes back as one content block per element
            print(block.text)

        print("\n== tools/call validate_order (combo with no drink) ==")
        lines = [{"item_id": "combo_3", "combo_choices": [{"slot": "side", "item_id": "onion_rings"}]}]
        result = await session.call_tool("validate_order", {"lines": lines})
        issues = json.loads(result.content[0].text)["issues"]
        print(json.dumps(issues, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
