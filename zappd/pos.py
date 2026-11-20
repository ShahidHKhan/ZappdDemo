"""POS adapter: the last hop, where a validated order becomes a real kitchen ticket.

ADAPTER PATTERN: the rest of the code only knows the `POSAdapter` interface.
MockPOS implements it here; a SquarePOS / ToastPOS would implement the same two
methods. Adding a new POS never touches the validator, parser, or channels.

IDEMPOTENCY: every submit carries an idempotency key. If the network drops
after the POS accepted the order and we retry, the POS sees the same key and
returns the SAME order instead of firing a second burger (and a second charge).
"""

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from zappd.catalog import DATA_DIR
from zappd.models import PricedLine


class SubmittedOrder(BaseModel):
    pos_order_id: str
    customer_id: str
    lines: list[PricedLine]
    pos_item_ids: list[str]  # the POS's own IDs - the POS is the "source of record"
    total_cents: int


class POSAdapter(Protocol):
    def submit_order(self, customer_id: str, lines: list[PricedLine], total_cents: int, idempotency_key: str) -> SubmittedOrder: ...

    def get_order(self, pos_order_id: str) -> SubmittedOrder | None: ...


def load_pos_id_map(path: Path = DATA_DIR / "pos_id_map.json") -> dict[str, str]:
    """canonical item id -> POS button id. Produced by ingest.py (entity resolution)."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class MockPOS:
    """In-memory stand-in for a real POS like Square or Toast."""

    def __init__(self, pos_id_map: dict[str, str] | None = None) -> None:
        self.pos_id_map = pos_id_map if pos_id_map is not None else load_pos_id_map()
        self.orders: dict[str, SubmittedOrder] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self.submit_calls = 0  # so tests can see retries happened

    def submit_order(self, customer_id: str, lines: list[PricedLine], total_cents: int, idempotency_key: str) -> SubmittedOrder:
        self.submit_calls += 1
        if idempotency_key in self._by_idempotency_key:  # a retry: return the original, do NOT re-fire
            return self.orders[self._by_idempotency_key[idempotency_key]]

        pos_order_id = f"POS-{len(self.orders) + 1:04d}"
        order = SubmittedOrder(
            pos_order_id=pos_order_id,
            customer_id=customer_id,
            lines=lines,
            pos_item_ids=[self.pos_id_map.get(line.item_id, "UNMAPPED") for line in lines],
            total_cents=total_cents,
        )
        self.orders[pos_order_id] = order
        self._by_idempotency_key[idempotency_key] = pos_order_id
        return order

    def get_order(self, pos_order_id: str) -> SubmittedOrder | None:
        return self.orders.get(pos_order_id)
