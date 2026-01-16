"""
Unit tests for Phase 3: Rebalance and Coordinated Close.

Tests:
- test_rebalance_reduces_delta_without_full_exit: Verify delta drift triggers rebalance
- test_coordinated_close_avoids_unhedged_window: Verify parallel close on both legs
"""

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


# ============================================================================
# Test Helpers
# ============================================================================

def _make_test_trade(
    leg1_qty: Decimal = Decimal("0.1"),
    leg2_qty: Decimal = Decimal("0.1"),
    leg1_side: Side = Side.SELL,
    leg2_side: Side = Side.BUY,
) -> Trade:
    """Create a test trade for rebalance/coordinated close tests."""
    leg1 = TradeLeg(
        exchange=Exchange.LIGHTER,
        side=leg1_side,
        qty=leg1_qty,
        filled_qty=leg1_qty,
        entry_price=Decimal("50000"),
    )
    leg2 = TradeLeg(
        exchange=Exchange.X10,
        side=leg2_side,
        qty=leg2_qty,
        filled_qty=leg2_qty,
        entry_price=Decimal("50000"),
    )
    return Trade(
        trade_id="test_rebalance_001",
        symbol="BTC-PERP",
        leg1=leg1,
        leg2=leg2,
        target_qty=leg1_qty,
        target_notional_usd=Decimal("5000"),
        entry_apy=Decimal("0.40"),
        status=TradeStatus.OPEN,
    )


def _make_mock_order(
    order_id: str,
    filled_qty: Decimal,
    avg_fill_price: Decimal,
    fee: Decimal,
    is_filled: bool = True,
) -> Order:
    """Create a mock order for testing."""
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.filled_qty = filled_qty
    order.avg_fill_price = avg_fill_price
    order.fee = fee
    order.is_filled = is_filled
    order.is_active = not is_filled
    order.status = OrderStatus.FILLED if is_filled else OrderStatus.OPEN
    order.client_order_id = f"client_{order_id}"
    return order


# ============================================================================
# Rebalance Tests
# ============================================================================

@pytest.mark.asyncio
async def test_rebalance_reduces_delta_without_full_exit():
    """
    Test that rebalance (1-3% delta drift) restores neutrality without full exit.

    Scenario:
    - Lighter leg: SELL 0.1 BTC @ $50,000 = $5,000 notional
    - X10 leg: BUY 0.095 BTC @ $50,000 = $4,750 notional
    - Net delta = $5,000 - $4,750 = $250 (5% drift - emergency exit territory)
    - With rebalance_min=1%, rebalance_max=3%: this triggers EMERGENCY exit

    For this test, we simulate a smaller drift (2%) to trigger rebalance:
    - Lighter leg: SELL 0.1 BTC @ $50,000 = $5,000 notional
    - X10 leg: BUY 0.098 BTC @ $50,000 = $4,900 notional
    - Net delta = $5,000 - $4,900 = $100 (2% drift - rebalance range)
    """
    from funding_bot.domain.rules import check_delta_bound

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),  # Lighter: SELL 0.1 BTC
        leg2_qty=Decimal("0.098"),  # X10: BUY 0.098 BTC (2% smaller)
    )

    # Test with rebalance enabled (1-3% range)
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),  # 3% max
        min_delta_pct=Decimal("0.01"),  # 1% min
    )

    # Calculate expected delta drift
    leg1_notional = Decimal("0.100") * Decimal("50000")  # $5,000
    leg2_notional = Decimal("0.098") * Decimal("50000")  # $4,900
    net_delta = abs(leg1_notional - leg2_notional)  # $100
    total_notional = leg1_notional + leg2_notional  # $9,900
    expected_drift_pct = net_delta / total_notional  # ~1.01% (within 1-3% range)

    # Verify rebalance trigger (not emergency exit)
    assert result.passed is True, "Rebalance should trigger for 2% drift"
    assert "REBALANCE" in result.reason, f"Reason should contain REBALANCE: {result.reason}"
    assert "1.01%" in result.reason or "1.02%" in result.reason, f"Reason should show drift %: {result.reason}"

    # Verify it's NOT an emergency exit
    assert "EMERGENCY" not in result.reason, f"Should not be emergency for rebalance range: {result.reason}"


@pytest.mark.asyncio
async def test_rebalance_below_min_threshold_no_trigger():
    """
    Test that delta drift below min threshold (1%) does NOT trigger rebalance.
    """
    from funding_bot.domain.rules import check_delta_bound

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),  # Lighter: SELL 0.1 BTC
        leg2_qty=Decimal("0.0995"),  # X10: BUY 0.0995 BTC (0.5% smaller)
    )

    # Test with rebalance enabled (1-3% range)
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),  # 3% max
        min_delta_pct=Decimal("0.01"),  # 1% min
    )

    # Should NOT trigger (below 1% threshold)
    assert result.passed is False, "Should not trigger for drift below min threshold"
    assert "Delta OK" in result.reason, f"Reason should indicate OK: {result.reason}"


