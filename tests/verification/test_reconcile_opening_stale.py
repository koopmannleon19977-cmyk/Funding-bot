
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, ExecutionState, Side, Trade, TradeStatus
from funding_bot.services.reconcile import Reconciler


@pytest.mark.asyncio
async def test_reconcile_does_not_close_inflight_opening_trade_under_threshold():
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.list_positions.return_value = []

    x10 = AsyncMock()
    x10.exchange = Exchange.X10
    x10.list_positions.return_value = []

    store = AsyncMock()
    event_bus = AsyncMock()

    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.status = TradeStatus.OPENING
    trade.execution_state = ExecutionState.LEG1_SUBMITTED
    trade.created_at = datetime.now(UTC) - timedelta(seconds=300)  # 5 minutes

    store.list_open_trades.return_value = [trade]

    reconciler = Reconciler(settings, lighter, x10, store, event_bus)
    result = await reconciler.reconcile()

    assert result.zombies_closed == 0
    store.update_trade.assert_not_awaited()
    event_bus.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_closes_stale_opening_trade_over_threshold():
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.list_positions.return_value = []

    x10 = AsyncMock()
    x10.exchange = Exchange.X10
    x10.list_positions.return_value = []

    store = AsyncMock()
    event_bus = AsyncMock()

    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.status = TradeStatus.OPENING
    trade.execution_state = ExecutionState.LEG1_SUBMITTED
    trade.created_at = datetime.now(UTC) - timedelta(seconds=1000)  # > 10 minutes

    store.list_open_trades.return_value = [trade]

    reconciler = Reconciler(settings, lighter, x10, store, event_bus)
    result = await reconciler.reconcile()

    assert result.zombies_closed == 1
    store.update_trade.assert_awaited()
    event_bus.publish.assert_awaited()


@pytest.mark.asyncio
async def test_reconcile_startup_aborts_orphaned_opening_trade_under_threshold():
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.list_positions.return_value = []

    x10 = AsyncMock()
    x10.exchange = Exchange.X10
    x10.list_positions.return_value = []

    store = AsyncMock()
    event_bus = AsyncMock()

    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.status = TradeStatus.OPENING
    trade.execution_state = ExecutionState.LEG1_SUBMITTED
    trade.created_at = datetime.now(UTC) - timedelta(seconds=60)  # under normal threshold

    store.list_open_trades.return_value = [trade]

    reconciler = Reconciler(settings, lighter, x10, store, event_bus)
    result = await reconciler.reconcile(startup=True)

    assert result.zombies_closed == 1
    lighter.cancel_all_orders.assert_awaited_with("TIA")
    x10.cancel_all_orders.assert_awaited_with("TIA-USD")
    store.update_trade.assert_awaited()
    event_bus.publish.assert_awaited()
