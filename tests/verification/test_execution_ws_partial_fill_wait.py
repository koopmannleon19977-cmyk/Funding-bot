from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Order, OrderStatus, OrderType, Side
from funding_bot.services.execution import ExecutionEngine


@pytest.mark.asyncio
async def test_wait_for_fill_does_not_return_on_partial_fill_ws_update():
    settings = MagicMock()
    settings.execution.ws_fill_wait_enabled = True
    # Keep polling far away so the test is WS-driven.
    settings.execution.ws_fill_wait_fallback_poll_seconds = Decimal("10.0")

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.subscribe_orders = AsyncMock()
    lighter.ws_ready = MagicMock(return_value=True)
    lighter.get_order = AsyncMock(return_value=None)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.subscribe_orders = AsyncMock()

    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    task = asyncio.create_task(engine._wait_for_fill(lighter, "ETH", "123", timeout=2.0))

    # Wait until watcher is registered (avoid race with WS update).
    for _ in range(200):
        async with engine._ws_orders_lock:
            if (Exchange.LIGHTER, "123") in engine._order_watchers:
                break
        await asyncio.sleep(0)
    else:
        raise AssertionError("watcher was not registered")

    partial = Order(
        order_id="123",
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=Decimal("0.1"),
        avg_fill_price=Decimal("100"),
        fee=Decimal("0"),
    )
    await engine._on_ws_order_update(partial)
    await asyncio.sleep(0)

    assert task.done() is False

    cancelled = Order(
        order_id="123",
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        status=OrderStatus.CANCELLED,
        filled_qty=Decimal("0.1"),
        avg_fill_price=Decimal("100"),
        fee=Decimal("0"),
    )
    await engine._on_ws_order_update(cancelled)
    result = await task

    assert result.status == OrderStatus.CANCELLED
    assert result.filled_qty == Decimal("0.1")
