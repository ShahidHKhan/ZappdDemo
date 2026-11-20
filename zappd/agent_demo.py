"""An AI agent orders from Bolt Burger using our tools - with the agent loop written out by hand.

    python -m zappd.agent_demo
    python -m zappd.agent_demo "a double bolt medium rare with bacon and a large fry"

This is what Claude Desktop / ChatGPT / any agent platform does internally when
it uses our MCP server. Here WE run that loop, so you can watch every step:

    1. send Claude: the task + the tool definitions (name, description, JSON schema)
    2. Claude replies with stop_reason="tool_use" and one or more tool_use blocks
    3. we run those tools (plain Python functions in agent_tools.py)
    4. we send the results back as tool_result blocks
    5. repeat until Claude replies without calling a tool (stop_reason="end_turn")

Claude never touches our database or the POS. It can only ASK us to run a tool,
and every tool goes through the validator.
"""

import json
import sys
import uuid
from typing import Any

from zappd import agent_tools
from zappd.llm import FALLBACK_BETA, FALLBACKS, MODEL, get_client
from zappd.models import CartLine

# ---- 1. Tool definitions: EXACTLY what Claude sees. Hand-written so you can read them.

CART_LINES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Canonical item id from search_menu"},
            "quantity": {"type": "integer", "minimum": 1},
            "modifier_ids": {"type": "array", "items": {"type": "string"},
                             "description": "Modifier ids from get_item. For combos, these apply to the main burger/sandwich."},
            "combo_choices": {
                "type": "array",
                "items": {"type": "object", "properties": {"slot": {"type": "string"}, "item_id": {"type": "string"}},
                          "required": ["slot", "item_id"]},
                "description": "Combos only: one choice per slot, e.g. {slot: 'drink', item_id: 'coke'}",
            },
        },
        "required": ["item_id", "quantity"],
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_menu",
        "description": "Search the Bolt Burger menu by name or slang ('number 3', 'large fry'). Returns item_ids, prices, availability.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_item",
        "description": "Full details for an item_id: modifier groups (REQUIRED ones flagged, with modifier_ids) and combo slots.",
        "input_schema": {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
    },
    {
        "name": "validate_order",
        "description": "Check an order without placing it. Returns exact totals and issues with fix hints.",
        "input_schema": {"type": "object", "properties": {"lines": CART_LINES_SCHEMA}, "required": ["lines"]},
    },
    {
        "name": "place_order",
        "description": "Place a validated order. Rejected with issues if invalid. Reuse the same idempotency_key on retries.",
        "input_schema": {
            "type": "object",
            "properties": {"lines": CART_LINES_SCHEMA, "idempotency_key": {"type": "string"}},
            "required": ["lines", "idempotency_key"],
        },
    },
]

SYSTEM = (
    "You are a personal assistant ordering food for your user from Bolt Burger using the tools. "
    "Look up items with search_menu and get_item; use only ids the tools return. Validate before placing. "
    "If something the user asked for is unavailable, leave it out and say so. If a required choice is "
    "missing and the user gave no preference, pick the most common option (medium doneness, regular fries, Coke) "
    "and say what you picked. Then place the order and report the order number and total."
)


# ---- 3. Run a tool Claude asked for. Returns JSON text for the tool_result block.

def run_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Returns (result_json, is_error)."""
    try:
        if name == "search_menu":
            result: Any = agent_tools.search_menu(args["query"])
        elif name == "get_item":
            result = agent_tools.get_item(args["item_id"])
        elif name == "validate_order":
            result = agent_tools.validate_order([CartLine.model_validate(x) for x in args["lines"]])
        elif name == "place_order":
            lines = [CartLine.model_validate(x) for x in args["lines"]]
            result = agent_tools.place_order(lines, args["idempotency_key"], customer_id="agent-demo")
        else:
            return json.dumps({"error": f"Unknown tool {name}"}), True
    except Exception as e:  # bad arguments etc. -> tell Claude, let it retry
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
    return json.dumps(result), False


# ---- 2/4/5. The agent loop.

def run_agent(task: str, max_turns: int = 12) -> None:
    client = get_client()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    print(f"USER: {task}\n")

    for _turn in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
            betas=[FALLBACK_BETA],
            fallbacks=FALLBACKS,
        )

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"CLAUDE: {block.text}\n")
            elif block.type == "tool_use":
                print(f"  -> tool call: {block.name}({json.dumps(block.input)})")

        if response.stop_reason == "refusal":
            print("Claude declined this request.")
            return

        # Claude's turn (including its tool_use blocks) goes into the history as-is.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            print(f"[done: stop_reason={response.stop_reason}, "
                  f"tokens in/out={response.usage.input_tokens}/{response.usage.output_tokens}]")
            return

        # Run every tool Claude asked for, send ALL results back in ONE user message.
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output, is_error = run_tool(block.name, block.input)
                print(f"  <- result: {output[:160]}{'...' if len(output) > 160 else ''}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})
        print()

    print("Stopped: too many turns.")


DEFAULT_TASK = (
    "Order lunch for my team: two #3 combos, one with a Coke and one with a Sprite, both with onion rings "
    "and no pickles. Also a Double Bolt medium rare with bacon, and a chocolate shake if they have it. "
    f"Use idempotency key {uuid.uuid4()}."
)

if __name__ == "__main__":
    from zappd.llm import LLMUnavailableError

    try:
        run_agent(" ".join(sys.argv[1:]) or DEFAULT_TASK)
    except LLMUnavailableError as e:
        sys.exit(str(e))
