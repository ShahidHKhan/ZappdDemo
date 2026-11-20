"""The deterministic order validator.  "The LLM proposes, this code disposes."

Input:  a Cart (IDs only), the Catalog, the Rules, and the current time.
Output: a ValidationResult - either ok=True with exact prices in cents,
        or a list of structured Issues, each with a question for the customer.

There is NO LLM in this file. Same input -> same output, every time.
That's what lets us put AI in front of a real kitchen and a real credit card.
"""

from datetime import datetime, time

from zappd.catalog import find_modifier, get_group, get_item
from zappd.models import (
    Cart,
    CartLine,
    Catalog,
    Issue,
    Item,
    ModifierGroup,
    PricedLine,
    PromotionRule,
    RequireGroupRule,
    Rule,
    SoldOutRule,
    TimeWindowRule,
    ValidationResult,
)

MAX_QUANTITY = 20


# ---------------------------------------------------------------- rule helpers


def _applies(rule: Rule, item: Item) -> bool:
    return bool(set(rule.applies_to_tags) & set(item.tags))


def effective_min_selections(item: Item, group: ModifierGroup, rules: list[Rule]) -> tuple[int, str | None]:
    """The catalog says doneness is optional; a rule may say otherwise.

    Returns (min selections, the question to ask if missing).
    """
    for rule in rules:
        if isinstance(rule, RequireGroupRule) and rule.group_id == group.id and _applies(rule, item):
            return max(1, group.min_selections), rule.question
    return group.min_selections, None


def unavailable_reason(item: Item, rules: list[Rule], now: datetime) -> str | None:
    """None if the item can be ordered right now, otherwise a short reason."""
    for rule in rules:
        if not _applies(rule, item):
            continue
        if isinstance(rule, SoldOutRule) and rule.active:
            return "sold out right now"
        if isinstance(rule, TimeWindowRule):
            start, end = time.fromisoformat(rule.start), time.fromisoformat(rule.end)
            if not (start <= now.time() < end):
                return f"only available {rule.start}-{rule.end}"
    return None


# ---------------------------------------------------------------- line checks


def _check_modifiers(index: int, line: CartLine, target: Item, catalog: Catalog, rules: list[Rule]) -> list[Issue]:
    """Check the modifiers on one line against the target item's groups.
    For a combo, `target` is the combo's main item (the burger)."""
    issues: list[Issue] = []
    count_per_group: dict[str, int] = {}

    for mod_id in line.modifier_ids:
        found = find_modifier(catalog, mod_id)
        if found is None:
            issues.append(Issue(
                code="UNKNOWN_MODIFIER", line_index=index,
                message=f"Modifier '{mod_id}' does not exist. Use get_item to see valid modifier IDs.",
            ))
            continue
        group, _ = found
        if group.id not in target.modifier_group_ids:
            issues.append(Issue(
                code="MODIFIER_NOT_ALLOWED", line_index=index,
                message=f"'{mod_id}' ({group.name}) cannot be applied to {target.name}.",
                question=f"Sorry, {target.name} can't be ordered with '{group.name.lower()}'. Is that okay?",
            ))
            continue
        count_per_group[group.id] = count_per_group.get(group.id, 0) + 1

    for group_id in target.modifier_group_ids:
        group = get_group(catalog, group_id)
        if group is None:
            continue
        count = count_per_group.get(group_id, 0)
        min_needed, rule_question = effective_min_selections(target, group, rules)
        if count < min_needed:
            options = ", ".join(m.name for m in group.modifiers)
            issues.append(Issue(
                code="MISSING_REQUIRED_MODIFIER", line_index=index,
                message=f"{target.name} requires {min_needed} selection(s) from '{group.id}' (got {count}).",
                question=rule_question or f"Which {group.name.lower()} would you like for your {target.name}? ({options})",
            ))
        if count > group.max_selections:
            issues.append(Issue(
                code="TOO_MANY_MODIFIERS", line_index=index,
                message=f"{target.name} allows at most {group.max_selections} from '{group.id}' (got {count}).",
                question=f"You can pick up to {group.max_selections} {group.name.lower()} for {target.name}. Which ones?",
            ))
    return issues