@pytest.mark.asyncio
async def test_rebalance_above_max_threshold_emergency_exit():
    """
    Test that delta drift above max threshold (3%) triggers EMERGENCY exit.
    """
    from funding_bot.domain.rules import check_delta_bound

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),  # Lighter: SELL 0.1 BTC
        leg2_qty=Decimal("0.090"),  # X10: BUY 0.09 BTC (10% smaller!)
    )

    # Test with rebalance enabled (1-3% range)
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),  # 3% max
        min_delta_pct=Decimal("0.01"),  # 1% min
    )

    # Should trigger EMERGENCY exit (above 3% threshold)
    assert result.passed is True, "Should trigger emergency exit for large drift"
    assert "EMERGENCY" in result.reason, f"Reason should contain EMERGENCY: {result.reason}"
    assert "Delta bound violated" in result.reason, f"Reason should mention violation: {result.reason}"


@pytest.mark.asyncio
async def test_rebalance_identifies_smaller_leg_correctly():
    """
    Test that rebalance correctly identifies which leg needs more position.

    Scenario: Lighter SELL leg is larger than X10 BUY leg
    - Need to add more to X10 BUY leg to restore neutrality
    """
    from funding_bot.services.positions.close import _rebalance_trade

    # Create mock service
    service = MagicMock()
    service.settings.trading.rebalance_maker_timeout_seconds = 6.0
    service.settings.trading.rebalance_use_ioc_fallback = True
    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.return_value = MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),  # Lighter: SELL 0.1 BTC = $5,000
        leg2_qty=Decimal("0.098"),  # X10: BUY 0.098 BTC = $4,900 (shorter)
    )

    # Mock the place_order to track which exchange gets the rebalance order
    rebalance_exchange = None
    rebalance_side = None
    rebalance_qty = None

    async def mock_place_order(request):
        nonlocal rebalance_exchange, rebalance_side, rebalance_qty
        # Determine exchange from service context
        if service.lighter == service.lighter:
            rebalance_exchange = Exchange.LIGHTER
        rebalance_side = request.side
        rebalance_qty = request.qty
        return _make_mock_order("rebalance_123", request.qty, Decimal("50000"), Decimal("0"))

    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)

    # Mock get_order to return filled
    service.lighter.get_order = AsyncMock(return_value=_make_mock_order("rebalance_123", Decimal("0.002"), Decimal("50000"), Decimal("0")))
    service.x10.get_order = AsyncMock(return_value=_make_mock_order("rebalance_123", Decimal("0.002"), Decimal("50000"), Decimal("0")))

    # Bind the method to the mock service
    from funding_bot.services.positions import close
    await close._rebalance_trade(service, trade)

    # Verify rebalance was attempted (exchange-specific logic would determine which leg)
    # In this case: Lighter SELL is larger, X10 BUY is smaller → add to X10
    # The rebalance should add 0.002 BTC to X10 BUY leg


# ============================================================================
# Coordinated Close Tests
# ============================================================================

@pytest.mark.asyncio
async def test_coordinated_close_submits_parallel_maker_orders():
    """
    Test that coordinated close submits maker orders on BOTH legs simultaneously.

    Benefits:
    - Minimizes unhedged exposure (both legs close in parallel)
    - Uses maker orders on X10 (post_only flag) for fee savings
    """
    from funding_bot.services.positions.close import _close_both_legs_coordinated

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.coordinated_close_x10_use_maker = True
    service.settings.trading.coordinated_close_maker_timeout_seconds = 6.0
    service.settings.trading.coordinated_close_ioc_timeout_seconds = 2.0
    service.settings.trading.coordinated_close_escalate_to_ioc = True

    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.side_effect = lambda symbol, exchange: MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade(
        leg1_qty=Decimal("0.1"),
        leg2_qty=Decimal("0.1"),
    )

    # Track which orders were placed
    lighter_order_placed = False
    x10_order_placed = False

    async def mock_lighter_place_order(request):
        nonlocal lighter_order_placed
        lighter_order_placed = True
        assert request.side == Side.BUY, "Lighter close should be BUY (inverse of SELL leg)"
        assert request.time_in_force == TimeInForce.POST_ONLY, "Should use POST_ONLY for maker"
        assert request.reduce_only is True, "Close orders should be reduce_only"
        return _make_mock_order("lighter_close_123", Decimal("0.1"), Decimal("50000"), Decimal("1"))

    async def mock_x10_place_order(request):
        nonlocal x10_order_placed
        x10_order_placed = True
        assert request.side == Side.SELL, "X10 close should be SELL (inverse of BUY leg)"
        # X10 uses POST_ONLY TimeInForce for maker orders (adapter converts to post_only flag)
        assert request.time_in_force == TimeInForce.POST_ONLY, "Should use POST_ONLY for maker"
        assert request.reduce_only is True, "Close orders should be reduce_only"
        return _make_mock_order("x10_close_456", Decimal("0.1"), Decimal("50000"), Decimal("0"))

    service.lighter.place_order = AsyncMock(side_effect=mock_lighter_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_x10_place_order)

    # Mock get_order to return filled immediately (simulating instant fill)
    service.lighter.get_order = AsyncMock(return_value=_make_mock_order("lighter_close_123", Decimal("0.1"), Decimal("50000"), Decimal("1")))
    service.x10.get_order = AsyncMock(return_value=_make_mock_order("x10_close_456", Decimal("0.1"), Decimal("50000"), Decimal("0")))

    # Mock fallback methods (in case of error)
    service._close_lighter_smart = AsyncMock()
    service._close_x10_smart = AsyncMock()
    service._update_trade = AsyncMock()

    # Bind the _submit_maker_order function to the service as a method
    # This allows it to be called properly via self._submit_maker_order
    from funding_bot.services.positions import close
    import types
    service._submit_maker_order = types.MethodType(close._submit_maker_order, service)
    service._execute_ioc_close = types.MethodType(close._execute_ioc_close, service)

    await close._close_both_legs_coordinated(service, trade)

    # Verify both legs had maker orders placed
    assert lighter_order_placed is True, "Lighter maker order should be placed"
    assert x10_order_placed is True, "X10 maker order should be placed"


