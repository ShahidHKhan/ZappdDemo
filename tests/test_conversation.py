"""Conversation loop tests with a FAKE parser: we script what the "LLM" returns,
so we test our own logic (ask -> summarize -> place) without network or cost."""

from datetime import datetime

from zappd.conversation import OrderingService
from zappd.models import Cart, CartLine, ComboChoice
from zappd.parser import ParsedOrder, ParseStats
from zappd.pos import MockPOS

NOON_WEDNESDAY = datetime(2026, 11, 18, 12, 0)
NO_STATS = ParseStats(latency_s=0.0, input_tokens=0, output_tokens=0)


class ScriptedParser:
    """Returns pre-written ParsedOrders in order, and records what it was given."""

    def __init__(self, *outputs: ParsedOrder) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, Cart]] = []

    def __call__(self, text: str, cart: Cart, history: list[tuple[str, str]]) -> tuple[ParsedOrder, ParseStats]:
        self.calls.append((text, cart))
        return self.outputs.pop(0), NO_STATS


def burger(*mods: str) -> CartLine:
    return CartLine(item_id="bolt_classic", modifier_ids=list(mods))


def test_ask_then_summarize_then_place() -> None:
    parser = ScriptedParser(
        ParsedOrder(lines=[burger()], not_on_menu=[], customer_confirmed=False),
        ParsedOrder(lines=[burger("doneness_medium")], not_on_menu=[], customer_confirmed=False),
        ParsedOrder(lines=[burger("doneness_medium")], not_on_menu=[], customer_confirmed=True),
    )
    pos = MockPOS(pos_id_map={"bolt_classic": "1001"})
    service = OrderingService(parser=parser, pos=pos, clock=lambda: NOON_WEDNESDAY)

    reply1 = service.handle_message("+15550001", "a classic burger")
    assert "cooked" in reply1  # the doneness rule kicked in

    reply2 = service.handle_message("+15550001", "medium")
    assert "Total: $8.99" in reply2 and "Reply YES" in reply2
    assert parser.calls[1][1].lines == [burger()]  # the parser received the current cart

    reply3 = service.handle_message("+15550001", "yes")
    assert "Order placed! #POS-0001" in reply3
    assert pos.orders["POS-0001"].pos_item_ids == ["1001"]


def test_confirming_an_invalid_cart_does_not_place_it() -> None:
    combo_no_drink = CartLine(item_id="combo_3", combo_choices=[ComboChoice(slot="side", item_id="onion_rings")])
    parser = ScriptedParser(ParsedOrder(lines=[combo_no_drink], not_on_menu=[], customer_confirmed=True))
    pos = MockPOS(pos_id_map={})
    service = OrderingService(parser=parser, pos=pos, clock=lambda: NOON_WEDNESDAY)

    reply = service.handle_message("+15550002", "number 3 with rings, place it")
    assert "drink" in reply
    assert pos.orders == {}  # "yes" never overrides the validator


def test_not_on_menu_is_mentioned() -> None:
    parser = ScriptedParser(ParsedOrder(lines=[CartLine(item_id="coke")], not_on_menu=["pizza"], customer_confirmed=False))
    service = OrderingService(parser=parser, pos=MockPOS(pos_id_map={}), clock=lambda: NOON_WEDNESDAY)
    assert service.handle_message("+15550003", "pizza and a coke").startswith("Sorry, we don't have: pizza.")


def test_pos_idempotency_key_prevents_double_orders() -> None:
    pos = MockPOS(pos_id_map={})
    first = pos.submit_order("+1555", [], 899, idempotency_key="+1555:1")
    retry = pos.submit_order("+1555", [], 899, idempotency_key="+1555:1")  # e.g. network timeout, we retried
    assert first.pos_order_id == retry.pos_order_id
    assert len(pos.orders) == 1 and pos.submit_calls == 2