def _check_combo(index: int, line: CartLine, combo_item: Item, catalog: Catalog, rules: list[Rule], now: datetime) -> list[Issue]:
    assert combo_item.combo is not None
    issues: list[Issue] = []
    slot_names = {s.name for s in combo_item.combo.slots}

    for choice in line.combo_choices:
        if choice.slot not in slot_names:
            issues.append(Issue(
                code="INVALID_COMBO_CHOICE", line_index=index,
                message=f"{combo_item.name} has no '{choice.slot}' slot. Slots: {sorted(slot_names)}.",
            ))

    for slot in combo_item.combo.slots:
        picks = [c for c in line.combo_choices if c.slot == slot.name]
        allowed_names = ", ".join(i.name for i in (get_item(catalog, x) for x in slot.allowed_item_ids) if i)
        if not picks:
            issues.append(Issue(
                code="INCOMPLETE_COMBO", line_index=index,
                message=f"{combo_item.name} needs a '{slot.name}'. Allowed: {slot.allowed_item_ids}.",
                question=f"What {slot.name} would you like with your {combo_item.name}? ({allowed_names})",
            ))
            continue
        if len(picks) > 1:
            issues.append(Issue(
                code="INVALID_COMBO_CHOICE", line_index=index,
                message=f"{combo_item.name} takes exactly one '{slot.name}' (got {len(picks)}).",
                question=f"Which one {slot.name} would you like with your {combo_item.name}? ({allowed_names})",
            ))
        for pick in picks:
            picked = get_item(catalog, pick.item_id)
            if pick.item_id not in slot.allowed_item_ids or picked is None:
                issues.append(Issue(
                    code="INVALID_COMBO_CHOICE", line_index=index,
                    message=f"'{pick.item_id}' is not allowed as the {slot.name} in {combo_item.name}. Allowed: {slot.allowed_item_ids}.",
                    question=f"That {slot.name} isn't part of the combo. Pick one of: {allowed_names}?",
                ))
            elif reason := unavailable_reason(picked, rules, now):
                issues.append(Issue(
                    code="ITEM_UNAVAILABLE", line_index=index,
                    message=f"{picked.name} is {reason}.",
                    question=f"Sorry, {picked.name} is {reason}. Pick another {slot.name}? ({allowed_names})",
                ))
    return issues


def _describe(line: CartLine, item: Item, catalog: Catalog) -> str:
    parts = [found[1].name for m in line.modifier_ids if (found := find_modifier(catalog, m))]
    for choice in line.combo_choices:
        picked = get_item(catalog, choice.item_id)
        parts.append(f"{choice.slot}: {picked.name if picked else choice.item_id}")
    return f"{item.name} ({', '.join(parts)})" if parts else item.name


# ---------------------------------------------------------------- main entry point


def validate_cart(cart: Cart, catalog: Catalog, rules: list[Rule], now: datetime) -> ValidationResult:
    issues: list[Issue] = []
    priced: list[PricedLine] = []
    priced_items: list[tuple[Item, int]] = []  # (item, quantity) - used for promotions

    if not cart.lines:
        issues.append(Issue(code="EMPTY_CART", line_index=None, message="Cart has no lines.",
                            question="What would you like to order?"))

    for index, line in enumerate(cart.lines):
        if not 1 <= line.quantity <= MAX_QUANTITY:
            issues.append(Issue(code="INVALID_QUANTITY", line_index=index,
                                message=f"Quantity must be 1-{MAX_QUANTITY} (got {line.quantity})."))
            continue

        item = get_item(catalog, line.item_id)
        if item is None:  # e.g. the LLM invented "truffle_fries"
            issues.append(Issue(
                code="UNKNOWN_ITEM", line_index=index,
                message=f"Item '{line.item_id}' is not on the menu. Use search_menu to find valid item IDs.",
                question="Sorry, we don't have that item. Want to hear what we do have?",
            ))
            continue

        if reason := unavailable_reason(item, rules, now):
            issues.append(Issue(
                code="ITEM_UNAVAILABLE", line_index=index,
                message=f"{item.name} is {reason}.",
                question=f"Sorry, {item.name} is {reason}. Would you like something else instead?",
            ))
            continue

        # For combos, modifiers ("no pickles", "medium") apply to the main item.
        target = get_item(catalog, item.combo.main_item_id) if item.combo else item
        if target is None:
            continue
        issues += _check_modifiers(index, line, target, catalog, rules)
        if item.combo:
            issues += _check_combo(index, line, item, catalog, rules, now)

        # Price: base + every known modifier's delta. Integer math only.
        deltas = sum(found[1].price_delta_cents for m in line.modifier_ids if (found := find_modifier(catalog, m)))
        unit = item.price_cents + deltas
        priced.append(PricedLine(
            item_id=item.id, name=item.name, quantity=line.quantity,
            unit_price_cents=unit, line_total_cents=unit * line.quantity,
            description=_describe(line, item, catalog),
        ))
        priced_items.append((item, line.quantity))

    subtotal = sum(p.line_total_cents for p in priced)

    discount = 0
    applied: list[str] = []
    for rule in rules:
        if isinstance(rule, PromotionRule) and now.weekday() in rule.weekdays:
            amount = sum(rule.discount_cents * qty for item, qty in priced_items if _applies(rule, item))
            if amount:
                discount += amount
                applied.append(rule.id)

    return ValidationResult(
        ok=not issues,
        issues=issues,
        lines=priced,
        subtotal_cents=subtotal,
        discount_cents=discount,
        total_cents=subtotal - discount,
        applied_promotions=applied,
    )
