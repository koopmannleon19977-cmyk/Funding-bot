"""
Tests for post-entry broken-hedge emergency close.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.events import BrokenHedgeDetected
from funding_bot.domain.models import (
    Exchange,
    Opportunity,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.services.execution_impl_post_leg2 import _execute_impl_post_leg2


class DummyExec:
    def __init__(self) -> None:
        self.lighter = AsyncMock()
        self.x10 = AsyncMock()
        self.store = AsyncMock()
        self.event_bus = AsyncMock()
        self.settings = MagicMock()
        self.settings.execution.hedge_ioc_fill_timeout_seconds = Decimal("0.1")
        self._update_trade = AsyncMock()
        self._kpi_update_attempt = AsyncMock()

    async def _execute_leg2(self, trade, opp, **_kwargs):
        trade.leg2.filled_qty = trade.target_qty
        trade.leg2.entry_price = opp.mid_price
        trade.leg2.order_id = "x10-order-001"

    async def _wait_for_fill(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_broken_hedge_triggers_emergency_close():
    exec_ctx = DummyExec()

    trade = Trade(
        trade_id="trade-hedge-001",
        symbol="ETH",
        status=TradeStatus.OPENING,
        target_qty=Decimal("1.0"),
        target_notional_usd=Decimal("100"),
        entry_apy=Decimal("0.10"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            filled_qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            order_id="lighter-order-001",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.SELL,
            filled_qty=Decimal("0"),
            entry_price=Decimal("0"),
            order_id=None,
        ),
        created_at=datetime.now(UTC),
    )

    opp = Opportunity(
        symbol="ETH",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.0001"),
        x10_rate=Decimal("-0.0001"),
        net_funding_hourly=Decimal("0.0002"),
        apy=Decimal("0.10"),
        spread_pct=Decimal("0.001"),
        mid_price=Decimal("100"),
        lighter_best_bid=Decimal("99"),
        lighter_best_ask=Decimal("101"),
        x10_best_bid=Decimal("99"),
        x10_best_ask=Decimal("101"),
        suggested_qty=Decimal("1.0"),
        suggested_notional=Decimal("100"),
        expected_value_usd=Decimal("1.0"),
        breakeven_hours=Decimal("2"),
        long_exchange=Exchange.LIGHTER,
        short_exchange=Exchange.X10,
    )

    exec_ctx.lighter.get_position = AsyncMock(return_value=None)
    exec_ctx.x10.get_position = AsyncMock(
        return_value=Position(
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
        )
    )
    exec_ctx.x10.place_order = AsyncMock(
        return_value=Order(
            order_id="x10-close-001",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("1.0"),
            avg_fill_price=Decimal("100"),
        )
    )

    await _execute_impl_post_leg2(
        exec_ctx,
        opp,
        trade,
        attempt_id="attempt-001",
        leg1_filled_monotonic=0.0,
    )

    assert trade.status == TradeStatus.CLOSING
    assert exec_ctx.x10.place_order.called
    request = exec_ctx.x10.place_order.call_args[0][0]
    assert request.reduce_only is True
    assert request.side == Side.BUY

    published = [call.args[0] for call in exec_ctx.event_bus.publish.call_args_list]
    assert any(isinstance(evt, BrokenHedgeDetected) for evt in published)