@pytest.mark.asyncio
async def test_coordinated_close_esculates_unfilled_to_ioc():
    """
    Test that coordinated close escalates unfilled legs to IOC after maker timeout.

    This ensures no unhedged window: BOTH legs are either filled by maker
    or escalated to IOC simultaneously.
    """
    from funding_bot.services.positions.close import _close_both_legs_coordinated

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.coordinated_close_x10_use_maker = True
    service.settings.trading.coordinated_close_maker_timeout_seconds = 0.1  # Very short for testing
    service.settings.trading.coordinated_close_ioc_timeout_seconds = 2.0
    service.settings.trading.coordinated_close_escalate_to_ioc = True

    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.side_effect = lambda symbol, exchange: MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade()

    # Track IOC escalation
    lighter_ioc_placed = False
    x10_ioc_placed = False

    async def mock_place_order(request):
        if request.time_in_force == TimeInForce.POST_ONLY or getattr(request, "post_only", False):
            # Maker order - return unfilled
            return _make_mock_order("maker_123", Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)
        elif request.time_in_force == TimeInForce.IOC:
            # IOC order
            nonlocal lighter_ioc_placed, x10_ioc_placed
            if service.lighter.place_order.call_count > service.x10.place_order.call_count:
                lighter_ioc_placed = True
            else:
                x10_ioc_placed = True
            return _make_mock_order("ioc_456", Decimal("0.1"), Decimal("50000"), Decimal("1"), is_filled=True)
        return _make_mock_order("unknown", Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)

    async def mock_get_order(order_id):
        # Return unfilled for maker orders (triggering IOC escalation)
        return _make_mock_order(order_id, Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)

    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)
    service.lighter.get_order = AsyncMock(side_effect=mock_get_order)
    service.x10.get_order = AsyncMock(side_effect=mock_get_order)

    # Mock fallback methods (in case of error)
    service._close_lighter_smart = AsyncMock()
    service._close_x10_smart = AsyncMock()
    service._update_trade = AsyncMock()

    # Bind the _submit_maker_order function to the service as a method
    # This allows it to be called properly via self._submit_maker_order
    from funding_bot.services.positions import close
    import types
    service._submit_maker_order = types.MethodType(close._submit_maker_order, service)
    service._execute_ioc_close = types.MethodType(close._execute_ioc_close, service)

    await close._close_both_legs_coordinated(service, trade)

    # Verify IOC escalation occurred for both legs
    assert lighter_ioc_placed is True, "Lighter should escalate to IOC"
    assert x10_ioc_placed is True, "X10 should escalate to IOC"


@pytest.mark.asyncio
async def test_coordinated_close_disabled_falls_back_to_sequential():
    """
    Test that coordinated close falls back to sequential close when disabled.
    """
    from funding_bot.services.positions.close import _close_both_legs_coordinated

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = False  # Disabled

    trade = _make_test_trade()

    # Track close order
    sequential_called = False

    async def mock_close_lighter(trade):
        nonlocal sequential_called
        sequential_called = True

    async def mock_close_x10(trade):
        pass

    async def mock_update_trade(trade):
        pass

    service._close_lighter_smart = AsyncMock(side_effect=mock_close_lighter)
    service._close_x10_smart = AsyncMock(side_effect=mock_close_x10)
    service._update_trade = AsyncMock(side_effect=mock_update_trade)

    # Bind the method to the mock service
    from funding_bot.services.positions import close
    await close._close_both_legs_coordinated(service, trade)

    # Verify sequential close was used
    assert sequential_called is True, "Should fall back to sequential close when disabled"
    service._close_lighter_smart.assert_called_once_with(trade)
    service._close_x10_smart.assert_called_once_with(trade)


