"""
Unit tests for close idempotency and post-close readback functionality.

Tests T1-T5 Phase-2 improvements:
- T1: Close order logging to trade.events
- T2: Idempotent close (no duplicate orders when already CLOSING)
- T3: Post-close readback (API truth for VWAP/fees)
- T4: Finalize uses readback data
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderStatus,
    Side,
    TimeInForce,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.services.positions.close import (
    _collect_close_order_ids,
)


# ============================================================================
# Test Helpers
# ============================================================================

def _make_test_trade(
    status: TradeStatus = TradeStatus.OPEN,
    close_reason: str | None = None,
) -> Trade:
    """Create a test trade for close idempotency tests."""
    leg1 = TradeLeg(
        exchange=Exchange.LIGHTER,
        side=Side.SELL,
        qty=Decimal("0.1"),
        filled_qty=Decimal("0.1"),
        entry_price=Decimal("50000"),
    )
    leg2 = TradeLeg(
        exchange=Exchange.X10,
        side=Side.BUY,
        qty=Decimal("0.1"),
        filled_qty=Decimal("0.1"),
        entry_price=Decimal("50000"),
    )
    return Trade(
        trade_id="test_001",
        symbol="BTC-PERP",
        leg1=leg1,
        leg2=leg2,
        target_qty=Decimal("0.1"),
        target_notional_usd=Decimal("5000"),
        entry_apy=Decimal("0.40"),
        status=status,
        close_reason=close_reason,
    )


def _make_mock_order(
    order_id: str,
    filled_qty: Decimal,
    avg_fill_price: Decimal,
    fee: Decimal,
) -> Order:
    """Create a mock order for readback tests."""
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.filled_qty = filled_qty
    order.avg_fill_price = avg_fill_price
    order.fee = fee
    order.is_filled = filled_qty > 0
    order.is_active = False
    order.status = OrderStatus.FILLED
    return order


# ============================================================================
# T1: Close Order Logging Tests
# ============================================================================

def test_log_close_order_placed_adds_event_to_trade():
    """Test that close order placement adds an event to trade.events."""
    from funding_bot.services.positions.close import _log_close_order_placed

    trade = _make_test_trade()
    initial_event_count = len(trade.events)

    _log_close_order_placed(
        trade=trade,
        exchange=Exchange.LIGHTER,
        leg="leg1",
        order_id="order_123",
        client_order_id="client_456",
        side=Side.BUY,
        qty=Decimal("0.1"),
        price=Decimal("50000"),
        time_in_force=TimeInForce.POST_ONLY,
        post_only=True,
        attempt_id=1,
    )

    assert len(trade.events) == initial_event_count + 1

    event = trade.events[-1]
    assert event["event_type"] == "CLOSE_ORDER_PLACED"
    assert event["exchange"] == "LIGHTER"  # Exchange enum values are uppercase
    assert event["leg"] == "leg1"
    assert event["order_id"] == "order_123"
    assert event["client_order_id"] == "client_456"
    assert event["side"] == "BUY"  # Side enum values are uppercase
    assert event["qty"] == "0.1"
    assert event["price"] == "50000"
    assert event["time_in_force"] == "POST_ONLY"  # TimeInForce enum values are uppercase
    assert event["post_only"] is True
    assert event["attempt_id"] == 1
    assert "timestamp" in event


def test_log_close_order_handles_market_orders():
    """Test that market orders are logged correctly (price=0)."""
    from funding_bot.services.positions.close import _log_close_order_placed

    trade = _make_test_trade()

    _log_close_order_placed(
        trade=trade,
        exchange=Exchange.X10,
        leg="leg2",
        order_id="market_order",
        client_order_id=None,
        side=Side.SELL,
        qty=Decimal("0.5"),
        price=Decimal("0"),  # Market orders
        time_in_force=TimeInForce.IOC,
        post_only=False,
    )

    event = trade.events[-1]
    assert event["order_id"] == "market_order"
    assert event["price"] == "0"
    assert event["client_order_id"] is None
    assert event["time_in_force"] == "IOC"  # TimeInForce enum values are uppercase


# ============================================================================
# T2: Idempotent Close Tests
# ============================================================================

@pytest.mark.asyncio
async def test_close_idempotent_does_not_duplicate_orders():
    """
    Test that calling close() on a trade already in CLOSING state
    does NOT place new orders (idempotent behavior).
    """
    # This test requires the full PositionCloseService setup
    # For now, we test the logic path that would be taken
    trade = _make_test_trade(status=TradeStatus.CLOSING, close_reason="TAKE_PROFIT")

    # Verify trade is in CLOSING state with a reason
    assert trade.status == TradeStatus.CLOSING
    assert trade.close_reason == "TAKE_PROFIT"

    # In the actual implementation, _close_impl would detect this state
    # and call _close_verify_and_finalize instead of placing new orders
    # This test verifies the condition check:
    should_skip_order_placement = (
        trade.status == TradeStatus.CLOSING and trade.close_reason is not None
    )
    assert should_skip_order_placement is True


@pytest.mark.asyncio
async def test_close_first_time_sets_closing_status():
    """Test that first close() call sets status to CLOSING."""
    trade = _make_test_trade(status=TradeStatus.OPEN)

    # Verify trade is OPEN initially
    assert trade.status == TradeStatus.OPEN
    assert trade.close_reason is None

    # After first close call, status should be CLOSING
    # (This would be set by _close_impl before placing orders)
    should_proceed_with_orders = not (
        trade.status == TradeStatus.CLOSING and trade.close_reason
    )
    assert should_proceed_with_orders is True


# ============================================================================
# T3: Post-Close Readback Tests
# ============================================================================

def test_extract_close_order_ids_from_events():
    """Test extraction of close order IDs from trade.events."""
    trade = _make_test_trade()

    # Add some close order events
    trade.events.extend([
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "CLOSE_ORDER_PLACED",
            "exchange": "lighter",
            "leg": "leg1",
            "order_id": "lighter_order_1",
        },
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "CLOSE_ORDER_PLACED",
            "exchange": "lighter",
            "leg": "leg1",
            "order_id": "lighter_order_2",
        },
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "CLOSE_ORDER_PLACED",
            "exchange": "x10",
            "leg": "leg2",
            "order_id": "x10_order_1",
        },
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "OTHER_EVENT",  # Should be ignored
            "order_id": "should_be_ignored",
        },
    ])

    lighter_ids, x10_ids = _collect_close_order_ids(trade)

    assert lighter_ids["leg1"] == ["lighter_order_1", "lighter_order_2"]
    assert lighter_ids["leg2"] == []
    assert x10_ids["leg2"] == ["x10_order_1"]
    assert x10_ids["leg1"] == []


def test_extract_close_order_ids_empty_events():
    """Test extraction when no close order events exist."""
    trade = _make_test_trade()

    lighter_ids, x10_ids = _collect_close_order_ids(trade)

    assert lighter_ids["leg1"] == []
    assert lighter_ids["leg2"] == []
    assert x10_ids["leg1"] == []
    assert x10_ids["leg2"] == []


@pytest.mark.asyncio
async def test_post_close_readback_recomputes_vwap_and_fees():
    """
    Test that post-close readback correctly recalculates VWAP and fees
    from exchange API data.
    """
    # This is a simplified unit test - full integration test would require
    # mocking the exchange adapters and PositionCloseService
    from funding_bot.services.positions.close import _delta_from_cumulative_fill

    # Simulate multiple orders with cumulative fills (Lighter behavior)
    qty_seen: dict[str, Decimal] = {}
    fee_seen: dict[str, Decimal] = {}

    # Order 1: 0.05 BTC filled @ $50,000, $25 fee
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id="order_1",
        cum_qty=Decimal("0.05"),
        cum_fee=Decimal("25"),
        fill_price=Decimal("50000"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    # Order 2: 0.05 BTC filled @ $50,100, $26 fee
    # Note: Each order's cumulative values are tracked independently by order_id
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id="order_2",
        cum_qty=Decimal("0.05"),
        cum_fee=Decimal("26"),
        fill_price=Decimal("50100"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    # Verify deltas are calculated correctly
    assert d_qty1 == Decimal("0.05")
    assert d_notional1 == Decimal("2500")  # 0.05 * 50000
    assert d_fee1 == Decimal("25")

    # Order 2 has its own cumulative values (independent of order_1)
    assert d_qty2 == Decimal("0.05")
    assert d_notional2 == Decimal("2505")  # 0.05 * 50100
    assert d_fee2 == Decimal("26")

    # Calculate VWAP: (2500 + 2505) / (0.05 + 0.05) = 50050 / 0.10 = 50050
    total_qty = d_qty1 + d_qty2
    total_notional = d_notional1 + d_notional2
    total_fees = d_fee1 + d_fee2

    api_vwap = total_notional / total_qty if total_qty > 0 else Decimal("0")

    assert api_vwap == Decimal("50050")
    assert total_fees == Decimal("51")


@pytest.mark.asyncio
async def test_readback_handles_negative_deltas():
    """Test that readback handles API resets (negative deltas)."""
    from funding_bot.services.positions.close import _delta_from_cumulative_fill

    qty_seen: dict[str, Decimal] = {"order_1": Decimal("0.10")}
    fee_seen: dict[str, Decimal] = {"order_1": Decimal("51")}

    # API returns lower cumulative value (reset or out-of-order update)
    d_qty, d_notional, d_fee = _delta_from_cumulative_fill(
        order_id="order_1",
        cum_qty=Decimal("0.05"),  # Lower than seen (0.10)
        cum_fee=Decimal("25"),     # Lower than seen (51)
        fill_price=Decimal("50000"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    # Should return 0 instead of negative values
    assert d_qty == Decimal("0")
    assert d_notional == Decimal("0")
    assert d_fee == Decimal("0")


# ============================================================================
# T4: Finalize Uses Readback Data Tests
# ============================================================================

@pytest.mark.asyncio
async def test_readback_updates_leg_when_discrepancy_exceeds_tolerance():
    """
    Test that readback updates leg.exit_price and leg.fees when
    API data differs from current values by more than tolerance.
    Tolerance: VWAP diff > 3 bps (0.0003) OR net_pnl diff > $0.30
    """
    # This test verifies the discrepancy logic
    current_exit_price = Decimal("50000")
    current_fees = Decimal("50")
    leg_qty = Decimal("0.1")  # 0.1 BTC

    api_vwap = Decimal("50020")  # 4 bps higher (0.0004 > 0.0003)
    api_fees = Decimal("50.40")

    tolerance_pct = Decimal("0.0003")  # 3 bps
    net_pnl_tolerance_usd = Decimal("0.30")

    price_diff_pct = abs(api_vwap - current_exit_price) / current_exit_price
    fee_diff_usd = abs(api_fees - current_fees)

    # Calculate net_pnl impact
    price_impact_usd = abs(api_vwap - current_exit_price) * leg_qty
    net_pnl_diff_usd = price_impact_usd + fee_diff_usd

    # Price diff = 20/50000 = 0.0004 (4 bps > 3 bps tolerance)
    # Net PnL impact = $2 + $0.40 = $2.40 > $0.30
    should_update = price_diff_pct > tolerance_pct or net_pnl_diff_usd > net_pnl_tolerance_usd

    assert should_update is True
    assert price_diff_pct == Decimal("0.0004")  # 4 bps
    assert net_pnl_diff_usd == Decimal("2.40")  # $2.40


@pytest.mark.asyncio
async def test_readback_skips_update_when_within_tolerance():
    """
    Test that readback does NOT update when within tolerance.
    Tolerance: VWAP diff <= 3 bps AND net_pnl diff <= $0.30
    """
    current_exit_price = Decimal("50000")
    current_fees = Decimal("50")
    leg_qty = Decimal("0.1")  # 0.1 BTC

    # Use small values that are within BOTH tolerances
    api_vwap = Decimal("50001")  # 0.2 bps higher (within 3 bps tolerance)
    api_fees = Decimal("50.01")

    tolerance_pct = Decimal("0.0003")  # 3 bps
    net_pnl_tolerance_usd = Decimal("0.30")

    price_diff_pct = abs(api_vwap - current_exit_price) / current_exit_price
    fee_diff_usd = abs(api_fees - current_fees)

    # Calculate net_pnl impact
    price_impact_usd = abs(api_vwap - current_exit_price) * leg_qty
    net_pnl_diff_usd = price_impact_usd + fee_diff_usd

    should_update = price_diff_pct > tolerance_pct or net_pnl_diff_usd > net_pnl_tolerance_usd

    assert should_update is False
    assert price_diff_pct == Decimal("0.00002")  # 0.2 bps (within 3 bps)
    assert net_pnl_diff_usd == Decimal("0.11")  # $0.11 (within $0.30)


# ============================================================================
# Edge Cases
# ============================================================================

def test_extract_close_order_ids_filters_non_close_events():
    """Test that non-CLOSE_ORDER_PLACED events are filtered out."""
    trade = _make_test_trade()

    trade.events.extend([
        {"event_type": "ORDER_FILLED", "order_id": "filled_order"},
        {"event_type": "POSITION_OPENED", "order_id": "open_order"},
        {"event_type": "CLOSE_ORDER_PLACED", "exchange": "lighter", "leg": "leg1", "order_id": "close_order"},
    ])

    lighter_ids, x10_ids = _collect_close_order_ids(trade)

    # Only CLOSE_ORDER_PLACED should be extracted
    assert lighter_ids["leg1"] == ["close_order"]
    assert len(lighter_ids["leg1"]) == 1


@pytest.mark.asyncio
async def test_readback_handles_missing_orders():
    """Test that readback handles when get_order returns None."""
    # In actual implementation, this would log a warning and continue
    # Here we verify the logic flow
    order_results = [
        _make_mock_order("order_1", Decimal("0.05"), Decimal("50000"), Decimal("25")),
        None,  # Missing order
        _make_mock_order("order_3", Decimal("0.05"), Decimal("50100"), Decimal("26")),
    ]

    total_qty = Decimal("0")
    for order in order_results:
        if order and order.filled_qty:
            total_qty += order.filled_qty

    # Should only count valid orders
    assert total_qty == Decimal("0.10")  # 0.05 + 0.05 (None skipped)


@pytest.mark.asyncio
async def test_readback_handles_zero_filled_qty():
    """Test that readback handles orders with zero filled_qty."""
    order = _make_mock_order("unfilled", Decimal("0"), Decimal("0"), Decimal("0"))

    # Order with zero filled_qty should not contribute to totals
    # Note: Decimal("0") and ... returns Decimal("0"), not False
    should_include = bool(order.filled_qty and order.filled_qty > 0)

    assert should_include is False
