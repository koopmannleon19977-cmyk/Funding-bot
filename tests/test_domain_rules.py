"""
Comprehensive Tests for Domain Rules.

Tests all entry and exit rules to ensure the bot behaves correctly
in various market conditions.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.domain.models import (
    Balance,
    Exchange,
    Opportunity,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.domain.rules import (
    check_balance,
    check_funding_flip,
    check_max_hold_time,
    check_max_spread,
    check_max_trades,
    check_min_apy,
    check_min_expected_profit,
    check_min_hold_time,
    check_profit_target,
    get_dynamic_max_spread,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_trade():
    """Create a sample trade for testing."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="test-001",
        symbol="BTC",
        status=TradeStatus.OPEN,
        target_qty=Decimal("0.002"),
        target_notional_usd=Decimal("100"),
        entry_apy=Decimal("0.50"),  # 50% APY at entry
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("50000"),
            filled_qty=Decimal("0.002"),
            order_id="order-1",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("50000"),
            filled_qty=Decimal("0.002"),
            order_id="order-2",
        ),
        created_at=now - timedelta(hours=2),
        opened_at=now - timedelta(hours=2),
    )


@pytest.fixture
def sample_opportunity():
    """Create a sample opportunity for testing."""
    return Opportunity(
        symbol="ETH",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.0005"),  # 0.05% hourly
        x10_rate=Decimal("-0.0002"),  # -0.02% hourly
        net_funding_hourly=Decimal("0.0007"),
        apy=Decimal("0.60"),  # 60% APY - this is what check_min_apy uses
        spread_pct=Decimal("0.001"),  # 0.1% spread
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


@pytest.fixture
def sample_balance():
    """Create a sample balance for testing."""
    return Balance(
        exchange=Exchange.LIGHTER,
        available=Decimal("1000"),
        total=Decimal("1500"),
    )


# =============================================================================
# Entry Rules Tests
# =============================================================================


class TestCheckMinApy:
    """Tests for check_min_apy rule."""

    def test_passes_when_above_threshold(self, sample_opportunity):
        """Should pass when APY is above minimum."""
        result = check_min_apy(sample_opportunity, min_apy=Decimal("0.40"))
        assert result.passed is True

    def test_fails_when_below_threshold(self):
        """Should fail when APY is below minimum."""
        low_apy_opportunity = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(UTC),
            lighter_rate=Decimal("0.0001"),
            x10_rate=Decimal("-0.0001"),
            net_funding_hourly=Decimal("0.0002"),
            apy=Decimal("0.30"),  # 30% - below threshold
            spread_pct=Decimal("0.001"),
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1999"),
            lighter_best_ask=Decimal("2001"),
            x10_best_bid=Decimal("1998"),
            x10_best_ask=Decimal("2002"),
            suggested_qty=Decimal("0.25"),
            suggested_notional=Decimal("500"),
            expected_value_usd=Decimal("1.00"),
            breakeven_hours=Decimal("4"),
        )
        result = check_min_apy(low_apy_opportunity, min_apy=Decimal("0.40"))
        assert result.passed is False
        assert "30.00%" in result.reason

    def test_passes_at_exact_threshold(self):
        """Should pass when APY equals minimum."""
        exact_apy_opportunity = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(UTC),
            lighter_rate=Decimal("0.0002"),
            x10_rate=Decimal("-0.0001"),
            net_funding_hourly=Decimal("0.0003"),
            apy=Decimal("0.40"),  # Exactly at threshold
            spread_pct=Decimal("0.001"),
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1999"),
            lighter_best_ask=Decimal("2001"),
            x10_best_bid=Decimal("1998"),
            x10_best_ask=Decimal("2002"),
            suggested_qty=Decimal("0.25"),
            suggested_notional=Decimal("500"),
            expected_value_usd=Decimal("1.50"),
            breakeven_hours=Decimal("4"),
        )
        result = check_min_apy(exact_apy_opportunity, min_apy=Decimal("0.40"))
        assert result.passed is True


class TestCheckMaxSpread:
    """Tests for check_max_spread rule."""

    def test_passes_when_below_max(self, sample_opportunity):
        """Should pass when spread is below maximum."""
        result = check_max_spread(sample_opportunity, max_spread=Decimal("0.002"))
        assert result.passed is True

    def test_fails_when_above_max(self):
        """Should fail when spread exceeds maximum."""
        high_spread_opportunity = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(UTC),
            lighter_rate=Decimal("0.0005"),
            x10_rate=Decimal("-0.0002"),
            net_funding_hourly=Decimal("0.0007"),
            apy=Decimal("0.60"),
            spread_pct=Decimal("0.005"),  # 0.5% - above max
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1990"),
            lighter_best_ask=Decimal("2010"),
            x10_best_bid=Decimal("1985"),
            x10_best_ask=Decimal("2015"),
            suggested_qty=Decimal("0.25"),
            suggested_notional=Decimal("500"),
            expected_value_usd=Decimal("2.50"),
            breakeven_hours=Decimal("4"),
        )
        result = check_max_spread(high_spread_opportunity, max_spread=Decimal("0.002"))
        assert result.passed is False


