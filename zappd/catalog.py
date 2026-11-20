"""Load the canonical catalog + rules from disk, and look things up.

Every other module asks THIS module for menu data. Nobody else reads the JSON
files directly, so if we later move to SQLite/Postgres, only this file changes.
"""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from rapidfuzz import fuzz, process

from zappd.models import Catalog, Item, Modifier, ModifierGroup, Rule
from zappd.text import normalize

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@lru_cache  # read the files once per process, not on every request
def load_catalog() -> Catalog:
    raw = json.loads((DATA_DIR / "catalog.json").read_text(encoding="utf-8"))
    return Catalog.model_validate(raw)  # Pydantic rejects malformed data at load time


@lru_cache
def load_rules() -> list[Rule]:
    raw = json.loads((DATA_DIR / "rules.json").read_text(encoding="utf-8"))
    return TypeAdapter(list[Rule]).validate_python(raw)


def local_now(catalog: Catalog) -> datetime:
    """'Now' in the restaurant's timezone (breakfast ends at 11 *there*)."""
    return datetime.now(ZoneInfo(catalog.location.timezone))


# ---- lookups (linear scans are fine for 20 items; index them at scale)


def get_item(catalog: Catalog, item_id: str) -> Item | None:
    return next((i for i in catalog.items if i.id == item_id), None)


def get_group(catalog: Catalog, group_id: str) -> ModifierGroup | None:
    return next((g for g in catalog.modifier_groups if g.id == group_id), None)


def find_modifier(catalog: Catalog, modifier_id: str) -> tuple[ModifierGroup, Modifier] | None:
    for group in catalog.modifier_groups:
        for mod in group.modifiers:
            if mod.id == modifier_id:
                return group, mod
    return None


def search_items(catalog: Catalog, query: str, limit: int = 5) -> list[tuple[Item, float]]:
    """Fuzzy search over names + aliases. Returns (item, score 0-100), best first."""
    # Same normalization as ingestion, so "lg fry" -> "large fries" here too.
    # Filler words are kept: someone searching "burger" should find burgers.
    choices: dict[str, str] = {}  # normalized searchable text -> item_id
    for item in catalog.items:
        for text in [item.name, *item.aliases]:
            choices[normalize(text, drop_filler=False)] = item.id

    best: dict[str, float] = {}
    query_norm = normalize(query, drop_filler=False)
    for text, score, _ in process.extract(query_norm, list(choices), scorer=fuzz.WRatio, limit=None):
        item_id = choices[text]
        best[item_id] = max(best.get(item_id, 0.0), score)

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(item, score) for item_id, score in ranked if (item := get_item(catalog, item_id))]


def menu_for_prompt(catalog: Catalog) -> str:
    """A compact text version of the menu to put in the LLM prompt.

    Small context = cheaper, faster, and less room for the model to get
    distracted. With 20 items we send the whole menu; with 2,000 we'd first
    run search_items() on the customer's message and send only candidates.
    """
    lines = ["ITEMS (id | name | price | aliases | modifier groups | combo details):"]
    for item in catalog.items:
        combo = ""
        if item.combo:
            slots = "; ".join(f"{s.name} slot: {', '.join(s.allowed_item_ids)}" for s in item.combo.slots)
            combo = f" | main item={item.combo.main_item_id}; {slots}"
        groups = ", ".join(item.modifier_group_ids) or "-"
        lines.append(
            f"- {item.id} | {item.name} | ${item.price_cents / 100:.2f} | "
            f"{', '.join(item.aliases)} | {groups}{combo}"
        )

    lines.append("\nMODIFIER GROUPS (group id: modifier ids):")
    for group in catalog.modifier_groups:
        mods = ", ".join(
            m.id + (f" (+${m.price_delta_cents / 100:.2f})" if m.price_delta_cents else "")
            for m in group.modifiers
        )
        lines.append(f"- {group.id}: {mods}")
    return "\n".join(lines)
