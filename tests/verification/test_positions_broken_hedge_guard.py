
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.events import AlertEvent
from funding_bot.domain.models import Exchange, Side, Trade, TradeStatus
from funding_bot.services.positions import PositionManager


@pytest.mark.asyncio
async def test_broken_hedge_requires_two_confirmations_then_closes():
    settings = MagicMock()

    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    # One open trade
    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.mark_opened()
    trade.opened_at = datetime.now(UTC) - timedelta(seconds=120)
    trade.status = TradeStatus.OPEN

    store.list_open_trades.return_value = [trade]

    mgr = PositionManager(settings, lighter, x10, store, event_bus, market_data, opportunity_engine=None)

    # Simulate: Lighter has a position, X10 leg missing (liquidated)
    lighter.get_position.return_value = MagicMock(qty=Decimal("1"), side=Side.BUY, liquidation_price=None)
    x10.get_position.return_value = None

    # Patch close_trade so we don't execute full close logic in this unit test.
    mgr.close_trade = AsyncMock(return_value=MagicMock(success=True))  # type: ignore[method-assign]
    mgr._evaluate_exit = AsyncMock(return_value=MagicMock(should_exit=False, reason=""))  # type: ignore[method-assign]

    # First pass: only confirms
    closed = await mgr.check_trades()
    assert closed == []
    mgr.close_trade.assert_not_awaited()

    # Second pass: confirmed -> triggers close
    closed = await mgr.check_trades()
    assert len(closed) == 1
    mgr.close_trade.assert_awaited()
    assert any(
        isinstance(call.args[0], AlertEvent) and call.args[0].level == "CRITICAL"
        for call in event_bus.publish.await_args_list
    )