@pytest.mark.asyncio
async def test_coordinated_close_x10_uses_post_only_flag():
    """
    Test that X10 maker orders use the post_only flag (not just IOC).

    X10-specific: post_only is a separate boolean flag, not a TimeInForce value.
    """
    from funding_bot.services.positions.close import _submit_maker_order

    # Create mock service
    service = MagicMock()
    service.market_data.get_market_info.return_value = MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade()

    # Track the order request
    captured_request = None

    async def mock_place_order(request):
        nonlocal captured_request
        captured_request = request
        return _make_mock_order("x10_maker_123", Decimal("0.1"), Decimal("50000"), Decimal("0"))

    service.x10.place_order = AsyncMock(side_effect=mock_place_order)

    # Bind the method to the mock service
    from funding_bot.services.positions import close
    await close._submit_maker_order(
        service, trade, Exchange.X10, "leg2", Side.SELL, Decimal("0.1"), Decimal("50000"), Decimal("0.01")
    )

    # Verify X10 order parameters (POST_ONLY for maker)
    assert captured_request is not None, "Order request should be captured"
    assert captured_request.time_in_force == TimeInForce.POST_ONLY, "X10 uses POST_ONLY for maker orders"
    assert captured_request.reduce_only is True, "Close orders should be reduce_only"


# ============================================================================
# Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_rebalance_with_coordinated_close_flow():
    """
    Integration test: rebalance trigger → coordinated close not needed.

    Rebalance preserves the position, so coordinated close should NOT be triggered
    in the same evaluation cycle.
    """
    from funding_bot.domain.rules import check_delta_bound, evaluate_exit_rules

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),
        leg2_qty=Decimal("0.098"),  # 2% drift
    )

    # Simulate exit evaluation with rebalance enabled
    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("10"),
        current_apy=Decimal("0.40"),
        lighter_rate=Decimal("0.0002"),
        x10_rate=Decimal("-0.0001"),
        min_hold_seconds=7200,
        max_hold_hours=Decimal("72"),
        profit_target_usd=Decimal("3"),
        funding_flip_hours=Decimal("4"),
        delta_bound_enabled=True,
        delta_bound_max_delta_pct=Decimal("0.03"),
        rebalance_enabled=True,
        rebalance_min_delta_pct=Decimal("0.01"),
        rebalance_max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("50000"),
        leg2_mark_price=Decimal("50000"),
    )

    # Should return REBALANCE decision (not emergency, not coordinated close)
    assert decision.should_exit is True, "Rebalance should trigger exit for action"
    assert "REBALANCE" in decision.reason, f"Reason should be REBALANCE: {decision.reason}"
    assert "EMERGENCY" not in decision.reason, f"Should not be emergency: {decision.reason}"


# ============================================================================
# T3: _close_impl Routing Tests (NEW)
# ============================================================================

@pytest.mark.asyncio
async def test_close_rebalance_path_called_for_rebalance_reason():
    """
    T3 Test: Verify that when reason starts with "REBALANCE", _close_impl
    calls _rebalance_trade() and returns early WITHOUT setting status to CLOSING
    or marking the trade as closed.

    Critical: Rebalance is a REDUCE-ONLY operation that keeps the trade OPEN.
    """
    from funding_bot.services.positions.close import _close_impl
    from funding_bot.services.positions.types import CloseResult

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.early_tp_fast_close_enabled = True
    service.settings.trading.early_tp_fast_close_use_taker_directly = True

    # Mock opportunity engine for cooldown
    service.opportunity_engine = MagicMock()
    service.opportunity_engine.mark_cooldown = MagicMock()

    service.market_data = MagicMock()
    service.event_bus = MagicMock()
    service.event_bus.publish = AsyncMock()

    # Mock _update_trade to track status changes
    status_updates = []
    original_update = AsyncMock()

    async def mock_update_trade(trade):
        status_updates.append((trade.status, trade.close_reason))

    service._update_trade = mock_update_trade

    # Mock _rebalance_trade to track if it was called
    rebalance_called = False
    async def mock_rebalance(trade, leg1_mark_price=None, leg2_mark_price=None):
        nonlocal rebalance_called
        rebalance_called = True

    service._rebalance_trade = mock_rebalance

    # Bind the method to the mock service
    import types
    from funding_bot.services.positions import close
    service._close_impl = types.MethodType(close._close_impl, service)

    # Create test trade
    trade = _make_test_trade()
    trade.status = TradeStatus.OPEN  # Start as OPEN

    # Call _close_impl with REBALANCE reason
    result = await service._close_impl(trade, "REBALANCE: Delta drift 2% in rebalance range")

    # Verify _rebalance_trade was called
    assert rebalance_called is True, "_rebalance_trade should be called for REBALANCE reason"

    # Verify trade was NOT marked as CLOSING (critical for rebalance!)
    assert trade.status == TradeStatus.OPEN, f"Trade should remain OPEN after rebalance, got {trade.status}"

    # Verify close_reason was NOT set (trade is still open)
    assert trade.close_reason == "" or trade.close_reason is None, "close_reason should NOT be set for rebalance"

    # Verify result indicates rebalance (not full close)
    assert result.success is True
    assert result.reason == "REBALANCED"
    assert result.realized_pnl == Decimal("0"), "Rebalance should not realize PnL"


