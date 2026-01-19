"""
Test: Reconciler Quantity Mismatch Detection

Verifies that the reconciler detects and alerts when position quantities
differ between Lighter and X10 exchanges, indicating an imbalanced hedge.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.events import PositionReconciled
from funding_bot.domain.models import Exchange, Position, Side, Trade
from funding_bot.services.reconcile import Reconciler


@pytest.mark.asyncio
async def test_reconcile_quantity_mismatch_publishes_event():
    """
    When Lighter qty ≠ X10 qty for same trade (beyond tolerance),
    reconciler should publish PositionReconciled with action='quantity_mismatch'.
    """
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    x10 = AsyncMock()
    x10.exchange = Exchange.X10

    # DB says we have a trade with 100 qty
    trade = Trade.create(
        symbol="UNI",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("100"),
        target_notional_usd=Decimal("500"),
    )
    trade.mark_opened()

    store = AsyncMock()
    store.list_open_trades.return_value = [trade]

    # Exchange positions have MISMATCHED quantities
    # Lighter: 100 (correct)
    # X10: 50 (half - indicating orphaned position)
    lighter.list_positions.return_value = [
        Position(
            symbol="UNI",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,  # Same side as trade leg1
            qty=Decimal("100"),
            entry_price=Decimal("5.90"),
            mark_price=Decimal("5.95"),
        )
    ]
    x10.list_positions.return_value = [
        Position(
            symbol="UNI-USD",
            exchange=Exchange.X10,
            side=Side.SELL,  # Same side as trade leg2
            qty=Decimal("50"),  # MISMATCH: only half the quantity!
            entry_price=Decimal("5.90"),
            mark_price=Decimal("5.95"),
        )
    ]

    event_bus = AsyncMock()
    reconciler = Reconciler(settings, lighter, x10, store, event_bus)

    await reconciler.reconcile()

    # Find quantity_mismatch events
    mismatch_events = [
        call.args[0]
        for call in event_bus.publish.await_args_list
        if isinstance(call.args[0], PositionReconciled) and call.args[0].action == "quantity_mismatch"
    ]

    # Assert that a quantity mismatch was detected
    assert len(mismatch_events) == 1, f"Expected 1 mismatch event, got {len(mismatch_events)}"
    event = mismatch_events[0]
    assert event.symbol == "UNI"
    assert event.details["lighter_qty"] == "100"
    assert event.details["x10_qty"] == "50"
    assert event.details["delta"] == "50"


@pytest.mark.asyncio
async def test_reconcile_quantity_match_within_tolerance_no_event():
    """
    When quantities differ by less than 2%, no mismatch event should be published.
    """
    settings = MagicMock()
    settings.execution.maker_order_timeout_seconds = Decimal("60.0")
    settings.execution.maker_order_max_retries = 3

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    x10 = AsyncMock()
    x10.exchange = Exchange.X10

    trade = Trade.create(
        symbol="BTC",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("40000"),
    )
    trade.mark_opened()

    store = AsyncMock()
    store.list_open_trades.return_value = [trade]

    # Quantities differ by only 1% (within tolerance)
    lighter.list_positions.return_value = [
        Position(
            symbol="BTC",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            qty=Decimal("1.00"),
            entry_price=Decimal("40000"),
            mark_price=Decimal("40000"),
        )
    ]
    x10.list_positions.return_value = [
        Position(
            symbol="BTC-USD",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("0.99"),  # 1% difference - within tolerance
            entry_price=Decimal("40000"),
            mark_price=Decimal("40000"),
        )
    ]

    event_bus = AsyncMock()
    reconciler = Reconciler(settings, lighter, x10, store, event_bus)

    await reconciler.reconcile()

    # Should NOT have any quantity_mismatch events
    mismatch_events = [
        call.args[0]
        for call in event_bus.publish.await_args_list
        if isinstance(call.args[0], PositionReconciled) and call.args[0].action == "quantity_mismatch"
    ]

    assert len(mismatch_events) == 0, f"Expected 0 mismatch events, got {len(mismatch_events)}"
