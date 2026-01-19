from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
    TradeStatus,
)
from funding_bot.services.market_data import OrderbookSnapshot, PriceSnapshot
from funding_bot.services.positions import PositionManager


def _make_open_trade() -> Trade:
    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("2000"),
        entry_apy=Decimal("0.50"),
        entry_spread=Decimal("0.001"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(hours=10)
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = Decimal("2000")
    trade.leg2.entry_price = Decimal("2000")
    return trade


@pytest.mark.asyncio
async def test_close_trade_marks_closed_only_when_positions_flat(monkeypatch):
    monkeypatch.setattr("funding_bot.services.positions.close.asyncio.sleep", AsyncMock())

    trade = _make_open_trade()
    settings = MagicMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="ETH",
        lighter_price=Decimal("2000"),
        x10_price=Decimal("2000"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_orderbook.return_value = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("1999"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1999"),
        x10_ask=Decimal("2001"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("10"),
    )

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.tick_size = Decimal("0.01")
        return info

    market_data.get_market_info.side_effect = _market_info

    placed: list[OrderRequest] = []
    orders_by_id: dict[str, Order] = {}

    async def _place_order(request: OrderRequest) -> Order:
        placed.append(request)
        order_id = f"{request.exchange.value.lower()}_{len(placed)}"
        order = Order(
            order_id=order_id,
            symbol=request.symbol,
            exchange=request.exchange,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            price=request.price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            avg_fill_price=request.price or Decimal("2000"),
            fee=Decimal("0"),
        )
        orders_by_id[order_id] = order
        return order

    async def _get_order(_symbol: str, order_id: str) -> Order | None:
        return orders_by_id.get(order_id)

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.place_order = AsyncMock(side_effect=_place_order)
    lighter.get_order = AsyncMock(side_effect=_get_order)
    lighter.cancel_order = AsyncMock(return_value=True)
    lighter_pos = Position(
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        qty=Decimal("1"),
        entry_price=Decimal("2000"),
        mark_price=Decimal("2000"),
    )
    lighter_calls = {"n": 0}

    async def _lighter_get_position(_symbol: str) -> Position | None:
        lighter_calls["n"] += 1
        return lighter_pos if lighter_calls["n"] == 1 else None

    lighter.get_position = AsyncMock(side_effect=_lighter_get_position)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.place_order = AsyncMock(side_effect=_place_order)
    x10.get_order = AsyncMock(side_effect=_get_order)
    x10.cancel_order = AsyncMock(return_value=True)
    x10_pos = Position(
        symbol="ETH",
        exchange=Exchange.X10,
        side=Side.SELL,
        qty=Decimal("1"),
        entry_price=Decimal("2000"),
        mark_price=Decimal("2000"),
    )
    x10_calls = {"n": 0}

    async def _x10_get_position(_symbol: str) -> Position | None:
        x10_calls["n"] += 1
        return x10_pos if x10_calls["n"] == 1 else None

    x10.get_position = AsyncMock(side_effect=_x10_get_position)

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)

    result = await pm.close_trade(trade, "unit_test_exit")

    assert result.success is True
    assert trade.status == TradeStatus.CLOSED
    assert trade.leg1.exit_price > 0
    assert trade.leg2.exit_price > 0
    assert event_bus.publish.await_count == 1

    assert len(placed) >= 2
    assert all(r.reduce_only is True for r in placed)
    assert any(r.exchange == Exchange.LIGHTER and r.time_in_force == TimeInForce.POST_ONLY for r in placed)
    assert any(r.exchange == Exchange.X10 and r.time_in_force == TimeInForce.IOC for r in placed)


@pytest.mark.asyncio
async def test_close_trade_does_not_mark_closed_when_positions_still_open(monkeypatch):
    monkeypatch.setattr("funding_bot.services.positions.close.asyncio.sleep", AsyncMock())

    trade = _make_open_trade()
    settings = MagicMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="ETH",
        lighter_price=Decimal("2000"),
        x10_price=Decimal("2000"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_orderbook.return_value = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("1999"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1999"),
        x10_ask=Decimal("2001"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("10"),
    )

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.tick_size = Decimal("0.01")
        return info

    market_data.get_market_info.side_effect = _market_info

    orders_by_id: dict[str, Order] = {}

    async def _place_order(request: OrderRequest) -> Order:
        order_id = f"{request.exchange.value.lower()}_1"
        order = Order(
            order_id=order_id,
            symbol=request.symbol,
            exchange=request.exchange,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            price=request.price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            avg_fill_price=request.price or Decimal("2000"),
            fee=Decimal("0"),
        )
        orders_by_id[order_id] = order
        return order

    async def _get_order(_symbol: str, order_id: str) -> Order | None:
        return orders_by_id.get(order_id)

    still_open = Position(
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        qty=Decimal("1"),
        entry_price=Decimal("2000"),
        mark_price=Decimal("2000"),
    )

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.place_order = AsyncMock(side_effect=_place_order)
    lighter.get_order = AsyncMock(side_effect=_get_order)
    lighter.cancel_order = AsyncMock(return_value=True)
    lighter.get_position = AsyncMock(return_value=still_open)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.place_order = AsyncMock(side_effect=_place_order)
    x10.get_order = AsyncMock(side_effect=_get_order)
    x10.cancel_order = AsyncMock(return_value=True)
    x10.get_position = AsyncMock(return_value=None)

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)

    result = await pm.close_trade(trade, "unit_test_exit")

    assert result.success is False
    assert trade.status != TradeStatus.CLOSED
    assert trade.status == TradeStatus.CLOSING
    # Close failures should emit an alert, but must NOT emit TradeClosed.
    assert event_bus.publish.await_count == 1
    published = event_bus.publish.await_args_list[0].args[0]
    assert getattr(published, "event_type", "") == "AlertEvent"


@pytest.mark.asyncio
async def test_close_trade_still_closes_x10_when_lighter_get_order_hangs(monkeypatch):
    # Patch sleeps in the close module so IOC waits don't slow the test.
    monkeypatch.setattr("funding_bot.services.positions.close.asyncio.sleep", AsyncMock())

    trade = _make_open_trade()

    settings = MagicMock()
    settings.execution = MagicMock()
    settings.execution.close_get_order_timeout_seconds = 0.1
    settings.trading = MagicMock()
    settings.trading.early_tp_fast_close_enabled = True
    settings.trading.early_tp_fast_close_use_taker_directly = False
    settings.trading.early_tp_fast_close_total_timeout_seconds = (
        0.0  # force direct taker fallback in _close_lighter_smart
    )
    settings.trading.early_tp_fast_close_attempt_timeout_seconds = 0.0
    settings.trading.early_tp_fast_close_max_attempts = 1

    store = AsyncMock()
    event_bus = AsyncMock()

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="ETH",
        lighter_price=Decimal("2000"),
        x10_price=Decimal("2000"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_orderbook.return_value = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("1999"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1999"),
        x10_ask=Decimal("2001"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("10"),
    )

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.tick_size = Decimal("0.01")
        return info

    market_data.get_market_info.side_effect = _market_info

    never = asyncio.Event()

    async def _place_order(request: OrderRequest) -> Order:
        return Order(
            order_id=f"{request.exchange.value.lower()}_1",
            symbol=request.symbol,
            exchange=request.exchange,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            price=request.price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            avg_fill_price=request.price or Decimal("2000"),
            fee=Decimal("0"),
        )

    async def _get_order_hangs(_symbol: str, _order_id: str) -> Order | None:
        await never.wait()
        return None

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.place_order = AsyncMock(side_effect=_place_order)
    lighter.get_order = AsyncMock(side_effect=_get_order_hangs)
    lighter_pos = Position(
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        qty=Decimal("1"),
        entry_price=Decimal("2000"),
        mark_price=Decimal("2000"),
    )
    lighter_calls = {"n": 0}

    async def _lighter_get_position(_symbol: str) -> Position | None:
        lighter_calls["n"] += 1
        return lighter_pos if lighter_calls["n"] == 1 else None

    lighter.get_position = AsyncMock(side_effect=_lighter_get_position)

    x10 = MagicMock()
    x10.exchange = Exchange.X10
    x10.place_order = AsyncMock(side_effect=_place_order)
    x10.get_order = AsyncMock(
        return_value=Order(
            order_id="x10_1",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            qty=Decimal("1"),
            price=Decimal("2000"),
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("2000"),
            fee=Decimal("0"),
        )
    )
    x10_pos = Position(
        symbol="ETH",
        exchange=Exchange.X10,
        side=Side.SELL,
        qty=Decimal("1"),
        entry_price=Decimal("2000"),
        mark_price=Decimal("2000"),
    )
    x10_calls = {"n": 0}

    async def _x10_get_position(_symbol: str) -> Position | None:
        x10_calls["n"] += 1
        return x10_pos if x10_calls["n"] == 1 else None

    x10.get_position = AsyncMock(side_effect=_x10_get_position)

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)

    result = await pm.close_trade(trade, "EARLY_TAKE_PROFIT unit_test")

    assert result.success is True
    assert x10.place_order.await_count >= 1