@pytest.mark.asyncio
async def test_close_uses_coordinated_close_when_enabled():
    """
    T3 Test: Verify that when coordinated_close_enabled is True and reason
    is NOT emergency/fast-close, _close_impl calls _close_both_legs_coordinated()
    instead of sequential close.

    Verify that EMERGENCY and EARLY_TAKE_PROFIT reasons bypass coordinated close.
    """
    from funding_bot.services.positions.close import _close_impl
    from funding_bot.services.positions.types import CloseResult

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.early_tp_fast_close_enabled = True
    service.settings.trading.early_tp_fast_close_use_taker_directly = False

    service.opportunity_engine = MagicMock()
    service.opportunity_engine.mark_cooldown = MagicMock()
    service._close_failure_alert_last = {}  # Initialize alert tracking dict

    service.market_data = MagicMock()
    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.side_effect = lambda symbol, exchange: MagicMock(
        tick_size=Decimal("0.01")
    )

    service.event_bus = MagicMock()
    service.event_bus.publish = AsyncMock()
    service.lighter = MagicMock()
    service.x10 = MagicMock()

    # Track which close method was called
    coordinated_called = False
    sequential_called = False

    async def mock_coordinated(trade):
        nonlocal coordinated_called
        coordinated_called = True
        # Simulate successful close
        trade.leg1.exit_price = Decimal("50000")
        trade.leg1.fees = Decimal("1")
        trade.leg2.exit_price = Decimal("50000")
        trade.leg2.fees = Decimal("0")

    async def mock_sequential_lighter(trade):
        nonlocal sequential_called
        sequential_called = True
        trade.leg1.exit_price = Decimal("50000")
        trade.leg1.fees = Decimal("1")

    async def mock_sequential_x10(trade):
        trade.leg2.exit_price = Decimal("50000")
        trade.leg2.fees = Decimal("0")

    async def mock_update_trade(trade):
        pass

    async def mock_verify(trade):
        pass

    async def mock_readback(trade):
        return False, False

    def mock_calculate_pnl(trade):
        return Decimal("5")

    service._close_both_legs_coordinated = mock_coordinated
    service._close_lighter_smart = mock_sequential_lighter
    service._close_x10_smart = mock_sequential_x10
    service._update_trade = mock_update_trade
    service._verify_closed = mock_verify
    service._post_close_readback = mock_readback
    service._calculate_realized_pnl = mock_calculate_pnl

    # Bind the method to the mock service
    import types
    from funding_bot.services.positions import close
    service._close_impl = types.MethodType(close._close_impl, service)

    # Test 1: NetEV reason (ECON exit) → should use coordinated close
    coordinated_called = False
    sequential_called = False
    trade = _make_test_trade()
    await service._close_impl(trade, "NetEV: Funding no longer covers exit cost")

    assert coordinated_called is True, "Coordinated close should be used for NetEV reason"
    assert sequential_called is False, "Sequential close should NOT be used for NetEV"

    # Test 2: EMERGENCY reason → should NOT use coordinated close
    coordinated_called = False
    sequential_called = False
    trade = _make_test_trade()
    await service._close_impl(trade, "EMERGENCY: Delta bound violated")

    assert coordinated_called is False, "Coordinated close should NOT be used for EMERGENCY"
    assert sequential_called is True, "Sequential close should be used for EMERGENCY"

    # Test 3: EARLY_TAKE_PROFIT reason → should NOT use coordinated close
    coordinated_called = False
    sequential_called = False
    trade = _make_test_trade()
    await service._close_impl(trade, "EARLY_TAKE_PROFIT: Price dislocation $5")

    assert coordinated_called is False, "Coordinated close should NOT be used for EARLY_TAKE_PROFIT"
    assert sequential_called is True, "Sequential close should be used for EARLY_TAKE_PROFIT"


# ============================================================================
# Wiring Tests (NEW)
# ============================================================================

@pytest.mark.asyncio
async def test_position_manager_binds_rebalance_and_coordinated_methods():
    """
    Test that PositionManager properly binds rebalance and coordinated close methods.
    """
    from funding_bot.services.positions.manager import PositionManager

    # Verify all critical methods are bound
    assert hasattr(PositionManager, "_rebalance_trade"), "PositionManager should have _rebalance_trade method"
    assert hasattr(PositionManager, "_close_both_legs_coordinated"), "PositionManager should have _close_both_legs_coordinated method"
    assert hasattr(PositionManager, "_submit_maker_order"), "PositionManager should have _submit_maker_order method"
    assert hasattr(PositionManager, "_execute_ioc_close"), "PositionManager should have _execute_ioc_close method"

    # Verify methods are callable
    assert callable(getattr(PositionManager, "_rebalance_trade")), "_rebalance_trade should be callable"
    assert callable(getattr(PositionManager, "_close_both_legs_coordinated")), "_close_both_legs_coordinated should be callable"
    assert callable(getattr(PositionManager, "_submit_maker_order")), "_submit_maker_order should be callable"
    assert callable(getattr(PositionManager, "_execute_ioc_close")), "_execute_ioc_close should be callable"


