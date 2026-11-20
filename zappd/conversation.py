"""The conversation loop for a texting customer.

    customer text
        -> parser (LLM)      : text -> ParsedOrder (a proposed cart)
        -> validator (code)  : cart -> issues + exact prices
        -> decide the reply  : ask a question | show summary | place the order
        -> POS               : only when the cart is valid AND the customer said yes

Key design choice: the LLM never writes the reply text. Replies are built from
the validator's output, so the bot can't make up a price or promise an item
we don't have. The LLM only does the part code can't: understanding slang.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from zappd.catalog import load_catalog, load_rules, local_now
from zappd.models import Cart, Catalog, Rule, ValidationResult
from zappd.parser import ParsedOrder, ParseStats
from zappd.pos import POSAdapter
from zappd.validator import validate_cart

# The parser is passed in, so tests can use a fake one with no LLM or network.
ParserFn = Callable[[str, Cart, list[tuple[str, str]]], tuple[ParsedOrder, ParseStats]]

MAX_QUESTIONS_PER_REPLY = 2  # SMS: ask a couple of things at a time, not a wall of text


@dataclass
class Conversation:
    customer_id: str
    cart: Cart = field(default_factory=Cart)
    history: list[tuple[str, str]] = field(default_factory=list)  # ("customer" | "bolt", text)
    status: Literal["ordering", "placed"] = "ordering"
    orders_placed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def summarize(result: ValidationResult) -> str:
    lines = [f"- {p.quantity}x {p.description}: {dollars(p.line_total_cents)}" for p in result.lines]
    if result.discount_cents:
        lines.append(f"- Discount ({', '.join(result.applied_promotions)}): -{dollars(result.discount_cents)}")
    lines.append(f"Total: {dollars(result.total_cents)}")
    return "\n".join(lines)


class OrderingService:
    def __init__(
        self,
        parser: ParserFn,
        pos: POSAdapter,
        catalog: Catalog | None = None,
        rules: list[Rule] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.parser = parser
        self.pos = pos
        self.catalog = catalog or load_catalog()
        self.rules = rules if rules is not None else load_rules()
        self.clock = clock or (lambda: local_now(self.catalog))
        # In-memory state, keyed by phone number. A real system would use Redis/Postgres
        # so state survives restarts and is shared between server instances.
        self.conversations: dict[str, Conversation] = {}
        self._registry_lock = threading.Lock()

    def _get_conversation(self, customer_id: str) -> Conversation:
        with self._registry_lock:
            conv = self.conversations.get(customer_id)
            if conv is None or conv.status == "placed":  # texting after an order starts a new one
                previous = conv.orders_placed if conv else 0
                conv = Conversation(customer_id=customer_id, orders_placed=previous)
                self.conversations[customer_id] = conv
            return conv

    def handle_message(self, customer_id: str, text: str) -> str:
        conv = self._get_conversation(customer_id)
        # Per-conversation lock: if a customer double-texts, the two messages are
        # processed one after another instead of racing to overwrite the cart.
        # Different customers never wait on each other.
        with conv.lock:
            conv.history.append(("customer", text))
            reply = self._step(conv, text)
            conv.history.append(("bolt", reply))
            return reply

    def _step(self, conv: Conversation, text: str) -> str:
        # 1. LLM proposes
        parsed, _stats = self.parser(text, conv.cart, conv.history[:-1])
        conv.cart = Cart(lines=parsed.lines)

        # 2. Code disposes
        result = validate_cart(conv.cart, self.catalog, self.rules, self.clock())

        prefix = ""
        if parsed.not_on_menu:
            prefix = f"Sorry, we don't have: {', '.join(parsed.not_on_menu)}.\n"

        # 3a. Something is missing or wrong -> ask (deduped, max 2 questions)
        if not result.ok:
            questions: list[str] = []
            for issue in result.issues:
                q = issue.question or issue.message
                if q not in questions:
                    questions.append(q)
            return prefix + "\n".join(questions[:MAX_QUESTIONS_PER_REPLY])

        # 3b. Valid AND confirmed -> send to the POS
        if parsed.customer_confirmed:
            idempotency_key = f"{conv.customer_id}:{conv.orders_placed + 1}"
            order = self.pos.submit_order(conv.customer_id, result.lines, result.total_cents, idempotency_key)
            conv.orders_placed += 1
            conv.status = "placed"
            return f"Order placed! #{order.pos_order_id}, total {dollars(result.total_cents)}. Ready in about 15 minutes."

        # 3c. Valid but not confirmed -> read it back and ask for a yes
        return prefix + "Here's your order:\n" + summarize(result) + "\nReply YES to place it, or tell me what to change."
