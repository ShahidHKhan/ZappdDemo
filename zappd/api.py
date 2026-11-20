"""REST API (FastAPI). Run:  uvicorn zappd.api:app --reload   then open http://127.0.0.1:8000/docs

FastAPI reads the Pydantic types in each function signature to:
  - validate the incoming JSON (bad shape -> automatic 422 error)
  - generate the interactive docs at /docs (OpenAPI) - itself agent-readable
"""

from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from zappd import agent_tools
from zappd.catalog import load_catalog
from zappd.conversation import OrderingService
from zappd.llm import LLMUnavailableError
from zappd.models import Cart, Catalog
from zappd.parser import parse_order

app = FastAPI(title="Bolt Burger - agent-readable menu", version="0.1")


@app.get("/menu")
def get_menu() -> Catalog:
    return load_catalog()


@app.get("/menu/search")
def search(q: str) -> list[dict[str, Any]]:
    return agent_tools.search_menu(q)


@app.get("/menu/items/{item_id}")
def item_details(item_id: str) -> dict[str, Any]:
    details = agent_tools.get_item(item_id)
    if "error" in details:
        raise HTTPException(status_code=404, detail=details["error"])
    return details


@app.post("/orders/validate")
def validate(cart: Cart) -> dict[str, Any]:
    return agent_tools.validate_order(cart.lines)


@app.post("/orders")
def create_order(cart: Cart, idempotency_key: str = Header(..., alias="Idempotency-Key")) -> dict[str, Any]:
    """Same Idempotency-Key twice -> same order back, never a second one."""
    result = agent_tools.place_order(cart.lines, idempotency_key, customer_id="rest")
    if not result["placed"]:
        raise HTTPException(status_code=422, detail=result)
    return result


# ---- SMS channel (Twilio-shaped). Real Twilio posts form-encoded From/Body
# and expects TwiML back; we use JSON with the same field names to stay simple.


class IncomingSms(BaseModel):
    From: str  # customer's phone number = conversation key
    Body: str


@lru_cache
def ordering_service() -> OrderingService:
    return OrderingService(parser=parse_order, pos=agent_tools.POS)


@app.post("/sms")
def sms_webhook(msg: IncomingSms) -> dict[str, str]:
    try:
        reply = ordering_service().handle_message(msg.From, msg.Body)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"reply": reply}