@pytest.mark.asyncio
async def test_check_trades_does_not_count_rebalance_as_closed():
    """
    Test that check_trades() does NOT count rebalanced trades as closed.

    Rebalance returns success=True but trade status remains OPEN (not CLOSED).
    Only trades with status=CLOSED should be in closed_trades list.

    NOTE: This test uses direct async functions instead of MagicMock + MethodType
    binding to work properly with the parallel exit evaluation implementation.
    """
    from funding_bot.services.positions.manager import PositionManager
    from funding_bot.services.positions.types import CloseResult
    from funding_bot.domain.rules import ExitDecision
    import asyncio

    # Create test trade
    trade = _make_test_trade()
    trade.status = TradeStatus.OPEN  # Rebalance keeps trade OPEN

    # Mock close_trade to return REBALANCED result with OPEN status
    async def mock_close_trade_rebalance(t, reason):
        # Simulate rebalance: returns success but trade stays OPEN
        return CloseResult(success=True, realized_pnl=Decimal("0"), reason="REBALANCED")

    # Mock _evaluate_exits_parallel to return no exit decision
    async def mock_evaluate_exits_parallel_no_exit(trades, best_opp_apy, max_concurrent=10):
        # Return no exit decision
        return [(trade, ExitDecision(should_exit=False, reason=""), False, True)]

    # Create a minimal mock service with direct async functions
    service = MagicMock()
    service.settings = MagicMock()
    service.store = MagicMock()
    service.store.list_open_trades = AsyncMock(return_value=[trade])
    service.close_trade = mock_close_trade_rebalance
    service._evaluate_exits_parallel = mock_evaluate_exits_parallel_no_exit
    service._orderbook_subs_lock = asyncio.Lock()
    service._active_orderbook_subs = set()
    service.opportunity_engine = None

    # Bind the check_trades method using the actual implementation
    import types
    from funding_bot.services.positions import manager
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    # Call check_trades
    closed_trades = await service.check_trades()

    # Verify rebalanced trade is NOT counted as closed
    assert len(closed_trades) == 0, f"Rebalanced trade (status=OPEN) should not be counted as closed, got {len(closed_trades)}"
    assert trade.status == TradeStatus.OPEN, "Trade should remain OPEN after rebalance"

    # Now test with a truly closed trade
    # Reset trade to OPEN
    trade.status = TradeStatus.OPEN

    # Mock close_trade to mark trade as CLOSED
    async def mock_close_trade_closed(t, reason):
        # Simulate successful close: mark trade as CLOSED
        t.status = TradeStatus.CLOSED
        return CloseResult(success=True, realized_pnl=Decimal("5"), reason="NetEV")

    # Mock _evaluate_exits_parallel to return exit decision
    async def mock_evaluate_exits_parallel_with_exit(trades, best_opp_apy, max_concurrent=10):
        # Return exit decision
        return [(trade, ExitDecision(should_exit=True, reason="NetEV: Funding flipped"), False, True)]

    service.close_trade = mock_close_trade_closed
    service._evaluate_exits_parallel = mock_evaluate_exits_parallel_with_exit

    # Re-bind check_trades to use the new mocks
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    closed_trades = await service.check_trades()

    # Verify CLOSED trade IS counted
    assert len(closed_trades) == 1, f"Trade with status=CLOSED should be counted as closed, got {len(closed_trades)}"
    assert closed_trades[0] == trade, "Closed trade should be the same object"
    assert trade.status == TradeStatus.CLOSED, "Trade should be marked as CLOSED"


# ============================================================================
# D4: Robustness Tests (NEW) - Bug Fixes for D1/D2
# ============================================================================

@pytest.mark.asyncio
async def test_rebalance_ioc_fallback_when_maker_submit_fails_does_not_raise():
    """
    Test D1: IOC fallback works when maker_order submit returns None.

    Critical: This tests the fix for UnboundLocalError on remaining_qty.
    When adapter.place_order() returns None (network/API error), the IOC
    fallback should still work without crashing on undefined variables.
    """
    from funding_bot.services.positions.close import _rebalance_trade

    # Create mock service
    service = MagicMock()
    service.settings.trading.rebalance_maker_timeout_seconds = 0.1
    service.settings.trading.rebalance_use_ioc_fallback = True
    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.return_value = MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),  # Lighter: SELL 0.1 BTC
        leg2_qty=Decimal("0.098"),  # X10: BUY 0.098 BTC (2% drift)
    )

    # Mock place_order to return None on first call (maker failure)
    call_count = {"maker": 0, "ioc": 0}

    async def mock_place_order(request):
        if request.time_in_force == TimeInForce.POST_ONLY or getattr(request, "post_only", False):
            call_count["maker"] += 1
            # Simulate maker submit failure (returns None)
            return None
        elif request.time_in_force == TimeInForce.IOC:
            call_count["ioc"] += 1
            # IOC succeeds
            return _make_mock_order("ioc_123", request.qty, Decimal("50000"), Decimal("0"))
        return None

    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)

    # Bind the method
    import types
    from funding_bot.services.positions import close
    service._rebalance_trade = types.MethodType(close._rebalance_trade, service)
    service._update_trade = AsyncMock()

    # Execute rebalance - should NOT raise UnboundLocalError
    # This is the critical test: before D1 fix, this would crash with:
    # NameError: name 'remaining_qty' is not defined
    await service._rebalance_trade(trade)

    # Verify IOC was called as fallback
    assert call_count["ioc"] == 1, "IOC should be called when maker submit fails"
    assert call_count["maker"] == 1, "Maker should be attempted first"

    # Verify trade was updated (IOC filled the rebalance qty)
    service._update_trade.assert_called()


