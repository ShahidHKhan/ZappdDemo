"""Ingestion + entity resolution: messy sources -> canonical catalog matches.

    python -m zappd.ingest           # rules + fuzzy matching only
    python -m zappd.ingest --llm     # also ask Claude about the ambiguous ones

The problem: the same burger appears as "BOLT CLASSIC" (POS), "The Bolt Classic"
(website) and "Bolt Classic Burger" (delivery app), at different prices, and
the POS also has a stale duplicate button "BOLT CLSC". Which rows are the same
real-world item? That's ENTITY RESOLUTION.

Pipeline (cheapest, most reliable step first):
    1. normalize   - deterministic: lowercase, strip punctuation, expand "LG"->"large"
    2. fuzzy match - rapidfuzz score 0-100 against canonical names + aliases
    3. thresholds  - >= 90 auto-accept | 75-90 ambiguous | < 75 no match
    4. LLM judge   - (optional) ambiguous pairs only - a few cents, not thousands of calls
    5. human queue - anything still unsure goes to data/review_queue.json for a person
Then: detect duplicates, resolve price conflicts by source priority, record provenance.
"""

import csv
import json
import sys
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from zappd.catalog import DATA_DIR, load_catalog
from zappd.models import Catalog
from zappd.text import normalize

SourceName = Literal["pos", "website", "delivery_app"]

# Which source wins a price conflict. The POS is what actually charges the card;
# the delivery app is excluded because it inflates prices to cover its commission.
PRICE_PRIORITY: list[SourceName] = ["pos", "website"]

AUTO_ACCEPT = 90.0
AMBIGUOUS = 75.0


class SourceRecord(BaseModel):
    source: SourceName
    source_id: str  # POS button id, or row index for sources without ids
    raw_name: str
    price_cents: int


class Match(BaseModel):
    record: SourceRecord
    normalized: str
    canonical_id: str | None
    score: float
    decision: Literal["auto", "llm_accepted", "human_accepted", "human_rejected", "review", "no_match"]
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.canonical_id is not None and self.decision in ("auto", "llm_accepted", "human_accepted")


class PriceDecision(BaseModel):
    canonical_id: str
    chosen_cents: int
    chosen_from: str  # provenance: "pos:1001"
    catalog_cents: int
    conflicts: list[str]  # other sources that disagreed


# ---------------------------------------------------------------- 1. load sources


def _cents(text: str) -> int:
    return round(float(text.replace("$", "").strip()) * 100)


def load_sources() -> list[SourceRecord]:
    records: list[SourceRecord] = []
    with open(DATA_DIR / "sources" / "pos_export.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records.append(SourceRecord(source="pos", source_id=row["pos_id"], raw_name=row["name"], price_cents=_cents(row["price"])))

    website = json.loads((DATA_DIR / "sources" / "website.json").read_text(encoding="utf-8"))
    for i, row in enumerate(website["menu"]):
        records.append(SourceRecord(source="website", source_id=str(i), raw_name=row["title"], price_cents=_cents(row["price"])))

    delivery = json.loads((DATA_DIR / "sources" / "delivery_app.json").read_text(encoding="utf-8"))
    for i, row in enumerate(delivery["products"]):
        records.append(SourceRecord(source="delivery_app", source_id=str(i), raw_name=row["product_name"], price_cents=row["unit_price_cents"]))
    return records


# ---------------------------------------------------------------- 2. normalize + match


# normalize() lives in zappd/text.py (shared with menu search).


def _candidates(catalog: Catalog) -> list[tuple[str, str]]:
    """(normalized text, canonical id) for every canonical name and alias."""
    return [(normalize(text), item.id) for item in catalog.items for text in [item.name, *item.aliases]]


def score_pair(a: str, b: str) -> float:
    """0-100 similarity of two normalized names.

    token_sort_ratio: word order doesn't matter, missing/extra words are penalized.
    token_set_ratio:  100 if one name's words are a SUBSET of the other's
                      ("#1 combo" vs "#1 bolt classic combo"). Useful, but dangerous:
                      "truffle fries" contains "fries"! So it's capped at 85 - a subset
                      match can only ever reach the ambiguous band, never auto-accept.
    """
    return max(fuzz.token_sort_ratio(a, b), 0.85 * fuzz.token_set_ratio(a, b))


def best_match(normalized: str, catalog: Catalog) -> tuple[str | None, float]:
    best_id, best_score = None, 0.0
    for text, item_id in _candidates(catalog):
        score = score_pair(normalized, text)
        if score > best_score:
            best_id, best_score = item_id, score
    return best_id, best_score


# ---------------------------------------------------------------- 4. LLM judge (optional)


class SameItemVerdict(BaseModel):
    same_item: bool
    reason: str


def llm_judge(record: SourceRecord, canonical_name: str) -> SameItemVerdict:
    """Ask Claude: is this messy row the same menu item as this canonical item?"""
    from zappd.llm import FALLBACK_BETA, FALLBACKS, MODEL, get_client

    response = get_client().beta.messages.parse(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": (
            "You are matching restaurant menu data from different systems. Are these the same menu item?\n"
            f"Source ({record.source}): {record.raw_name!r} at ${record.price_cents / 100:.2f}\n"
            f"Canonical item: {canonical_name!r}\n"
            "Different sizes or flavors are NOT the same item."
        )}],
        output_format=SameItemVerdict,
        output_config={"effort": "low"},
        betas=[FALLBACK_BETA],
        fallbacks=FALLBACKS,
    )
    if response.parsed_output is None:
        return SameItemVerdict(same_item=False, reason="model gave no answer")
    return response.parsed_output


