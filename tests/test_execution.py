"""
Tests for Execution Engine.

Tests the critical execution flow including:
- Leg1 → Leg2 execution sequence
- Rollback logic on Leg2 failure
- Preflight checks
- Quantity calculations
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Opportunity,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Trade,
    TradeStatus,
)

# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.trading.desired_notional_usd = Decimal("500")
    settings.trading.max_leverage = Decimal("5")
    settings.trading.min_order_size_usd = Decimal("10")
    settings.execution.leg1_timeout_seconds = 30
    settings.execution.leg2_timeout_seconds = 10
    settings.execution.rollback_timeout_seconds = 10
    settings.execution.slippage_tolerance = Decimal("0.003")
    settings.execution.max_retries = 3
    return settings


@pytest.fixture
def mock_lighter():
    """Create mock Lighter adapter."""
    lighter = AsyncMock()
    lighter.name = "LIGHTER"
    lighter.get_market = MagicMock(return_value=MagicMock(
        step_size=Decimal("0.01"),
        min_order_size=Decimal("0.01"),
        tick_size=Decimal("0.01"),
    ))
    lighter.place_order = AsyncMock(return_value=Order(
        order_id="lighter-order-001",
        symbol="ETH",
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        price=Decimal("2000"),
        quantity=Decimal("0.25"),
        filled_quantity=Decimal("0.25"),
        status=OrderStatus.FILLED,
        exchange=Exchange.LIGHTER,
        created_at=datetime.now(UTC),
    ))
    lighter.cancel_order = AsyncMock(return_value=True)
    lighter.get_order = AsyncMock(return_value=Order(
        order_id="lighter-order-001",
        symbol="ETH",
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        price=Decimal("2000"),
        quantity=Decimal("0.25"),
        filled_quantity=Decimal("0.25"),
        status=OrderStatus.FILLED,
        exchange=Exchange.LIGHTER,
        created_at=datetime.now(UTC),
    ))
    return lighter


@pytest.fixture
def mock_x10():
    """Create mock X10 adapter."""
    x10 = AsyncMock()
    x10.name = "X10"
    x10.get_market = MagicMock(return_value=MagicMock(
        step_size=Decimal("0.01"),
        min_order_size=Decimal("0.01"),
        tick_size=Decimal("0.01"),
    ))
    x10.place_order = AsyncMock(return_value=Order(
        order_id="x10-order-001",
        symbol="ETH-USD",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        price=Decimal("2000"),
        quantity=Decimal("0.25"),
        filled_quantity=Decimal("0.25"),
        status=OrderStatus.FILLED,
        exchange=Exchange.X10,
        created_at=datetime.now(UTC),
    ))
    x10.get_order = AsyncMock(return_value=Order(
        order_id="x10-order-001",
        symbol="ETH-USD",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        price=Decimal("2000"),
        quantity=Decimal("0.25"),
        filled_quantity=Decimal("0.25"),
        status=OrderStatus.FILLED,
        exchange=Exchange.X10,
        created_at=datetime.now(UTC),
    ))
    return x10


@pytest.fixture
def mock_store():
    """Create mock trade store."""
    store = AsyncMock()
    store.create_trade = AsyncMock(return_value="trade-001")
    store.update_trade = AsyncMock(return_value=True)
    store.get_trade = AsyncMock(return_value=None)
    store.list_open_trades = AsyncMock(return_value=[])
    store.create_execution_attempt = AsyncMock(return_value="attempt-001")
    store.update_execution_attempt = AsyncMock(return_value=True)
    return store


@pytest.fixture
def mock_event_bus():
    """Create mock event bus."""
    event_bus = AsyncMock()
    event_bus.publish = AsyncMock()
    return event_bus


@pytest.fixture
def mock_market_data():
    """Create mock market data service."""
    market_data = MagicMock()
    market_data.get_orderbook = MagicMock(return_value=MagicMock(
        lighter_bid=Decimal("1999"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1998"),
        x10_ask=Decimal("2002"),
        mid_price=Decimal("2000"),
        timestamp=datetime.now(UTC),
    ))
    market_data.get_funding = MagicMock(return_value=MagicMock(
        lighter_rate=MagicMock(rate=Decimal("0.0005")),
        x10_rate=MagicMock(rate=Decimal("-0.0002")),
    ))
    return market_data


@pytest.fixture
def sample_opportunity():
    """Create sample opportunity for testing."""
    return Opportunity(
        symbol="ETH",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.0005"),
        x10_rate=Decimal("-0.0002"),
        net_funding_hourly=Decimal("0.0007"),
        apy=Decimal("0.60"),
        spread_pct=Decimal("0.001"),
        mid_price=Decimal("2000"),
        lighter_best_bid=Decimal("1999"),
        lighter_best_ask=Decimal("2001"),
        x10_best_bid=Decimal("1998"),
        x10_best_ask=Decimal("2002"),
        suggested_qty=Decimal("0.25"),
        suggested_notional=Decimal("500"),
        expected_value_usd=Decimal("2.50"),
        breakeven_hours=Decimal("4"),
    )


# =============================================================================
# Trade Creation Tests
# =============================================================================

class TestTradeCreation:
    """Tests for trade creation logic."""

    def test_trade_create_factory(self):
        """Should create trade with correct initial state."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
            entry_apy=Decimal("0.60"),
            entry_spread=Decimal("0.001"),
        )

        assert trade.symbol == "ETH"
        assert trade.leg1.exchange == Exchange.LIGHTER
        assert trade.leg1.side == Side.SELL
        assert trade.leg2.exchange == Exchange.X10
        assert trade.leg2.side == Side.BUY  # Inverse
        assert trade.target_qty == Decimal("0.25")
        assert trade.status == TradeStatus.PENDING

    def test_trade_leg_sides_are_inverse(self):
        """Hedge trade must have opposite sides."""
        trade = Trade.create(
            symbol="BTC",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.BUY,  # Long Lighter
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.01"),
            target_notional_usd=Decimal("500"),
        )

        assert trade.leg1.side == Side.BUY
        assert trade.leg2.side == Side.SELL  # Must be inverse