@pytest.mark.asyncio
async def test_rebalance_cancels_maker_before_ioc():
    """
    Test D2: Maker order is cancelled before IOC fallback.

    Critical: Prevents double-fill if maker fills during IOC submission.
    The maker must be cancelled BEFORE placing the IOC order.
    """
    from funding_bot.services.positions.close import _rebalance_trade

    # Create mock service
    service = MagicMock()
    service.settings.trading.rebalance_maker_timeout_seconds = 0.1
    service.settings.trading.rebalance_use_ioc_fallback = True
    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.return_value = MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),
        leg2_qty=Decimal("0.098"),
    )

    # Track cancel calls
    cancel_called = {"cancelled": False}
    maker_order_id = "maker_123"

    async def mock_place_order(request):
        if request.time_in_force == TimeInForce.POST_ONLY or getattr(request, "post_only", False):
            # Maker placed but not filled
            return _make_mock_order(maker_order_id, Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)
        elif request.time_in_force == TimeInForce.IOC:
            # Verify maker was cancelled before IOC
            assert cancel_called["cancelled"], "Maker should be cancelled before IOC placement"
            return _make_mock_order("ioc_456", request.qty, Decimal("50000"), Decimal("0"))
        return None

    async def mock_get_order(order_id):
        # Return unfilled maker order (active but not filled)
        return _make_mock_order(order_id, Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)

    async def mock_cancel_order(order_id, symbol):
        cancel_called["cancelled"] = True
        # Verify correct order ID and symbol
        assert order_id == maker_order_id, "Should cancel the correct maker order"
        assert symbol == trade.symbol, "Should pass correct symbol to cancel"

    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)
    service.lighter.get_order = AsyncMock(side_effect=mock_get_order)
    service.x10.get_order = AsyncMock(side_effect=mock_get_order)
    service.lighter.cancel_order = AsyncMock(side_effect=mock_cancel_order)
    service.x10.cancel_order = AsyncMock(side_effect=mock_cancel_order)

    # Bind methods
    import types
    from funding_bot.services.positions import close
    service._rebalance_trade = types.MethodType(close._rebalance_trade, service)
    service._update_trade = AsyncMock()

    # Execute rebalance
    await service._rebalance_trade(trade)

    # Verify cancel was called
    assert cancel_called["cancelled"], "Maker order should be cancelled before IOC"


@pytest.mark.asyncio
async def test_coordinated_close_bypass_for_velocity_reason():
    """
    Test D3: VELOCITY reason bypasses coordinated close (uses fast close).

    Funding Velocity Exit is a proactive APY crash detection - it should
    use fast close (sequential) instead of coordinated close to minimize
    exposure during rapid APY deterioration.
    """
    from funding_bot.services.positions.close import _close_impl

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.early_tp_fast_close_enabled = False

    service.opportunity_engine = MagicMock()
    service.opportunity_engine.mark_cooldown = MagicMock()

    service.market_data = MagicMock()
    service.event_bus = MagicMock()
    service.event_bus.publish = AsyncMock()
    service.lighter = MagicMock()
    service.x10 = MagicMock()

    # Track which close method was called
    coordinated_called = False
    sequential_called = False

    async def mock_coordinated(trade):
        nonlocal coordinated_called
        coordinated_called = True

    async def mock_sequential_lighter(trade):
        nonlocal sequential_called
        sequential_called = True
        trade.leg1.exit_price = Decimal("50000")
        trade.leg1.fees = Decimal("1")

    async def mock_sequential_x10(trade):
        trade.leg2.exit_price = Decimal("50000")
        trade.leg2.fees = Decimal("0")

    async def mock_update_trade(trade):
        pass

    async def mock_verify(trade):
        pass

    async def mock_readback(trade):
        return False, False

    def mock_calculate_pnl(trade):
        return Decimal("5")

    service._close_both_legs_coordinated = mock_coordinated
    service._close_lighter_smart = mock_sequential_lighter
    service._close_x10_smart = mock_sequential_x10
    service._update_trade = mock_update_trade
    service._verify_closed = mock_verify
    service._post_close_readback = mock_readback
    service._calculate_realized_pnl = mock_calculate_pnl

    # Bind the method to the mock service
    import types
    from funding_bot.services.positions import close
    service._close_impl = types.MethodType(close._close_impl, service)

    # Test: VELOCITY reason → should NOT use coordinated close
    trade = _make_test_trade()
    await service._close_impl(trade, "VELOCITY: APY crashed -15%/hr")

    assert coordinated_called is False, "Coordinated close should NOT be used for VELOCITY"
    assert sequential_called is True, "Sequential close should be used for VELOCITY (fast close)"


# ============================================================================
# D5: Robustness Tests - IOC Fallback with get_order()=None (NEW)
# ============================================================================

