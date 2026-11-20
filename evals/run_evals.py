"""Run the parser + validator on realistic messy orders and score them.

    python evals/run_evals.py            # all cases (~12 LLM calls, a few cents)
    python evals/run_evals.py typos      # only cases whose id contains "typos"

Why: when you change the prompt, the model, or the menu format, you need a
NUMBER that tells you if things got better or worse - not a vibe from trying
two orders by hand. Run this before and after every prompt change.

Grading compares carts as a multiset of units, so these are all equal:
    [combo_3 x2]  ==  [combo_3 x1, combo_3 x1]      and modifier order doesn't matter.
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python evals/run_evals.py`

from zappd.catalog import load_catalog, load_rules  # noqa: E402
from zappd.llm import estimate_cost_usd  # noqa: E402
from zappd.models import Cart, CartLine  # noqa: E402
from zappd.parser import parse_order  # noqa: E402
from zappd.validator import validate_cart  # noqa: E402

Unit = tuple[str, frozenset[str], frozenset[tuple[str, str]]]


def to_units(lines: list[CartLine]) -> Counter[Unit]:
    units: Counter[Unit] = Counter()
    for line in lines:
        key = (line.item_id, frozenset(line.modifier_ids), frozenset((c.slot, c.item_id) for c in line.combo_choices))
        units[key] += line.quantity
    return units


def main(filter_text: str = "") -> None:
    cases: list[dict[str, Any]] = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))
    cases = [c for c in cases if filter_text in c["id"]]
    catalog, rules = load_catalog(), load_rules()

    cart_ok = issues_ok = asked = 0
    latencies: list[float] = []
    cost = 0.0

    for case in cases:
        parsed, stats = parse_order(case["text"], Cart(), [])
        result = validate_cart(Cart(lines=parsed.lines), catalog, rules, datetime.fromisoformat(case["now"]))

        expected = [CartLine.model_validate(x) for x in case["expected_lines"]]
        cart_match = to_units(parsed.lines) == to_units(expected)
        codes = sorted({i.code for i in result.issues})
        issue_match = codes == sorted(set(case["expected_issue_codes"]))
        if case.get("expect_not_on_menu"):
            issue_match = issue_match and bool(parsed.not_on_menu)

        cart_ok += cart_match
        issues_ok += issue_match
        asked += bool(result.issues)
        latencies.append(stats.latency_s)
        cost += estimate_cost_usd(stats.input_tokens, stats.output_tokens)

        mark = "PASS" if cart_match and issue_match else "FAIL"
        print(f"[{mark}] {case['id']:28} {stats.latency_s:5.1f}s  issues={codes}")
        if not cart_match:
            print(f"       expected: {[l.model_dump(exclude_defaults=True) for l in expected]}")
            print(f"       got:      {[l.model_dump(exclude_defaults=True) for l in parsed.lines]}")

    n = len(cases) or 1
    latencies.sort()
    print("\n---- summary")
    print(f"cart exact match:    {cart_ok}/{len(cases)}  ({cart_ok / n:.0%})")
    print(f"issue codes correct: {issues_ok}/{len(cases)}  ({issues_ok / n:.0%})")
    print(f"clarification rate:  {asked / n:.0%}  (how often we had to ask the customer something)")
    print(f"latency p50 / max:   {latencies[len(latencies) // 2]:.1f}s / {latencies[-1]:.1f}s")
    print(f"estimated cost:      ${cost:.4f} total, ${cost / n:.4f} per order")


if __name__ == "__main__":
    from zappd.llm import LLMUnavailableError

    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except LLMUnavailableError as e:
        sys.exit(str(e))
