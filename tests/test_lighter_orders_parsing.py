import pytest

from src.adapters.lighter_adapter import LighterAdapter, _normalize_lighter_orders_response


def test_normalize_lighter_orders_response_shapes():
    order = {"id": 1, "status": 0}

    assert _normalize_lighter_orders_response([order]) == [order]
    assert _normalize_lighter_orders_response({"orders": [order], "code": 0}) == [order]
    assert _normalize_lighter_orders_response({"orders": {"51": [order]}, "code": 0}) == [order]
    assert _normalize_lighter_orders_response({"data": [order]}) == [order]
    assert _normalize_lighter_orders_response({"result": [order]}) == [order]
    assert _normalize_lighter_orders_response(None) == []


@pytest.mark.asyncio
async def test_get_open_orders_falls_back_market_param_name(monkeypatch):
    adapter = LighterAdapter()

    # Avoid calling networked resolvers
    adapter._resolved_account_index = 123
    # Simulate markets where market_id != market_index.
    # If the adapter incorrectly reuses the same value for both params,
    # it will miss open orders.
    adapter.market_info = {"TEST-USD": {"market_id": 51, "market_index": 999}}

    seen_params = []

    async def fake_rest_get(path, params=None):
        assert path == "/api/v1/orders"
        seen_params.append(dict(params or {}))

        # Simulate: only the correct market_index value returns the order.
        # This catches bugs where market_index param is sent with market_id's value.
        if (params or {}).get("market_index") != 999:
            return {"orders": [], "code": 0}

        return {
            "orders": {
                "999": [
                    {
                        "id": 987,
                        "status": 0,
                        "price": "1.234",
                        "size": "2.5",
                        "is_ask": False,
                    }
                ]
            },
            "code": 0,
        }

    monkeypatch.setattr(adapter, "_rest_get", fake_rest_get)

    open_orders = await adapter.get_open_orders("TEST-USD")

    assert len(open_orders) == 1
    assert open_orders[0]["id"] == "987"
    assert open_orders[0]["side"] == "BUY"

    # Ensure we tried market_id first, then market_index.
    assert len(seen_params) >= 2
    assert "market_id" in seen_params[0]
    assert "market_index" in seen_params[1]
    assert seen_params[1]["market_index"] == 999