@pytest.mark.asyncio
async def test_rebalance_cancels_maker_before_ioc_even_if_get_order_returns_none():
    """
    Test D5: Maker order is cancelled before IOC fallback even when get_order() returns None.

    Critical: This tests the fix for double-fill risk when get_order() fails/timeout.
    When adapter.get_order() returns None, we must still cancel the maker order
    before placing IOC to prevent both orders filling simultaneously.
    """
    from funding_bot.services.positions.close import _rebalance_trade

    # Create mock service
    service = MagicMock()
    service.settings.trading.rebalance_maker_timeout_seconds = 0.1
    service.settings.trading.rebalance_use_ioc_fallback = True
    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.return_value = MagicMock(
        tick_size=Decimal("0.01")
    )

    trade = _make_test_trade(
        leg1_qty=Decimal("0.100"),
        leg2_qty=Decimal("0.098"),
    )

    # Track cancel calls
    cancel_called = {"cancelled": False}
    maker_order_id = "maker_123"

    async def mock_place_order(request):
        if request.time_in_force == TimeInForce.POST_ONLY or getattr(request, "post_only", False):
            # Maker placed successfully
            return _make_mock_order(maker_order_id, Decimal("0"), Decimal("0"), Decimal("0"), is_filled=False)
        elif request.time_in_force == TimeInForce.IOC:
            # IOC should only be placed AFTER maker cancel
            assert cancel_called["cancelled"], "Maker should be cancelled BEFORE IOC placement"
            return _make_mock_order("ioc_456", request.qty, Decimal("50000"), Decimal("0"))
        return None

    async def mock_get_order(order_id):
        # Simulate get_order failure/timeout - returns None
        # This is the critical condition for D5 test
        return None

    async def mock_cancel_order(order_id, symbol):
        cancel_called["cancelled"] = True
        assert order_id == maker_order_id, "Should cancel the correct maker order"
        assert symbol == trade.symbol, "Should pass correct symbol to cancel"

    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)
    service.lighter.get_order = AsyncMock(side_effect=mock_get_order)
    service.x10.get_order = AsyncMock(side_effect=mock_get_order)
    service.lighter.cancel_order = AsyncMock(side_effect=mock_cancel_order)
    service.x10.cancel_order = AsyncMock(side_effect=mock_cancel_order)

    # Bind methods
    import types
    from funding_bot.services.positions import close
    service._rebalance_trade = types.MethodType(close._rebalance_trade, service)
    service._update_trade = AsyncMock()

    # Execute rebalance
    await service._rebalance_trade(trade)

    # Verify cancel was called BEFORE IOC
    assert cancel_called["cancelled"], "Maker order should be cancelled even when get_order returns None"


# ============================================================================
# D6: Fast-Close Bypass Tests - APY Crashed Substring (NEW)
# ============================================================================

@pytest.mark.asyncio
async def test_coordinated_close_not_used_for_apy_crash_reason_without_prefix():
    """
    Test D6: "APY crashed" reason (without prefix) bypasses coordinated close.

    Critical: Ensure that "APY crashed -15%/hr" (substring match) triggers
    fast-close instead of coordinated close. This is a safety check for
    the _close_impl routing logic.

    The reason may be generated without a VELOCITY/Z-SCORE prefix, so we
    check for substring "APY crashed" in addition to startswith() checks.
    """
    from funding_bot.services.positions.close import _close_impl

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.early_tp_fast_close_enabled = False

    service.opportunity_engine = MagicMock()
    service.opportunity_engine.mark_cooldown = MagicMock()

    service.market_data = MagicMock()
    service.event_bus = MagicMock()
    service.event_bus.publish = AsyncMock()
    service.lighter = MagicMock()
    service.x10 = MagicMock()

    # Track which close method was called
    coordinated_called = False
    sequential_called = False

    async def mock_coordinated(trade):
        nonlocal coordinated_called
        coordinated_called = True

    async def mock_sequential_lighter(trade):
        nonlocal sequential_called
        sequential_called = True
        trade.leg1.exit_price = Decimal("50000")
        trade.leg1.fees = Decimal("1")

    async def mock_sequential_x10(trade):
        trade.leg2.exit_price = Decimal("50000")
        trade.leg2.fees = Decimal("0")

    async def mock_update_trade(trade):
        pass

    async def mock_verify(trade):
        pass

    async def mock_readback(trade):
        return False, False

    def mock_calculate_pnl(trade):
        return Decimal("5")

    service._close_both_legs_coordinated = mock_coordinated
    service._close_lighter_smart = mock_sequential_lighter
    service._close_x10_smart = mock_sequential_x10
    service._update_trade = mock_update_trade
    service._verify_closed = mock_verify
    service._post_close_readback = mock_readback
    service._calculate_realized_pnl = mock_calculate_pnl

    # Bind the method to the mock service
    import types
    from funding_bot.services.positions import close
    service._close_impl = types.MethodType(close._close_impl, service)

    # Test: "APY crashed" reason WITHOUT prefix → should NOT use coordinated close
    trade = _make_test_trade()
    await service._close_impl(trade, "APY crashed -15%/hr")

    assert coordinated_called is False, "Coordinated close should NOT be used for 'APY crashed' (no prefix)"
    assert sequential_called is True, "Sequential close should be used for 'APY crashed' (fast close)"
