from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Order, OrderStatus, OrderType, Side
from funding_bot.services.execution import ExecutionEngine


@pytest.mark.asyncio
async def test_wait_for_fill_uses_ws_client_key_for_pending_lighter_orders(monkeypatch):
    settings = MagicMock()
    settings.execution.ws_fill_wait_enabled = True
    # Keep polling far away so the test is truly WS-driven.
    settings.execution.ws_fill_wait_fallback_poll_seconds = Decimal("10.0")

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.subscribe_orders = AsyncMock()
    lighter.get_order = AsyncMock(return_value=None)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.subscribe_orders = AsyncMock()

    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    pending_order_id = "pending_123_7"
    task = asyncio.create_task(engine._wait_for_fill(lighter, "ETH", pending_order_id, timeout=1.0))

    # Wait until _wait_for_fill registered its watcher (avoid race with WS update).
    for _ in range(200):
        async with engine._ws_orders_lock:
            if (Exchange.LIGHTER, "client:123") in engine._order_watchers:
                break
        await asyncio.sleep(0)
    else:
        raise AssertionError("watcher for client:123 was not registered")

    filled = Order(
        order_id="999",
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        status=OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
        fee=Decimal("0"),
        client_order_id="123",
    )

    await engine._on_ws_order_update(filled)
    result = await task

    assert result is filled
    assert lighter.get_order.await_count == 0
