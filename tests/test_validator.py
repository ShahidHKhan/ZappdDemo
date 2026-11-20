"""The validator is the trust layer, so it gets the most tests.
No LLM, no network: these run in milliseconds."""

from datetime import datetime

import pytest

from zappd.catalog import load_catalog, load_rules
from zappd.models import Cart, CartLine, ComboChoice, ValidationResult
from zappd.validator import validate_cart

WEDNESDAY_NOON = datetime(2026, 11, 18, 12, 0)
WEDNESDAY_8AM = datetime(2026, 11, 18, 8, 0)
TUESDAY_NOON = datetime(2026, 11, 17, 12, 0)


def run(*lines: CartLine, now: datetime = WEDNESDAY_NOON) -> ValidationResult:
    return validate_cart(Cart(lines=list(lines)), load_catalog(), load_rules(), now)


def codes(result: ValidationResult) -> list[str]:
    return [i.code for i in result.issues]


def full_combo_3(**kwargs) -> CartLine:
    return CartLine(
        item_id="combo_3",
        combo_choices=[ComboChoice(slot="side", item_id="fries_regular"), ComboChoice(slot="drink", item_id="coke")],
        **kwargs,
    )


# ---- the happy path


def test_valid_order_is_priced_in_cents() -> None:
    result = run(
        CartLine(item_id="bolt_classic", modifier_ids=["doneness_medium", "add_bacon"]),  # 899 + 150
        CartLine(item_id="fries_large", quantity=2),  # 449 * 2
    )
    assert result.ok, result.issues
    assert result.subtotal_cents == 1049 + 898
    assert result.total_cents == 1947
    assert result.lines[0].description == "Bolt Classic Burger (Medium, Add Bacon)"


def test_valid_combo_with_burger_modifiers() -> None:
    result = run(full_combo_3(quantity=2, modifier_ids=["no_pickles"]))
    assert result.ok, result.issues
    assert result.total_cents == 1199 * 2


# ---- catalog errors (what an LLM hallucination looks like)


def test_unknown_item_is_rejected() -> None:
    result = run(CartLine(item_id="truffle_fries"))
    assert codes(result) == ["UNKNOWN_ITEM"]
    assert not result.ok


def test_unknown_modifier_is_rejected() -> None:
    result = run(CartLine(item_id="bolt_classic", modifier_ids=["doneness_medium", "extra_sauce"]))
    assert codes(result) == ["UNKNOWN_MODIFIER"]


def test_modifier_from_wrong_group_is_rejected() -> None:
    # Doneness exists, but not for a chicken sandwich.
    result = run(CartLine(item_id="crispy_chicken", modifier_ids=["doneness_medium"]))
    assert codes(result) == ["MODIFIER_NOT_ALLOWED"]


def test_invalid_quantity() -> None:
    assert codes(run(CartLine(item_id="coke", quantity=0))) == ["INVALID_QUANTITY"]


def test_empty_cart_asks_what_they_want() -> None:
    result = run()
    assert codes(result) == ["EMPTY_CART"]
    assert result.issues[0].question == "What would you like to order?"


# ---- rules / tribal knowledge


def test_doneness_required_by_rule_even_though_catalog_says_optional() -> None:
    result = run(CartLine(item_id="double_bolt"))
    assert codes(result) == ["MISSING_REQUIRED_MODIFIER"]
    assert "cooked" in (result.issues[0].question or "")


def test_chicken_does_not_need_doneness() -> None:
    assert run(CartLine(item_id="crispy_chicken")).ok


def test_latte_requires_milk_choice_from_catalog() -> None:
    assert codes(run(CartLine(item_id="latte"))) == ["MISSING_REQUIRED_MODIFIER"]
    result = run(CartLine(item_id="latte", modifier_ids=["milk_oat"]))
    assert result.ok and result.total_cents == 425 + 75


def test_too_many_from_single_choice_group() -> None:
    result = run(CartLine(item_id="bolt_classic", modifier_ids=["doneness_medium", "doneness_well"]))
    assert codes(result) == ["TOO_MANY_MODIFIERS"]


@pytest.mark.parametrize(("now", "ok"), [(WEDNESDAY_8AM, True), (WEDNESDAY_NOON, False)])
def test_breakfast_only_before_11(now: datetime, ok: bool) -> None:
    assert run(CartLine(item_id="breakfast_burrito"), now=now).ok is ok


def test_shakes_sold_out_when_machine_is_down() -> None:
    result = run(CartLine(item_id="chocolate_shake"))
    assert codes(result) == ["ITEM_UNAVAILABLE"]


# ---- combos


def test_combo_missing_drink_asks_for_it() -> None:
    line = CartLine(item_id="combo_1", modifier_ids=["doneness_well"],
                    combo_choices=[ComboChoice(slot="side", item_id="onion_rings")])
    result = run(line)
    assert codes(result) == ["INCOMPLETE_COMBO"]
    assert "drink" in (result.issues[0].question or "")


def test_combo_burger_still_needs_doneness() -> None:
    line = CartLine(item_id="combo_1", combo_choices=[
        ComboChoice(slot="side", item_id="fries_regular"), ComboChoice(slot="drink", item_id="coke")])
    assert codes(run(line)) == ["MISSING_REQUIRED_MODIFIER"]


def test_combo_rejects_item_not_allowed_in_slot() -> None:
    line = CartLine(item_id="combo_3", combo_choices=[
        ComboChoice(slot="side", item_id="fries_large"),  # large fries aren't a combo side
        ComboChoice(slot="drink", item_id="coke")])
    assert codes(run(line)) == ["INVALID_COMBO_CHOICE"]


# ---- promotions


def test_tuesday_combo_discount() -> None:
    result = run(full_combo_3(quantity=2), CartLine(item_id="coke"), now=TUESDAY_NOON)
    assert result.ok
    assert result.subtotal_cents == 1199 * 2 + 249
    assert result.discount_cents == 200  # $1 off each combo, not the coke
    assert result.total_cents == result.subtotal_cents - 200
    assert result.applied_promotions == ["tuesday_combo_deal"]


def test_no_discount_on_wednesday() -> None:
    assert run(full_combo_3()).discount_cents == 0
