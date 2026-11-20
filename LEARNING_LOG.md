# Learning log: concepts, decisions, and talking points

Each section has the **concept**, the **decision I made and its tradeoff**, and **talking points**
phrased the way I'd say them out loud.

---

## 1. Canonical data model (`models.py`, `catalog.json`)

**Concepts**
- *Canonical model:* one agreed shape for a menu that every messy source is converted *into* and every channel reads *from*. N sources × M channels becomes N + M adapters instead of N × M.
- *Modifier groups:* choices attached to an item, each group with `min_selections` / `max_selections`. "Required" just means `min ≥ 1`. Combos are items with *slots* (side, drink), and the combo's modifiers apply to its main item.
- *Integer cents:* floats can't represent 0.10 exactly; money is always `int` cents.
- *IDs, not names:* every reference is a stable ID like `combo_3`. Names are for humans; IDs are for machines and LLMs.

**Decision:** Pydantic models validated at load time. A malformed catalog fails loudly at startup, not halfway through an order.

**Talking points**
- "Everything starts with a canonical catalog. Once the menu is one clean shape with stable IDs, every agent platform can read the same data, and the LLM's job shrinks to mapping words onto IDs."
- "I store prices as integer cents. It's a small thing, but a POS total that's off by a penny is a real bug."

---

## 2. Messy sources and entity resolution (`ingest.py`, `text.py`)

**Concepts**
- *Entity resolution:* deciding which records from different systems are the same real-world thing ("LG FRY" = "Large Fries").
- *Normalization:* deterministic cleanup first (lowercase, punctuation, expanding abbreviations).
- *Fuzzy matching with confidence thresholds:* ≥90 auto-accept, 75–90 ambiguous, <75 no match.
- *Tiered escalation:* rules → fuzzy → LLM (only for the gray zone) → human. Each tier is more expensive and handles fewer cases.
- *Provenance:* record where each value came from (`chosen_from: "pos:1001"`).
- *Source priority:* the POS wins price conflicts because it's what actually charges the card. The delivery app never sets prices because they're inflated to cover commission.

**Decision:** a subset match ("#1 combo" ⊂ "#1 bolt classic combo") is capped at 85, so it can *never* auto-accept. This matters because "truffle fries" contains "fries". A confident wrong match is worse than asking a human.

**What actually happened:** my first scorer matched "DBL BOLT" to the *combo* instead of the burger. Tests now lock that case down.

**Talking points**
- "Ingestion is a confidence ladder. Cheap deterministic rules handle most records, fuzzy matching handles typos and abbreviations, an LLM only judges the ambiguous pairs, and anything still uncertain goes to a human review queue."
- "Human decisions are saved and re-applied on the next run, so a person never has to make the same call twice. Over time the system learns the restaurant's quirks."
- "Every resolved field keeps its provenance, so when a manager asks why the burger is $8.99, I can point to POS button 1001."

---

## 3. Rules and tribal knowledge (`rules.json`, `validator.py`)

**Concepts**
- *Rules as data:* "doneness required for beef", "breakfast 6–11", "shakes sold out", "$1 off combos Tuesday" live in JSON, not in `if` statements.
- *Tags:* rules target tags (`beef`, `breakfast`, `combo`) rather than individual items, so a new burger automatically gets the doneness rule.
- *Discriminated union:* the `type` field picks the Pydantic class, so a typo in a rule fails at load time.

