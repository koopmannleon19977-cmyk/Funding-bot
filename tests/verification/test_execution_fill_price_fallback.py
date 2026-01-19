from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Opportunity,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Trade,
)
from funding_bot.services.execution import ExecutionEngine


@pytest.mark.asyncio
async def test_leg1_falls_back_to_attempt_price_when_avg_fill_is_zero():
    settings = MagicMock()
    settings.execution.maker_order_max_retries = 1
    settings.execution.maker_order_timeout_seconds = 10

    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    market_data = MagicMock()
    market_data.get_price.return_value.lighter_price = Decimal("100")
    market_data.get_market_info.return_value.tick_size = Decimal("1")

    ob = MagicMock()
    ob.lighter_bid = Decimal("100")
    ob.lighter_ask = Decimal("101")
    ob.lighter_bid_qty = Decimal("10")
    ob.lighter_ask_qty = Decimal("10")
    market_data.get_orderbook.return_value = ob

    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("100"),
    )

    opp = Opportunity(
        symbol="TEST",
        timestamp=datetime.now(),
        lighter_rate=Decimal("0"),
        x10_rate=Decimal("0"),
        net_funding_hourly=Decimal("0"),
        apy=Decimal("0"),
        spread_pct=Decimal("0"),
        mid_price=Decimal("100"),
        lighter_best_bid=Decimal("100"),
        lighter_best_ask=Decimal("101"),
        x10_best_bid=Decimal("100"),
        x10_best_ask=Decimal("101"),
        suggested_qty=Decimal("1"),
        suggested_notional=Decimal("100"),
        expected_value_usd=Decimal("0"),
        breakeven_hours=Decimal("0"),
        long_exchange=Exchange.LIGHTER,
        short_exchange=Exchange.X10,
    )

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    lighter.get_position.return_value = None
    lighter.place_order.return_value = Order(
        order_id="l1",
        symbol="TEST",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        price=Decimal("100"),
        status=OrderStatus.OPEN,
    )

    filled = Order(
        order_id="l1",
        symbol="TEST",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        price=Decimal("100"),
        status=OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("0"),
        fee=Decimal("0"),
    )
    engine._wait_for_fill = AsyncMock(return_value=filled)  # type: ignore[method-assign]

    await engine._execute_leg1(trade, opp)

    assert trade.leg1.filled_qty == Decimal("1")
    assert trade.leg1.entry_price == Decimal("100")
