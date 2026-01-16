from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.domain.models import Exchange


@pytest.mark.asyncio
async def test_lighter_ws_account_update_flattens_orders_dict_of_lists():
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._market_id_to_symbol = {7: "ETH"}

    # Simulate an active WS task so ws_ready() can become True once a message is received.
    adapter._ws_task = MagicMock()
    adapter._ws_task.done.return_value = False

    cb = AsyncMock()
    await adapter.subscribe_orders(cb)

    msg = {
        "type": "update/account_all",
        "channel": "account_all:123",
        "orders": {
            "7": [
                {
                    "order_id": "999",
                    "order_index": 999,
                    "client_order_index": 12345,
                    "market_index": 7,
                    "status": "filled",
                    "side": "buy",
                    "initial_base_amount": "1",
                    "filled_base_amount": "1",
                    "filled_quote_amount": "100",
                    "remaining_base_amount": "0",
                    "price": "100",
                }
            ]
        },
        "positions": {},
    }

    adapter._on_ws_account_update(123, msg)
    await asyncio.sleep(0)

    assert adapter._ws_account_ready.is_set()
    cb.assert_awaited()
    order = cb.await_args.args[0]
    assert order.exchange == Exchange.LIGHTER
    assert order.order_id == "999"
    assert order.client_order_id == "12345"
    assert adapter._client_order_id_map["12345"] == "999"


@pytest.mark.asyncio
async def test_wait_for_fill_falls_back_to_polling_when_ws_not_ready(monkeypatch):
    from funding_bot.services.execution import ExecutionEngine

    settings = MagicMock()
    settings.execution.ws_fill_wait_enabled = True
    settings.execution.ws_fill_wait_fallback_poll_seconds = Decimal("10.0")

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.subscribe_orders = AsyncMock()
    lighter.ws_ready = MagicMock(return_value=False)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.subscribe_orders = AsyncMock()

    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)
    sentinel = MagicMock()
    engine._wait_for_fill_polling = AsyncMock(return_value=sentinel)

    result = await engine._wait_for_fill(lighter, "ETH", "123", timeout=1.0)

    assert result is sentinel
    engine._wait_for_fill_polling.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_orders_ws_market_does_not_accept_stale_market_ready_event(monkeypatch):
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._markets = {"ETH": MagicMock()}
    adapter._get_market_index = MagicMock(return_value=7)  # type: ignore[method-assign]

    # Pretend a previous run set the market-ready event, but the WS is currently not connected.
    ev = adapter._get_orders_ws_market_event(7)
    ev.set()
    assert ev.is_set()
    assert adapter.ws_ready() is False

    adapter._start_orders_websocket = AsyncMock()  # type: ignore[method-assign]

    ok = await adapter.ensure_orders_ws_market("ETH", timeout=0.01)

    assert ok is False
    assert ev.is_set() is False


@pytest.mark.asyncio
async def test_ensure_orders_ws_market_accepts_market_index_zero(monkeypatch):
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._markets = {"ETH": MagicMock()}
    adapter._get_market_index = MagicMock(return_value=0)  # type: ignore[method-assign]

    adapter._orders_ws_ready.set()
    adapter.ws_ready = MagicMock(return_value=True)  # type: ignore[method-assign]
    adapter._start_orders_websocket = AsyncMock()  # type: ignore[method-assign]

    adapter._get_orders_ws_market_event(0).set()

    ok = await adapter.ensure_orders_ws_market("ETH", timeout=0.01)

    assert ok is True
    assert 0 in adapter._orders_ws_markets


@pytest.mark.asyncio
async def test_lighter_ws_orders_does_not_overestimate_fill_from_remaining_field():
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._market_id_to_symbol = {36: "CRV"}

    adapter._ws_task = MagicMock()
    adapter._ws_task.done.return_value = False

    cb = AsyncMock()
    await adapter.subscribe_orders(cb)

    msg = {
        "type": "update/account_all",
        "channel": "account_all:123",
        "orders": {
            "36": [
                {
                    "order_id": "111",
                    "client_order_index": 777,
                    "market_index": 36,
                    "status": "partially_filled",
                    "side": "buy",
                    "initial_base_amount": "420",
                    "filled_base_amount": "0.2",
                    # Some WS payloads have a misleading remaining field; we must not derive
                    # filled_qty from initial-remaining when explicit filled_* fields exist.
                    "remaining_base_amount": "0.2",
                    "price": "0.42432",
                }
            ]
        },
        "positions": {},
    }

    adapter._on_ws_account_update(123, msg)
    await asyncio.sleep(0)

    cb.assert_awaited()
    order = cb.await_args.args[0]
    assert order.symbol == "CRV"
    assert order.filled_qty == Decimal("0.2")


@pytest.mark.asyncio
async def test_lighter_ws_orders_clamps_implausible_avg_fill_price_to_order_price():
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._market_id_to_symbol = {36: "CRV"}

    adapter._ws_task = MagicMock()
    adapter._ws_task.done.return_value = False

    cb = AsyncMock()
    await adapter.subscribe_orders(cb)

    msg = {
        "type": "update/account_all",
        "channel": "account_all:123",
        "orders": {
            "36": [
                {
                    "order_id": "222",
                    "client_order_index": 888,
                    "market_index": 36,
                    "status": "filled",
                    "side": "buy",
                    "initial_base_amount": "420",
                    "filled_base_amount": "420",
                    "remaining_base_amount": "0",
                    "price": "0.42416",
                    # A mis-scaled filled_quote_amount would imply an absurd avg fill price.
                    "filled_quote_amount": "0.16968",
                }
            ]
        },
        "positions": {},
    }

    adapter._on_ws_account_update(123, msg)
    await asyncio.sleep(0)

    cb.assert_awaited()
    order = cb.await_args.args[0]
    assert order.symbol == "CRV"
    assert order.avg_fill_price == Decimal("0.42416")


@pytest.mark.asyncio
async def test_lighter_ws_orders_derives_fill_from_initial_minus_remaining_when_no_filled_field():
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)
    adapter._market_id_to_symbol = {7: "ETH"}

    adapter._ws_task = MagicMock()
    adapter._ws_task.done.return_value = False

    cb = AsyncMock()
    await adapter.subscribe_orders(cb)

    msg = {
        "type": "update/account_all",
        "channel": "account_all:123",
        "orders": {
            "7": [
                {
                    "order_id": "333",
                    "client_order_index": 999,
                    "market_index": 7,
                    "status": "open",
                    "side": "buy",
                    "initial_base_amount": "10",
                    # No filled_* fields
                    "remaining_base_amount": "7.5",
                    "price": "100",
                }
            ]
        },
        "positions": {},
    }

    adapter._on_ws_account_update(123, msg)
    await asyncio.sleep(0)

    cb.assert_awaited()
    order = cb.await_args.args[0]
    assert order.symbol == "ETH"
    assert order.filled_qty == Decimal("2.5")