**Decision:** rules *override* catalog data. The catalog still says doneness is optional (that's what the POS exports), and the rule makes it required. Keeping them separate means re-importing the POS never wipes out the knowledge a cashier gave us.

**Talking points**
- "The hardest data isn't in any system. It's in a cashier's head: 'we always ask doneness', 'the shake machine is down'. I capture that as rules a manager can edit, not code an engineer has to deploy."
- "To collect it I'd interview staff, mine past order transcripts for questions cashiers repeatedly ask, and watch which orders get voided or corrected in the POS."

---

## 4. Deterministic validator (`validator.py`, `tests/test_validator.py`)

**Concepts**
- *"The LLM proposes, deterministic code disposes."* The model suggests a cart; code decides whether it's real, complete, available, and what it costs.
- *Structured errors:* each issue has a stable `code` for programs, a `message` that tells an agent how to fix it, and a `question` to text the customer.
- *Injected time:* `validate_cart(..., now)` takes the time as a parameter, so tests can be 8am on a Tuesday.

**Decision:** the validator never calls the LLM, and the reply text is built from validator output. So the bot can't hallucinate a price or promise a sold-out item.

**Talking points**
- "If the LLM invents an item, say truffle fries, it becomes an `UNKNOWN_ITEM` issue, not a kitchen ticket. The model can be wrong; the order can't."
- "The validator is pure and deterministic, so it gets the heaviest testing: 19 tests that run in half a second."
- "Error messages are written for two readers. The code is for programs, the question is for the customer, and the message tells an agent which tool to call to fix it."

---

## 5. LLM parser and conversation loop (`parser.py`, `conversation.py`)

**Concepts**
- *Grounding:* the prompt contains the real menu with IDs.
- *Structured outputs:* `messages.parse(output_format=ParsedOrder)` forces schema-valid JSON, so there's no regex parsing of prose.
- *Don't let the model guess:* the prompt says never default a doneness or drink. Missing info goes to the validator, which asks.
- *Stateless parser, stateful service:* each call gets the current cart + recent history and returns the FULL new cart. Our code owns the state.
- *Small context:* only the last 6 turns, compact menu text. That means less cost, less latency, less distraction.
- *Effort `low`:* parsing is simple extraction, so less thinking means faster and cheaper.
- *Per-conversation lock:* two quick texts from one customer can't race and overwrite the cart; different customers never block each other.

**Decision:** return the whole cart each turn rather than a diff. It costs a few extra tokens, but it's simpler and self-correcting: a mistake in turn 2 can be fixed in turn 3.

**Talking points**
- "I use the LLM for exactly one thing: understanding messy language. 'Two number 3s no pickles, one with a coke' becomes a list of IDs. Everything else is code."
- "Structured outputs mean I never parse prose. The response either matches my schema or it's rejected."
- "The model is told not to fill in missing choices. If it guessed 'medium' for you, that's a wrong order. Asking is cheaper than a remake."
- "Cost and latency: one call per message with a small context at low effort. With a menu of thousands I'd retrieve candidate items first and only send those."

---

## 6. Agent-facing API and MCP (`agent_tools.py`, `api.py`, `mcp_server.py`, `agent_demo.py`)

**Concepts**
- *Agent-readable:* an outside AI can discover what's on the menu, what's required, and what it costs, all through typed tools rather than by scraping a website.
- *MCP (Model Context Protocol):* a standard way for AI apps to list and call tools. The app launches our server, calls `tools/list` (names, descriptions, JSON schemas), then `tools/call`.
- *Tool design for an LLM reader:* clear names, descriptions that explain the workflow, and errors that say what to do next ("Call search_menu to find valid item IDs").
- *Tool-use loop:* Claude replies with `tool_use` blocks → we run them → send `tool_result` blocks → repeat until `end_turn`.
- *Neutral layer:* the same 4 functions sit behind REST, MCP, and our own agent. One source of truth, many platforms.

**Decision:** `place_order` validates internally. An agent can't skip validation, even if it forgets to call `validate_order`.

**Talking points**
- "Every agent platform (ChatGPT, Claude, Gemini, Meta) wants to order food, and no restaurant wants to integrate with each one. A neutral layer exposes one well-designed tool surface and all of them can use it."
- "Tool descriptions and error messages are the agent's UI. When an agent sends a combo without a drink, it gets back 'INCOMPLETE_COMBO: needs a drink, allowed: coke, sprite…', which is enough to fix itself or ask the user."
- "The LLM never touches the database or the POS. It can only ask us to run a tool, and every tool goes through the validator."

---

## 7. POS adapter and idempotency (`pos.py`)

**Concepts**
- *Adapter pattern:* the rest of the code only knows `POSAdapter`. Square or Toast would be another class with the same methods.
- *Source of record:* the POS owns the real order; we map canonical IDs to POS IDs (from ingestion).
- *Idempotency key:* if a request times out after the POS accepted it and we retry with the same key, we get the original order back, not a second burger and a second charge.

**Talking points**
- "Networks fail after the work is done. Without idempotency keys, a retry means a double order. With them, retries are always safe."
- "Adding a new POS is one new adapter class. The validator, parser, and channels don't change."

---

## 8. Evals (`evals/`)

**Concepts**
- *Eval set:* realistic messy orders (typos, slang, combos, sold-out, not-on-menu) with expected carts.
- *Metrics:* cart exact match, issue-code accuracy, clarification rate, latency, cost per order.
- *Order-insensitive grading:* carts compared as multisets, so `2× combo` equals `1× + 1×`.

**Talking points**
- "Evals are how you change a prompt without guessing. I run the set before and after, and if exact-match drops, the change doesn't ship."
- "Clarification rate matters as much as accuracy. A bot that asks five questions per order is accurate and useless."

---

## Likely interview questions (with my answers)

- **"What happens if the LLM invents an item?"** It returns an ID that isn't in the catalog. The validator flags `UNKNOWN_ITEM`, nothing goes to the POS, and the customer gets a question.
- **"What if the customer says 'yes' but the cart is incomplete?"** Confirmation never overrides the validator. We ask the missing question first (there's a test for this).
- **"Why not let the LLM compute the total?"** LLMs are bad at exact arithmetic and can't be audited. Code computes totals in integer cents.
- **"How do you handle 1,000 items?"** Fuzzy-search the message for candidate items and only put those in the prompt. Optionally constrain IDs with an enum in the schema.
- **"Two texts arrive at once from one customer?"** A per-conversation lock processes them in order, so the second sees the first's cart.
- **"How do you know your prompt change helped?"** Run the evals before and after and compare exact-match, clarification rate, latency, and cost.
- **"Why MCP and REST both?"** REST for any developer or integration; MCP so AI agents can discover and use the tools with zero custom glue. Both wrap the same functions.
