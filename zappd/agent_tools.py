"""The four capabilities we expose to the outside world.

These are plain Python functions returning plain dicts. Three different
"transports" wrap the SAME functions:
    api.py         -> REST endpoints   (for any HTTP client)
    mcp_server.py  -> MCP tools        (for Claude Desktop / Claude Code / any MCP agent)
    agent_demo.py  -> Claude API tools (we run the agent loop ourselves)

That's the "neutral layer": one source of truth, many agent platforms.

Error messages are written for an LLM reader: they say what went wrong AND
which tool to call next, so the agent can fix itself instead of giving up.
"""

from typing import Any

from zappd.catalog import get_group, load_catalog, load_rules, local_now, search_items
from zappd.models import Cart, CartLine
from zappd.pos import MockPOS
from zappd.validator import effective_min_selections, unavailable_reason, validate_cart

POS = MockPOS()  # one shared POS for this process (REST + MCP + demo all see the same orders)


def search_menu(query: str) -> list[dict[str, Any]]:
    """Fuzzy-search the menu. Returns the best matching items with their IDs."""
    catalog, rules, now = load_catalog(), load_rules(), local_now(load_catalog())
    return [
        {
            "item_id": item.id,
            "name": item.name,
            "category": item.category,
            "price": f"${item.price_cents / 100:.2f}",
            "available_now": unavailable_reason(item, rules, now) is None,
            "match_score": round(score),
        }
        for item, score in search_items(catalog, query)
    ]


def get_item(item_id: str) -> dict[str, Any]:
    """Full details for one item: modifier groups (with REQUIRED flags after rules), combo slots."""
    catalog, rules = load_catalog(), load_rules()
    item = next((i for i in catalog.items if i.id == item_id), None)
    if item is None:
        return {"error": f"No item with id '{item_id}'. Call search_menu to find valid item IDs."}

    # For a combo, the customizable item is the main burger/sandwich.
    target = next((i for i in catalog.items if item.combo and i.id == item.combo.main_item_id), item)
    groups = []
    for group_id in target.modifier_group_ids:
        group = get_group(catalog, group_id)
        if group is None:
            continue
        min_sel, _ = effective_min_selections(target, group, rules)
        groups.append({
            "group_id": group.id,
            "name": group.name,
            "required": min_sel > 0,
            "min_selections": min_sel,
            "max_selections": group.max_selections,
            "options": [
                {"modifier_id": m.id, "name": m.name, "price_delta": f"${m.price_delta_cents / 100:.2f}"}
                for m in group.modifiers
            ],
        })

    reason = unavailable_reason(item, rules, local_now(catalog))
    return {
        "item_id": item.id,
        "name": item.name,
        "price": f"${item.price_cents / 100:.2f}",
        "available_now": reason is None,
        "unavailable_reason": reason,
        "modifier_groups": groups,
        "combo_slots": [
            {"slot": s.name, "choose_exactly_one_of": s.allowed_item_ids} for s in item.combo.slots
        ] if item.combo else [],
    }


def validate_order(lines: list[CartLine]) -> dict[str, Any]:
    """Check a cart without placing it. Returns issues (with fix hints) and exact totals."""
    catalog = load_catalog()
    result = validate_cart(Cart(lines=lines), catalog, load_rules(), local_now(catalog))
    return result.model_dump()


def place_order(lines: list[CartLine], idempotency_key: str, customer_id: str = "agent") -> dict[str, Any]:
    """Validate, then submit to the POS. Never places an invalid order."""
    catalog = load_catalog()
    result = validate_cart(Cart(lines=lines), catalog, load_rules(), local_now(catalog))
    if not result.ok:
        return {
            "placed": False,
            "issues": [i.model_dump() for i in result.issues],
            "hint": "Fix the issues (ask the customer if needed), then call place_order again.",
        }
    order = POS.submit_order(customer_id, result.lines, result.total_cents, idempotency_key)
    return {"placed": True, "pos_order_id": order.pos_order_id, "total": f"${result.total_cents / 100:.2f}",
            "lines": [line.description for line in result.lines]}
