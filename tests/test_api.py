"""REST API tests via FastAPI's TestClient (no server process needed)."""

from fastapi.testclient import TestClient

from zappd.api import app

client = TestClient(app)


def test_search_finds_combo_by_slang() -> None:
    results = client.get("/menu/search", params={"q": "number 3"}).json()
    assert results[0]["item_id"] == "combo_3"


def test_search_understands_pos_abbreviations() -> None:
    results = client.get("/menu/search", params={"q": "lg fry"}).json()
    assert results[0]["item_id"] == "fries_large"  # not regular fries


def test_item_details_show_rule_based_required_flag() -> None:
    details = client.get("/menu/items/bolt_classic").json()
    doneness = next(g for g in details["modifier_groups"] if g["group_id"] == "doneness")
    assert doneness["required"] is True  # catalog says optional; the rule wins


def test_unknown_item_is_404() -> None:
    assert client.get("/menu/items/pizza").status_code == 404


def test_bad_json_shape_is_rejected_by_pydantic() -> None:
    response = client.post("/orders/validate", json={"lines": [{"quantity": 2}]})  # no item_id
    assert response.status_code == 422


def test_place_order_is_idempotent() -> None:
    cart = {"lines": [{"item_id": "coke", "quantity": 2}]}
    headers = {"Idempotency-Key": "test-key-123"}
    first = client.post("/orders", json=cart, headers=headers).json()
    second = client.post("/orders", json=cart, headers=headers).json()
    assert first["placed"] and first["pos_order_id"] == second["pos_order_id"]


def test_invalid_order_is_not_placed() -> None:
    response = client.post("/orders", json={"lines": [{"item_id": "double_bolt"}]}, headers={"Idempotency-Key": "k2"})
    assert response.status_code == 422
    assert response.json()["detail"]["issues"][0]["code"] == "MISSING_REQUIRED_MODIFIER"