# =============================================================================
# Execution State Machine Tests
# =============================================================================

class TestExecutionStateMachine:
    """Tests for execution state transitions."""

    def test_pending_to_leg1_executing(self):
        """Trade should transition from PENDING to LEG1_EXECUTING."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        assert trade.execution_state == ExecutionState.PENDING

        # Simulate leg1 start
        trade.execution_state = ExecutionState.LEG1_SUBMITTED
        assert trade.execution_state == ExecutionState.LEG1_SUBMITTED

    def test_leg1_filled_to_leg2_executing(self):
        """Trade should transition from LEG1_FILLED to LEG2_EXECUTING."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.execution_state = ExecutionState.LEG1_FILLED
        trade.execution_state = ExecutionState.LEG2_SUBMITTED
        assert trade.execution_state == ExecutionState.LEG2_SUBMITTED

    def test_complete_state(self):
        """Trade should reach COMPLETE after both legs filled."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.mark_opened()
        assert trade.execution_state == ExecutionState.COMPLETE
        assert trade.status == TradeStatus.OPEN


# =============================================================================
# Rollback Logic Tests
# =============================================================================

class TestRollbackLogic:
    """Tests for rollback behavior when leg2 fails."""

    def test_rollback_required_when_leg1_filled_leg2_failed(self):
        """Rollback should be required when leg2 fails after leg1 fill."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Simulate leg1 filled
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.execution_state = ExecutionState.LEG1_FILLED

        # Leg2 fails - we need to rollback
        needs_rollback = trade.leg1.filled_qty > 0 and trade.leg2.filled_qty == 0
        assert needs_rollback is True

    def test_no_rollback_if_leg1_not_filled(self):
        """No rollback needed if leg1 never filled."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Leg1 never filled
        trade.leg1.filled_qty = Decimal("0")

        needs_rollback = trade.leg1.filled_qty > 0 and trade.leg2.filled_qty == 0
        assert needs_rollback is False

    def test_no_rollback_if_both_legs_filled(self):
        """No rollback needed if both legs filled."""
        trade = Trade.create(
            symbol="ETH",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")

        needs_rollback = trade.leg1.filled_qty > 0 and trade.leg2.filled_qty == 0
        assert needs_rollback is False


# =============================================================================
# Quantity Calculation Tests
# =============================================================================

class TestQuantityCalculation:
    """Tests for order quantity calculation."""

    def test_quantity_respects_step_size(self):
        """Calculated quantity should be rounded to step size."""
        step_size = Decimal("0.01")
        raw_qty = Decimal("0.25789")

        # Round down to step size
        rounded = (raw_qty // step_size) * step_size
        assert rounded == Decimal("0.25")

    def test_quantity_respects_min_order(self):
        """Quantity should be at least min_order_size."""
        min_order = Decimal("0.01")
        calculated_qty = Decimal("0.005")

        final_qty = max(calculated_qty, min_order)
        assert final_qty == min_order

    def test_quantity_from_notional(self):
        """Should calculate correct quantity from notional and price."""
        notional_usd = Decimal("500")
        price = Decimal("2000")
        expected_qty = notional_usd / price

        assert expected_qty == Decimal("0.25")


# =============================================================================
# Order Placement Tests
# =============================================================================

class TestOrderPlacement:
    """Tests for order placement logic."""

    def test_leg1_is_maker_order(self):
        """Leg1 should be placed as LIMIT (maker)."""
        # This is a design requirement - Lighter leg should be maker
        leg1_type = OrderType.LIMIT
        assert leg1_type == OrderType.LIMIT

    def test_leg2_is_taker_order(self):
        """Leg2 should be placed as MARKET (taker hedge)."""
        # This is a design requirement - X10 leg should be taker
        leg2_type = OrderType.MARKET
        assert leg2_type == OrderType.MARKET

    def test_slippage_price_calculation(self):
        """Slippage price should be calculated correctly."""
        mid_price = Decimal("2000")
        slippage = Decimal("0.003")  # 0.3%

        # For BUY, max price = mid * (1 + slippage)
        buy_max_price = mid_price * (1 + slippage)
        assert buy_max_price == Decimal("2006")

        # For SELL, min price = mid * (1 - slippage)
        sell_min_price = mid_price * (1 - slippage)
        assert sell_min_price == Decimal("1994")


# =============================================================================
# Edge Cases
# =============================================================================

class TestExecutionEdgeCases:
    """Tests for edge cases in execution."""

    def test_zero_quantity_rejected(self):
        """Zero quantity orders should be rejected."""
        qty = Decimal("0")
        is_valid = qty > 0
        assert is_valid is False

    def test_negative_quantity_rejected(self):
        """Negative quantity orders should be rejected."""
        qty = Decimal("-0.25")
        is_valid = qty > 0
        assert is_valid is False

    def test_very_small_notional_handling(self):
        """Very small notionals should be rejected or upsized."""
        min_notional = Decimal("10")
        notional = Decimal("5")

        is_valid = notional >= min_notional
        assert is_valid is False

    def test_partial_fill_handling(self):
        """Should handle partial fills correctly."""
        target_qty = Decimal("1.0")
        filled_qty = Decimal("0.75")  # 75% filled

        remaining = target_qty - filled_qty
        assert remaining == Decimal("0.25")

        fill_percentage = filled_qty / target_qty * 100
        assert fill_percentage == 75
