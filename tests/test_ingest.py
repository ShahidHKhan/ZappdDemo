from zappd.catalog import load_catalog
from zappd.ingest import find_duplicates, load_sources, normalize, resolve, resolve_prices, score_pair


def test_normalize_expands_pos_abbreviations() -> None:
    assert normalize("LG FRY") == "large fries"
    assert normalize("CRSPY CHKN SAND") == "crispy chicken sandwich"
    assert normalize("Mushroom & Swiss") == "mushroom swiss"


def test_subset_match_can_never_auto_accept() -> None:
    # "truffle fries" contains "fries" - that must NOT be treated as a confident match.
    assert score_pair("truffle fries", "fries") < 90


def test_pipeline_end_to_end() -> None:
    catalog = load_catalog()
    matches = resolve(load_sources(), catalog, use_llm=False, human_decisions={})
    by_name = {m.record.raw_name: m for m in matches}

    assert by_name["DBL BOLT"].canonical_id == "double_bolt"  # not combo_2!
    assert by_name["Truffle Fries"].decision == "review"  # a human decides
    assert by_name["TEST ITEM DO NOT USE"].decision == "no_match"

    dupes = find_duplicates(matches)
    assert set(dupes) == {"bolt_classic", "fries_large"}

    prices = {d.canonical_id: d for d in resolve_prices(matches, catalog)}
    assert prices["mushroom_swiss"].chosen_cents == 999  # POS beats website's $9.49
    assert prices["coke"].chosen_cents == 249  # delivery app's $3.49 is ignored


def test_human_decision_overrides_score() -> None:
    matches = resolve(load_sources(), load_catalog(), human_decisions={"pos:1017": "combo_1", "pos:1022": None})
    by_id = {f"{m.record.source}:{m.record.source_id}": m for m in matches}
    assert by_id["pos:1017"].decision == "human_accepted" and by_id["pos:1017"].canonical_id == "combo_1"
    assert by_id["pos:1022"].decision == "human_rejected"
