from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.events import PositionReconciled
from funding_bot.domain.models import Exchange, Position, Side, Trade
from funding_bot.services.reconcile import Reconciler


@pytest.mark.asyncio
async def test_reconcile_conflict_publishes_closed_conflict_action():
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3
    settings.reconciliation.soft_close_enabled = False

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    x10 = AsyncMock()
    x10.exchange = Exchange.X10

    # DB says we are BUY on Lighter / SELL on X10
    trade = Trade.create(
        symbol="TIA",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("10"),
        target_notional_usd=Decimal("100"),
    )
    trade.mark_opened()

    store = AsyncMock()
    store.list_open_trades.return_value = [trade]

    # Exchange shows opposite sides -> conflict
    lighter.list_positions.return_value = [
        Position(
            symbol="TIA",
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            qty=Decimal("1"),
            entry_price=Decimal("1"),
            mark_price=Decimal("1"),
        )
    ]
    x10.list_positions.return_value = [
        Position(
            symbol="TIA-USD",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1"),
            entry_price=Decimal("1"),
            mark_price=Decimal("1"),
        )
    ]

    lighter.place_order = AsyncMock()
    x10.place_order = AsyncMock()
    lighter.get_position = AsyncMock(return_value=lighter.list_positions.return_value[0])
    x10.get_position = AsyncMock(return_value=x10.list_positions.return_value[0])

    event_bus = AsyncMock()
    reconciler = Reconciler(settings, lighter, x10, store, event_bus)

    await reconciler.reconcile()

    assert x10.place_order.await_count == 1
    reconciled = [
        call.args[0] for call in event_bus.publish.await_args_list if isinstance(call.args[0], PositionReconciled)
    ]
    assert any(e.symbol == "TIA" and e.action == "closed_conflict" for e in reconciled)