# ---------------------------------------------------------------- 3-5. decide


def load_human_decisions() -> dict[str, str | None]:
    path = DATA_DIR / "review_decisions.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def resolve(records: list[SourceRecord], catalog: Catalog, use_llm: bool = False,
            human_decisions: dict[str, str | None] | None = None) -> list[Match]:
    names = {i.id: i.name for i in catalog.items}
    human = human_decisions if human_decisions is not None else load_human_decisions()
    matches: list[Match] = []
    for record in records:
        norm = normalize(record.raw_name)
        item_id, score = best_match(norm, catalog)
        key = f"{record.source}:{record.source_id}"
        if key in human:  # a person already decided: that beats any score
            decided = human[key]
            matches.append(Match(record=record, normalized=norm, canonical_id=decided, score=score,
                                 decision="human_accepted" if decided else "human_rejected",
                                 reason="from review_decisions.json"))
        elif score >= AUTO_ACCEPT:
            matches.append(Match(record=record, normalized=norm, canonical_id=item_id, score=score, decision="auto"))
        elif score >= AMBIGUOUS and item_id:
            if use_llm:
                verdict = llm_judge(record, names[item_id])
                decision = "llm_accepted" if verdict.same_item else "review"
                matches.append(Match(record=record, normalized=norm, canonical_id=item_id, score=score,
                                     decision=decision, reason=f"LLM: {verdict.reason}"))
            else:
                matches.append(Match(record=record, normalized=norm, canonical_id=item_id, score=score,
                                     decision="review", reason="ambiguous score"))
        else:
            matches.append(Match(record=record, normalized=norm, canonical_id=None, score=score,
                                 decision="no_match", reason="new item, or junk? a human decides"))
    return matches


def find_duplicates(matches: list[Match]) -> dict[str, list[str]]:
    """Canonical ids with more than one POS button (stale / duplicate buttons)."""
    buttons: dict[str, list[str]] = {}
    for m in matches:
        if m.record.source == "pos" and m.accepted:
            buttons.setdefault(m.canonical_id, []).append(f"{m.record.source_id} {m.record.raw_name}")
    return {cid: b for cid, b in buttons.items() if len(b) > 1}


def resolve_prices(matches: list[Match], catalog: Catalog) -> list[PriceDecision]:
    accepted = [m for m in matches if m.accepted]
    decisions: list[PriceDecision] = []
    for item in catalog.items:
        mine = [m for m in accepted if m.canonical_id == item.id]
        ranked = sorted((m for m in mine if m.record.source in PRICE_PRIORITY),
                        key=lambda m: PRICE_PRIORITY.index(m.record.source))
        if not ranked:
            continue
        winner = ranked[0]
        conflicts = [
            f"{m.record.source}:{m.record.raw_name} says ${m.record.price_cents / 100:.2f}"
            for m in mine if m.record.price_cents != winner.record.price_cents
        ]
        decisions.append(PriceDecision(
            canonical_id=item.id, chosen_cents=winner.record.price_cents,
            chosen_from=f"{winner.record.source}:{winner.record.source_id}",
            catalog_cents=item.price_cents, conflicts=conflicts,
        ))
    return decisions


def pos_id_map(matches: list[Match]) -> dict[str, str]:
    """canonical id -> POS button id (lowest id wins when there are duplicates)."""
    result: dict[str, str] = {}
    for m in matches:
        if m.record.source == "pos" and m.accepted:
            current = result.get(m.canonical_id)
            if current is None or int(m.record.source_id) < int(current):
                result[m.canonical_id] = m.record.source_id
    return dict(sorted(result.items()))


# ---------------------------------------------------------------- report


def main(use_llm: bool) -> None:
    catalog = load_catalog()
    matches = resolve(load_sources(), catalog, use_llm=use_llm)

    print(f"{'SOURCE':13} {'RAW NAME':26} {'NORMALIZED':26} {'-> CANONICAL':20} SCORE  DECISION")
    for m in matches:
        print(f"{m.record.source:13} {m.record.raw_name[:25]:26} {m.normalized[:25]:26} "
              f"{(m.canonical_id or '-'):20} {m.score:5.1f}  {m.decision}")

    print("\nDUPLICATE POS BUTTONS (same real item, several buttons):")
    for cid, buttons in find_duplicates(matches).items():
        print(f"  {cid}: {buttons}")

    print("\nPRICE CONFLICTS (winner by source priority: POS > website; delivery app never sets price):")
    for d in resolve_prices(matches, catalog):
        if d.conflicts:
            print(f"  {d.canonical_id}: ${d.chosen_cents / 100:.2f} from {d.chosen_from}; ignored: {d.conflicts}")

    queue = [m.model_dump() for m in matches if m.decision in ("review", "no_match")]
    (DATA_DIR / "review_queue.json").write_text(json.dumps(queue, indent=2), encoding="utf-8")
    (DATA_DIR / "pos_id_map.json").write_text(json.dumps(pos_id_map(matches), indent=2), encoding="utf-8")
    print(f"\n{len(queue)} records need a human -> data/review_queue.json")
    print("canonical id -> POS id mapping -> data/pos_id_map.json (used by pos.py)")


if __name__ == "__main__":
    main(use_llm="--llm" in sys.argv)
