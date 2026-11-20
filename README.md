# Bolt Burger: a mini "neutral layer for ordering"

A small, working version of agent-readable menu infrastructure for a fictional restaurant.
It takes messy menu data, builds one canonical catalog with rules, lets an LLM turn free text into
a structured cart, validates that cart with deterministic code, and exposes it all to AI agents
over REST and MCP.

## Architecture

```
  MESSY SOURCES                         INGESTION (offline, run when menus change)
  pos_export.csv  ─┐
  website.json    ─┼──► ingest.py ──► normalize ─► fuzzy match ─► [LLM judge] ─► human review queue
  delivery_app.json┘     (text.py)                     │
                                                       ├──► pos_id_map.json   (canonical id → POS id)
                                                       └──► price conflicts   (POS wins; delivery app ignored)

  CANONICAL DATA                        catalog.json  +  rules.json  (tribal knowledge as data)
                                              │  loaded + validated by catalog.py into models.py types
                                              ▼
  ORDERING (online, per message)
                 "2 number 3s no pickles"
                          │
             ┌────────────┴─────────────┐
       parser.py  (LLM)            validator.py  (NO LLM)
       text ─► Cart of IDs  ─────► Cart ─► issues + exact cents
                                         │
                              conversation.py: ask question │ read back │ place
                                         │
                                    pos.py (MockPOS, idempotency keys)

  CHANNELS (who can order)
     api.py      REST  /menu  /menu/search  /orders/validate  /orders  /sms
     mcp_server.py     MCP tools for Claude Desktop / Claude Code / any agent
     agent_demo.py     our own Claude agent loop using the same tools
     chat.py           terminal chat (same path as /sms)
           └── all of them call agent_tools.py ──► validator.py ──► pos.py
```

**The core idea:** *the LLM proposes, deterministic code disposes.* The LLM only turns human
language into IDs. Everything that must be correct (what exists, what's required, what's available,
what it costs) is decided by plain code reading plain data.

## What every file does

| File | Role | Calls / is called by |
|---|---|---|
| [zappd/models.py](zappd/models.py) | All data shapes (Pydantic): Catalog, Item, ModifierGroup, Rule types, CartLine, Issue, ValidationResult | Imported by everything |
| [zappd/text.py](zappd/text.py) | `normalize()`: "LG FRY" → "large fries" | catalog.py (search), ingest.py (matching) |
| [zappd/catalog.py](zappd/catalog.py) | Loads catalog.json + rules.json; lookups; fuzzy `search_items`; `menu_for_prompt` for the LLM | Everyone who needs menu data |
| [zappd/validator.py](zappd/validator.py) | `validate_cart()`: the trust layer. Existence, availability, required modifiers, combos, promos, totals | conversation.py, agent_tools.py, evals |
| [zappd/llm.py](zappd/llm.py) | The one place that builds the Anthropic client; model name; fallback settings; cost estimate | parser.py, agent_demo.py, ingest.py |
| [zappd/parser.py](zappd/parser.py) | `parse_order()`: ONE LLM call, structured output → `ParsedOrder` | conversation.py, evals |
| [zappd/conversation.py](zappd/conversation.py) | `OrderingService.handle_message()`: parse → validate → reply; per-customer state + locks | api.py `/sms`, chat.py |
| [zappd/pos.py](zappd/pos.py) | `POSAdapter` interface + `MockPOS` with idempotency keys and canonical→POS id mapping | conversation.py, agent_tools.py |
| [zappd/agent_tools.py](zappd/agent_tools.py) | The 4 capabilities: `search_menu`, `get_item`, `validate_order`, `place_order` | api.py, mcp_server.py, agent_demo.py |
| [zappd/api.py](zappd/api.py) | FastAPI REST + SMS webhook | HTTP clients |
| [zappd/mcp_server.py](zappd/mcp_server.py) | MCP server exposing the 4 tools over stdio | Claude Desktop / Claude Code |
| [zappd/agent_demo.py](zappd/agent_demo.py) | A hand-written Claude tool-use loop that orders using the 4 tools | You, from the terminal |
| [zappd/chat.py](zappd/chat.py) | Terminal chat with the ordering bot | You, from the terminal |
| [zappd/ingest.py](zappd/ingest.py) | Entity resolution over the 3 messy sources | You, offline |
| [scripts/mcp_client_demo.py](scripts/mcp_client_demo.py) | Acts like Claude Desktop: launches the MCP server and calls tools by hand (no LLM) | You |
| [evals/run_evals.py](evals/run_evals.py) | Runs 12 messy orders through parser + validator, prints accuracy/latency/cost | You, after prompt changes |
| [data/](data/) | `catalog.json`, `rules.json`, `sources/`, `review_decisions.json`, generated `pos_id_map.json` | Read by catalog.py / ingest.py / pos.py |

## One text message, end to end

Customer texts **"a double bolt and a large fry"** → `POST /sms {"From": "+1555...", "Body": "..."}`

