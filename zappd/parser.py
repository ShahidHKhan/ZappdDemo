"""LLM order parser: free text -> structured Cart.

    "2 number 3s no pickles, one w/ a coke"  ->  ParsedOrder(lines=[CartLine(item_id="combo_3", ...)])

How hallucination is kept in check:
  1. GROUNDING   - the prompt contains the real menu with IDs. The model picks IDs from it.
  2. STRUCTURE   - structured outputs force the reply to match the ParsedOrder schema
                   (valid JSON, right fields, right types). No free-text parsing.
  3. NO GUESSING - the prompt tells the model NOT to fill in choices the customer didn't
                   make (doneness, drink...). Missing info is the validator's job to ask about.
  4. VERIFY      - whatever comes back still goes through validator.py. If the model
                   invents an ID anyway, it becomes an UNKNOWN_ITEM issue, not an order.

The parser is STATELESS: we pass in the current cart + recent conversation every time,
and the model returns the FULL updated cart. Our code (conversation.py) owns the state.
"""

import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from zappd.catalog import load_catalog, menu_for_prompt
from zappd.llm import FALLBACK_BETA, FALLBACKS, MODEL, get_client
from zappd.models import Cart, CartLine


class ParsedOrder(BaseModel):
    """The exact JSON shape Claude must return. The SDK turns this into a JSON schema."""

    lines: list[CartLine] = Field(description="The FULL cart after applying the customer's latest message.")
    not_on_menu: list[str] = Field(description="Things the customer asked for that are not on the menu, in their words.")
    customer_confirmed: bool = Field(description="True only if the latest message clearly confirms placing the order.")


@dataclass
class ParseStats:
    latency_s: float
    input_tokens: int
    output_tokens: int


class ParseError(RuntimeError):
    pass


def build_system_prompt() -> str:
    catalog = load_catalog()
    return f"""You convert customer messages into a structured order for {catalog.location.name}.

Rules:
- Use ONLY item ids and modifier ids that appear in the MENU below. Never invent ids.
- Return the FULL cart after the customer's latest message: keep existing lines, then add, change or remove what they asked.
- Map slang to the menu: "number 3" / "#3" = combo_3, "large fry" = fries_large, "no pickles" = no_pickles, "med rare" = doneness_medium_rare.
- Combos: put the side and drink in combo_choices (slot "side" / "drink"). Modifiers for the combo's burger or sandwich go in modifier_ids on the combo line.
- "2 number 3s, one with a coke and one with a sprite" means two separate combo lines with quantity 1, because they differ.
- Only record choices the customer actually made. If they did not say a doneness, side, drink or milk, leave it out. Never pick a default for them; the system will ask.
- If they ask for something not on the menu, do not substitute anything: put their words in not_on_menu.
- customer_confirmed is true only when the latest message clearly says to place the order ("yes", "that's it", "place it").

MENU:
{menu_for_prompt(catalog)}"""


def _render_history(history: list[tuple[str, str]], max_turns: int = 6) -> str:
    """Only the last few turns: enough to resolve "medium" -> doneness, small enough to stay cheap."""
    recent = history[-max_turns:]
    return "\n".join(f"{speaker}: {text}" for speaker, text in recent) or "(none)"


def parse_order(text: str, current_cart: Cart, history: list[tuple[str, str]]) -> tuple[ParsedOrder, ParseStats]:
    """One LLM call. Returns the parsed order plus latency/token stats."""
    user_message = (
        f"Current cart (JSON):\n{current_cart.model_dump_json()}\n\n"
        f"Recent conversation:\n{_render_history(history)}\n\n"
        f"Customer's latest message:\n{text}"
    )

    started = time.perf_counter()
    response = get_client().beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
        output_format=ParsedOrder,  # structured output: the reply MUST match this schema
        output_config={"effort": "low"},  # simple extraction -> low effort = faster + cheaper
        betas=[FALLBACK_BETA],
        fallbacks=FALLBACKS,
    )
    stats = ParseStats(
        latency_s=time.perf_counter() - started,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ParseError(f"Model returned no parsable order (stop_reason={response.stop_reason}).")
    return response.parsed_output, stats
