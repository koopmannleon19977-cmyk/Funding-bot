from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Order, OrderStatus, OrderType, Side, Trade
from funding_bot.services.market_data.models import OrderbookSnapshot
from funding_bot.services.positions.manager import PositionManager


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.trading = MagicMock()
    settings.execution = MagicMock()
    # Keep per-call get_order timeouts tiny so the test doesn't stall.
    settings.execution.close_get_order_timeout_seconds = 0.05
    return settings


@pytest.mark.asyncio
async def test_close_leg_sets_exit_price_when_position_flat_but_order_unavailable():
    settings = _make_settings()

    market_data = MagicMock()
    market_data.get_orderbook.return_value = OrderbookSnapshot(
        symbol="APEX",
        lighter_bid=Decimal("0.52050"),
        lighter_ask=Decimal("0.52059"),
        x10_bid=Decimal("0.51900"),
        x10_ask=Decimal("0.51910"),
    )

    lighter = MagicMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.place_order = AsyncMock(
        return_value=Order(
            order_id="pending_1_1",
            symbol="APEX",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=Decimal("112"),
            status=OrderStatus.PENDING,
        )
    )
    # Simulate get_order repeatedly timing out / being unavailable
    lighter.get_order = AsyncMock(side_effect=TimeoutError())
    # Position already flat -> close succeeded, but we couldn't fetch fills
    lighter.get_position = AsyncMock(return_value=None)
    lighter.get_fee_schedule = AsyncMock(return_value={"maker_fee": Decimal("0.00002"), "taker_fee": Decimal("0.0002")})

    x10 = MagicMock()
    x10.exchange = Exchange.X10

    store = MagicMock()
    event_bus = MagicMock()

    mgr = PositionManager(
        settings=settings,
        lighter=lighter,
        x10=x10,
        store=store,
        event_bus=event_bus,
        market_data=market_data,
        opportunity_engine=None,
    )

    trade = Trade.create(
        symbol="APEX",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.SELL,  # short
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("112"),
        target_notional_usd=Decimal("58.3"),
    )
    trade.leg1.filled_qty = Decimal("112")

    await mgr._close_leg(lighter, trade, trade.leg1, update_leg=True)

    assert trade.leg1.exit_price == Decimal("0.52059")
    assert trade.leg1.fees > 0