1. `api.sms_webhook` → `OrderingService.handle_message(phone, text)`
2. `conversation._get_conversation(phone)`: finds or creates this customer's `Conversation` (cart + history), takes its lock
3. `parser.parse_order(text, current_cart, history)`
   - builds the system prompt: rules + `catalog.menu_for_prompt()` (items with IDs)
   - **LLM call:** `client.beta.messages.parse(..., output_format=ParsedOrder)`
   - Claude returns JSON matching the schema: `{"lines": [{"item_id": "double_bolt"}, {"item_id": "fries_large"}], "not_on_menu": [], "customer_confirmed": false}`
   - the SDK validates it into a `ParsedOrder` object
4. `validator.validate_cart(cart, catalog, rules, now)`
   - `double_bolt` exists ✓, available ✓, has tag `beef`
   - rule `burger_doneness_required` raises doneness from optional → required → **MISSING_REQUIRED_MODIFIER**, question "How would you like your burger cooked…?"
   - `fries_large` ✓ → priced at 449 cents
5. Back in `conversation._step`: there are issues, so the reply is the issue's question. **The LLM didn't write the reply.**
6. Customer: **"medium"** → same path. The parser gets the current cart + history, so it returns the full cart with `doneness_medium` added. The validator says ok, and the reply is a read-back with the total and "Reply YES".
7. Customer: **"yes"** → `customer_confirmed=true` and the cart is valid → `pos.submit_order(..., idempotency_key="+1555...:1")` → "Order placed! #POS-0001"

## Where LLMs are called (and where they aren't)

| Where | How | Why that way |
|---|---|---|
| `parser.parse_order` | One `messages.parse` call with **structured output** (Pydantic schema) at `effort: low` | We control the flow; we just need text → JSON. Cheap, fast, predictable |
| `agent_demo.run_agent` | **Tool use loop**: Claude chooses which tool to call; we execute it and send back results | Simulates an outside agent (ChatGPT, Claude…) that decides its own steps |
| `ingest.llm_judge` | Structured output yes/no, **only for ambiguous fuzzy matches** (`--llm`) | Fuzzy matching handles 90%; LLM for the gray zone; humans for the rest |
| `mcp_server.py` | **We don't call an LLM here.** The agent app's LLM calls *us* | That's what "agent-readable" means |
| `validator.py` | **Never** | Correctness must be deterministic and testable |

All calls use `claude-opus-5-5` with server-side refusal fallbacks enabled (`llm.py`).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then paste your ANTHROPIC_API_KEY into .env
```

## Commands

| What | Command | Needs API key? |
|---|---|---|
| Tests (34) | `python -m pytest -q` | no |
| Ingestion report | `python -m zappd.ingest` (add `--llm` for the LLM judge) | no (yes with `--llm`) |
| REST API + docs | `uvicorn zappd.api:app --reload` → http://127.0.0.1:8000/docs | only `/sms` |
| MCP protocol demo | `python scripts/mcp_client_demo.py` | no |
| Chat with the bot | `python -m zappd.chat` | yes |
| Claude agent orders via tools | `python -m zappd.agent_demo` | yes |
| Evals | `python evals/run_evals.py` | yes (~12 calls, a few cents) |
| Connect to Claude Code | `claude mcp add bolt-burger -- C:\path\to\ZappdDemo\.venv\Scripts\python.exe -m zappd.mcp_server` (run from the project folder) | Claude Code's own |

For **Claude Desktop**, add this to `claude_desktop_config.json`:
```json
{"mcpServers": {"bolt-burger": {
  "command": "C:\\Users\\<you>\\projects\\ZappdDemo\\.venv\\Scripts\\python.exe",
  "args": ["-m", "zappd.mcp_server"],
  "cwd": "C:\\Users\\<you>\\projects\\ZappdDemo"}}}
```

## 3-minute demo script

1. **The problem (30s):** open `data/sources/`. The same burger appears under three names at three prices, there are duplicate POS buttons, and doneness is marked optional in the POS.
2. **Ingestion (30s):** `python -m zappd.ingest`. Point out the duplicate buttons, POS winning price conflicts, and "Truffle Fries" routed to a human instead of being matched to fries.
3. **Rules as data (20s):** open `data/rules.json`. Flip `shake_machine_down` and explain that a manager can do the same.
4. **Ordering (60s):** `python -m zappd.chat` → "2 number 3s no pickles, one w/ a coke, and a large fry". Watch it ask for the missing side/drink, read the order back with an exact total, then place it.
5. **Agents (40s):** `python -m zappd.agent_demo`. Claude searches, validates, learns the shake is sold out, and places the order, all through the same tools the MCP server exposes.

## Tradeoffs and what I'd do next

- **State is in memory** (dict per phone number). Next: Redis/Postgres so it survives restarts and scales horizontally.
- **JSON files instead of a DB.** Fine for one location; `catalog.py` is the only reader, so swapping storage touches one file.
- **Whole menu in the prompt.** Fine at 20 items. At 2,000, retrieve candidates with `search_items` first and send only those.
- **IDs are plain strings in the LLM schema.** Next: generate the schema with an `enum` of valid IDs so the model *can't* emit an unknown one.
- **Mock POS.** Next: a Square sandbox adapter behind the same `POSAdapter` interface, with order-status webhooks and retries with backoff.
- **12 eval cases.** Next: 50+ from real transcripts, run in CI on every prompt change.
