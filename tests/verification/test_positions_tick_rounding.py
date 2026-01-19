from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    Side,
    TimeInForce,
    Trade,
    TradeStatus,
)
from funding_bot.services.market_data import OrderbookSnapshot, PriceSnapshot
from funding_bot.services.positions import PositionManager


@pytest.mark.asyncio
async def test_lighter_post_only_close_sell_uses_ceiling_rounding(monkeypatch):
    monkeypatch.setattr("funding_bot.services.positions.asyncio.sleep", AsyncMock())

    settings = MagicMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,  # long -> close is SELL
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("100"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC)
    trade.leg1.filled_qty = Decimal("1")

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="ETH",
        lighter_price=Decimal("100"),
        x10_price=Decimal("100"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_orderbook.return_value = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("100.002"),
        lighter_ask=Decimal("100.003"),
        x10_bid=Decimal("0"),
        x10_ask=Decimal("0"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("0"),
        x10_ask_qty=Decimal("0"),
    )

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.tick_size = Decimal("0.01")
        return info

    market_data.get_market_info.side_effect = _market_info

    captured: list[OrderRequest] = []

    async def _place_order(req: OrderRequest) -> Order:
        captured.append(req)
        return Order(
            order_id="close_1",
            symbol=req.symbol,
            exchange=req.exchange,
            side=req.side,
            order_type=req.order_type,
            qty=req.qty,
            price=req.price,
            time_in_force=req.time_in_force,
            reduce_only=req.reduce_only,
            status=OrderStatus.OPEN,
        )

    async def _get_order(_symbol: str, _order_id: str) -> Order | None:
        if not captured:
            return None
        req = captured[-1]
        return Order(
            order_id=_order_id,
            symbol=req.symbol,
            exchange=req.exchange,
            side=req.side,
            order_type=req.order_type,
            qty=req.qty,
            price=req.price,
            time_in_force=req.time_in_force,
            reduce_only=req.reduce_only,
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_fill_price=req.price or Decimal("0"),
        )

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.place_order = AsyncMock(side_effect=_place_order)
    lighter.get_order = AsyncMock(side_effect=_get_order)
    lighter.cancel_order = AsyncMock(return_value=True)

    x10 = MagicMock()
    x10.exchange = Exchange.X10

    mgr = PositionManager(settings, lighter, x10, store, event_bus, market_data)

    await mgr._close_lighter_smart(trade)

    assert captured, "expected a close order request"
    assert captured[0].time_in_force == TimeInForce.POST_ONLY
    assert captured[0].side == Side.SELL
    assert captured[0].price == Decimal("100.01")