class TestGetDynamicMaxSpread:
    """Tests for dynamic spread calculation."""

    def test_high_apy_gets_tighter_spread(self):
        """High APY (>100%) should have tightest spread tolerance (0.5%)."""
        result = get_dynamic_max_spread(
            apy=Decimal("1.50"),  # 150% APY
            base_max_spread=Decimal("0.012"),
        )
        assert result == Decimal("0.005")  # 0.5%

    def test_medium_apy_gets_medium_spread(self):
        """Medium APY (>50%) should have 0.4% spread tolerance."""
        result = get_dynamic_max_spread(
            apy=Decimal("0.60"),  # 60% APY
            base_max_spread=Decimal("0.012"),
        )
        assert result == Decimal("0.004")  # 0.4%

    def test_moderate_apy_gets_normal_spread(self):
        """Moderate APY (>20%) should have 0.3% spread tolerance."""
        result = get_dynamic_max_spread(
            apy=Decimal("0.25"),  # 25% APY
            base_max_spread=Decimal("0.012"),
        )
        assert result == Decimal("0.003")  # 0.3%

    def test_low_apy_uses_base_spread(self):
        """Low APY (<20%) should use base spread."""
        result = get_dynamic_max_spread(
            apy=Decimal("0.15"),  # 15% APY
            base_max_spread=Decimal("0.0012"),
        )
        assert result == Decimal("0.0012")

    def test_cap_is_respected(self):
        """Cap should never be exceeded."""
        result = get_dynamic_max_spread(
            apy=Decimal("0.10"),
            base_max_spread=Decimal("0.050"),
            cap_max_spread=Decimal("0.002"),
        )
        assert result == Decimal("0.002")


class TestCheckMinExpectedProfit:
    """Tests for minimum expected profit rule."""

    def test_passes_when_profit_sufficient(self, sample_opportunity):
        """Should pass when expected profit exceeds minimum."""
        # sample_opportunity has expected_value_usd=2.50
        result = check_min_expected_profit(sample_opportunity, min_profit_usd=Decimal("0.50"))
        assert result.passed is True

    def test_fails_when_profit_insufficient(self):
        """Should fail when expected profit is below minimum."""
        low_profit_opportunity = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(UTC),
            lighter_rate=Decimal("0.0001"),
            x10_rate=Decimal("-0.0001"),
            net_funding_hourly=Decimal("0.0002"),
            apy=Decimal("0.20"),
            spread_pct=Decimal("0.001"),
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1999"),
            lighter_best_ask=Decimal("2001"),
            x10_best_bid=Decimal("1998"),
            x10_best_ask=Decimal("2002"),
            suggested_qty=Decimal("0.25"),
            suggested_notional=Decimal("500"),
            expected_value_usd=Decimal("0.30"),  # Below threshold
            breakeven_hours=Decimal("4"),
        )
        result = check_min_expected_profit(low_profit_opportunity, min_profit_usd=Decimal("0.50"))
        assert result.passed is False


class TestCheckBalance:
    """Tests for balance check rule."""

    def test_passes_with_sufficient_balance(self, sample_balance):
        """Should pass when balance covers requirement with buffer."""
        result = check_balance(
            required_notional=Decimal("500"),
            balance=sample_balance,
            margin_buffer=Decimal("1.2"),
        )
        assert result.passed is True

    def test_fails_with_insufficient_balance(self, sample_balance):
        """Should fail when balance doesn't cover requirement."""
        result = check_balance(
            required_notional=Decimal("900"),  # Need 900*1.2=1080
            balance=sample_balance,  # Only 1000 available
            margin_buffer=Decimal("1.2"),
        )
        assert result.passed is False


class TestCheckMaxTrades:
    """Tests for max trades rule."""

    def test_passes_when_under_limit(self):
        """Should pass when current trades under limit."""
        result = check_max_trades(current_count=1, max_trades=3)
        assert result.passed is True

    def test_fails_when_at_limit(self):
        """Should fail when already at max trades."""
        result = check_max_trades(current_count=3, max_trades=3)
        assert result.passed is False


# =============================================================================
# Exit Rules Tests
# =============================================================================


class TestCheckProfitTarget:
    """Tests for profit target rule."""

    def test_triggers_when_profit_reached(self, sample_trade):
        """Should trigger exit when profit target is met."""
        result = check_profit_target(
            trade=sample_trade,
            current_pnl=Decimal("15.00"),
            target_usd=Decimal("10.00"),
        )
        assert result.passed is True

    def test_no_exit_when_below_target(self, sample_trade):
        """Should not trigger when below target."""
        result = check_profit_target(
            trade=sample_trade,
            current_pnl=Decimal("5.00"),
            target_usd=Decimal("10.00"),
        )
        assert result.passed is False


