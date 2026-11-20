"""Every data shape in the system, in one place.

Three families of models:
  1. CATALOG  - what the restaurant sells (loaded from data/catalog.json)
  2. RULES    - tribal knowledge stored as data (loaded from data/rules.json)
  3. ORDERS   - what a customer/agent wants, and what the validator says about it

Money is ALWAYS integer cents. 0.1 + 0.2 != 0.3 in floating point;
nobody wants a POS total that is off by a penny.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 1. CATALOG


class Location(BaseModel):
    id: str
    name: str
    timezone: str


class Modifier(BaseModel):
    id: str
    name: str
    price_delta_cents: int = 0  # "Add Bacon" = +150


class ModifierGroup(BaseModel):
    """A set of choices attached to an item, e.g. "Doneness" or "Add-ons".

    min_selections > 0 means the group is REQUIRED.
    max_selections caps how many the customer may pick.
    """

    id: str
    name: str
    min_selections: int = 0
    max_selections: int = 1
    modifiers: list[Modifier]


class ComboSlot(BaseModel):
    """One "pick one of these" slot inside a combo, e.g. side or drink."""

    name: str
    allowed_item_ids: list[str]


class ComboSpec(BaseModel):
    main_item_id: str  # the burger/sandwich; modifiers on the combo line apply to it
    slots: list[ComboSlot]


class Item(BaseModel):
    id: str  # canonical ID - the ONLY thing the LLM is allowed to reference
    name: str
    category: str
    price_cents: int
    aliases: list[str] = []  # how humans actually say it ("number 3", "coke")
    tags: list[str] = []  # rules target tags, not individual items
    modifier_group_ids: list[str] = []
    combo: ComboSpec | None = None  # only set for combos


class Catalog(BaseModel):
    location: Location
    modifier_groups: list[ModifierGroup]
    items: list[Item]


# ---------------------------------------------------------------- 2. RULES
# A "discriminated union": the `type` field decides which class a JSON rule
# becomes. Adding a new kind of rule = adding a new class, not editing ifs
# scattered across the codebase.


class RequireGroupRule(BaseModel):
    type: Literal["require_group"]
    id: str
    applies_to_tags: list[str]
    group_id: str
    question: str  # what to ask the customer if it's missing
    note: str = ""


class TimeWindowRule(BaseModel):
    type: Literal["time_window"]
    id: str
    applies_to_tags: list[str]
    start: str  # "06:00" (location local time)
    end: str  # "11:00"
    note: str = ""


class SoldOutRule(BaseModel):
    type: Literal["sold_out"]
    id: str
    applies_to_tags: list[str]
    active: bool
    note: str = ""


class PromotionRule(BaseModel):
    type: Literal["promotion"]
    id: str
    applies_to_tags: list[str]
    weekdays: list[int]  # Monday = 0
    discount_cents: int
    note: str = ""


Rule = Annotated[
    RequireGroupRule | TimeWindowRule | SoldOutRule | PromotionRule,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------- 3. ORDERS


class ComboChoice(BaseModel):
    slot: str  # "side" / "drink"
    item_id: str


class CartLine(BaseModel):
    """One line of a proposed order. This is what the LLM produces
    and what outside agents send us. Everything is referenced by ID."""

    item_id: str
    quantity: int = 1
    modifier_ids: list[str] = []
    combo_choices: list[ComboChoice] = []


class Cart(BaseModel):
    lines: list[CartLine] = []


IssueCode = Literal[
    "EMPTY_CART",
    "INVALID_QUANTITY",
    "UNKNOWN_ITEM",
    "ITEM_UNAVAILABLE",
    "UNKNOWN_MODIFIER",
    "MODIFIER_NOT_ALLOWED",
    "MISSING_REQUIRED_MODIFIER",
    "TOO_MANY_MODIFIERS",
    "INCOMPLETE_COMBO",
    "INVALID_COMBO_CHOICE",
]


class Issue(BaseModel):
    """A structured problem with the cart.

    `code`     is for programs (stable, machine-readable).
    `message`  is for developers / AI agents (says how to fix it).
    `question` is what we'd literally text the customer.
    """

    code: IssueCode
    line_index: int | None
    message: str
    question: str | None = None


class PricedLine(BaseModel):
    item_id: str
    name: str
    quantity: int
    unit_price_cents: int  # base price + modifier deltas
    line_total_cents: int
    description: str  # human-readable, e.g. "Bolt Classic Burger (Medium, No Pickles)"


class ValidationResult(BaseModel):
    ok: bool
    issues: list[Issue]
    lines: list[PricedLine]
    subtotal_cents: int
    discount_cents: int
    total_cents: int
    applied_promotions: list[str]
