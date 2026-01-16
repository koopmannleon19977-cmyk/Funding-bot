
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.events import AlertEvent
from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.services.positions import PositionManager, PositionsStillOpenError


@pytest.mark.asyncio
async def test_close_failure_positions_still_open_sends_throttled_critical_alert():
    settings = MagicMock()
    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.mark_opened()
    trade.opened_at = datetime.now(UTC) - timedelta(hours=1)

    mgr = PositionManager(
        settings,
        lighter,
        x10,
        store,
        event_bus,
        market_data,
        opportunity_engine=None,
    )

    # Avoid exercising real close logic here.
    mgr._close_lighter_smart = AsyncMock()  # type: ignore[method-assign]
    mgr._close_x10_smart = AsyncMock()  # type: ignore[method-assign]
    mgr._verify_closed = AsyncMock(side_effect=PositionsStillOpenError("still open"))  # type: ignore[method-assign]

    result1 = await mgr.close_trade(trade, "test_close")
    assert result1.success is False

    alerts = [
        call.args[0]
        for call in event_bus.publish.await_args_list
        if isinstance(call.args[0], AlertEvent)
    ]
    assert any(a.level == "CRITICAL" and "Close failed" in a.message for a in alerts)

    # Immediate retry should not spam (throttled)
    event_bus.publish.reset_mock()
    result2 = await mgr.close_trade(trade, "test_close_retry")
    assert result2.success is False
    alerts2 = [
        call.args[0]
        for call in event_bus.publish.await_args_list
        if isinstance(call.args[0], AlertEvent)
    ]
    assert alerts2 == []