class TestCheckMinHoldTime:
    """Tests for minimum hold time rule."""

    def test_blocks_exit_before_min_time(self, sample_trade):
        """Should block early exits."""
        sample_trade.opened_at = datetime.now(UTC) - timedelta(minutes=30)
        result = check_min_hold_time(sample_trade, min_seconds=3600)  # 1 hour
        assert result.passed is False

    def test_allows_exit_after_min_time(self, sample_trade):
        """Should allow exit after minimum time."""
        sample_trade.opened_at = datetime.now(UTC) - timedelta(hours=2)
        result = check_min_hold_time(sample_trade, min_seconds=3600)  # 1 hour
        assert result.passed is True


class TestCheckMaxHoldTime:
    """Tests for maximum hold time rule."""

    def test_triggers_after_max_time(self, sample_trade):
        """Should trigger forced exit after max hold time."""
        sample_trade.opened_at = datetime.now(UTC) - timedelta(hours=50)
        result = check_max_hold_time(sample_trade, max_hours=Decimal("48"))
        assert result.passed is True

    def test_no_exit_before_max_time(self, sample_trade):
        """Should not trigger before max hold time."""
        sample_trade.opened_at = datetime.now(UTC) - timedelta(hours=24)
        result = check_max_hold_time(sample_trade, max_hours=Decimal("48"))
        assert result.passed is False


class TestCheckFundingFlip:
    """Tests for funding flip detection."""

    def test_no_flip_when_rates_favorable(self, sample_trade):
        """Should not trigger when funding rates are still favorable."""
        sample_trade.leg1.side = Side.SELL  # Short Lighter, Long X10
        result = check_funding_flip(
            trade=sample_trade,
            current_lighter_rate=Decimal("0.0005"),  # Positive = we earn
            current_x10_rate=Decimal("-0.0002"),  # Negative = we pay less
            hours_threshold=Decimal("8"),
            current_pnl=Decimal("5.00"),
            estimated_exit_cost_usd=Decimal("1.00"),
        )
        assert result.passed is False

    def test_flip_triggers_profit_lock(self, sample_trade):
        """Should trigger to lock profits when funding flips."""
        sample_trade.leg1.side = Side.SELL
        sample_trade.entry_apy = Decimal("0.50")
        result = check_funding_flip(
            trade=sample_trade,
            current_lighter_rate=Decimal("-0.0010"),  # Flipped negative
            current_x10_rate=Decimal("0.0005"),  # Now we pay more on X10
            hours_threshold=Decimal("8"),
            current_pnl=Decimal("10.00"),  # Profitable
            estimated_exit_cost_usd=Decimal("2.00"),
        )
        assert result.passed is True
        assert "profit" in result.reason.lower()

    def test_flip_hold_when_exit_expensive(self, sample_trade):
        """Should hold despite flip when exit cost > projected loss."""
        sample_trade.leg1.side = Side.SELL
        sample_trade.entry_apy = Decimal("0.50")
        sample_trade.target_notional_usd = Decimal("100")
        result = check_funding_flip(
            trade=sample_trade,
            current_lighter_rate=Decimal("-0.0001"),  # Slightly negative
            current_x10_rate=Decimal("0.0001"),  # Slightly positive
            hours_threshold=Decimal("8"),
            current_pnl=Decimal("0.50"),  # Slightly profitable but < exit cost
            estimated_exit_cost_usd=Decimal("5.00"),  # High exit cost
        )
        assert result.passed is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_negative_pnl_handling(self, sample_trade):
        """Should handle negative PnL correctly."""
        result = check_profit_target(
            trade=sample_trade,
            current_pnl=Decimal("-5.00"),
            target_usd=Decimal("10.00"),
        )
        assert result.passed is False

    def test_decimal_precision(self):
        """Should maintain decimal precision in calculations."""
        result = get_dynamic_max_spread(
            apy=Decimal("0.123456789"),
            base_max_spread=Decimal("0.0012"),
        )
        # Low APY, should use base
        assert result == Decimal("0.0012")


# =============================================================================
# Integration-Style Tests
# =============================================================================


class TestExitDecisionFlow:
    """Tests simulating realistic exit decision scenarios."""

    def test_graceful_exit_profitable_trade(self, sample_trade):
        """Trade that's profitable and should exit gracefully."""
        sample_trade.entry_apy = Decimal("0.60")
        sample_trade.target_notional_usd = Decimal("400")

        # Profit target check
        profit_result = check_profit_target(
            trade=sample_trade,
            current_pnl=Decimal("12.00"),
            target_usd=Decimal("10.00"),
        )
        assert profit_result.passed is True
