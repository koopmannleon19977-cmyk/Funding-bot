"""
Unit tests for Phase 2 Basis Convergence Exit Strategy.

Tests check_basis_convergence function which exits when price spread
between exchanges converges to near-zero, signaling arb completion.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.domain.models import Exchange, Side, Trade, TradeLeg, TradeStatus
from funding_bot.domain.rules import check_basis_convergence


pytestmark = pytest.mark.unit


@pytest.fixture
def sample_trade() -> Trade:
    """Create a sample trade for testing basis convergence."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="test-basis-001",
        symbol="ETH",
        status=TradeStatus.OPEN,
        target_qty=Decimal("0.1"),
        target_notional_usd=Decimal("400"),
        entry_apy=Decimal("0.50"),  # 50% APY at entry
        entry_spread=Decimal("0.005"),  # 0.5% entry spread
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.1"),
            order_id="order-1",
            fees=Decimal("0.08"),
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.1"),
            order_id="order-2",
            fees=Decimal("0.09"),
        ),
        created_at=now - timedelta(hours=4),
        opened_at=now - timedelta(hours=4),
    )


class TestBasisConvergence:
    """Tests for check_basis_convergence exit rule."""

    def test_spread_below_threshold_triggers_exit(self, sample_trade: Trade):
        """When spread < absolute threshold and PnL > min, should exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0003"),  # 0.03% - below 0.05% threshold
            entry_spread_pct=Decimal("0.005"),  # 0.5% at entry
            current_pnl=Decimal("2.00"),  # Profitable
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert result.passed
        assert "CONVERGED" in result.reason

    def test_spread_collapsed_80_percent_triggers_exit(self, sample_trade: Trade):
        """When spread drops by 80%+ from entry and profitable, should exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.001"),  # 0.1% - 80% collapse from 0.5%
            entry_spread_pct=Decimal("0.005"),  # 0.5% at entry
            current_pnl=Decimal("1.50"),  # Profitable
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),  # 20% of entry = 0.001 = exactly at threshold
            min_profit_usd=Decimal("0.50"),
        )
        assert result.passed
        assert "COLLAPSED" in result.reason

    def test_spread_still_wide_no_exit(self, sample_trade: Trade):
        """When spread is still significant, should hold."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.003"),  # 0.3% - still significant
            entry_spread_pct=Decimal("0.005"),  # 0.5% at entry
            current_pnl=Decimal("3.00"),  # Profitable
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert not result.passed
        assert "holding" in result.reason.lower()

    def test_profitable_but_spread_high_no_exit(self, sample_trade: Trade):
        """High profit but spread still wide should hold."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.004"),  # 0.4% - near entry
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("10.00"),  # Very profitable
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert not result.passed

    def test_spread_converged_but_negative_pnl_no_exit(self, sample_trade: Trade):
        """Don't exit at a loss even if spread converged."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0001"),  # 0.01% - fully converged
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("-1.00"),  # At a loss
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert not result.passed
        assert "PnL" in result.reason

    def test_spread_converged_minimal_profit_triggers(self, sample_trade: Trade):
        """Edge case: PnL exactly at minimum should trigger exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0004"),  # Just below threshold
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("0.50"),  # Exactly at minimum
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert result.passed

    def test_missing_spread_data_no_exit(self, sample_trade: Trade):
        """Missing spread data should not trigger exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=None,  # type: ignore - Missing data
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("5.00"),
        )
        assert not result.passed
        assert "Missing" in result.reason

    def test_negative_spread_handled_safely(self, sample_trade: Trade):
        """Negative spread (invalid data) should not crash."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("-0.001"),  # Invalid
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("5.00"),
        )
        assert not result.passed

    def test_zero_entry_spread_uses_absolute_threshold(self, sample_trade: Trade):
        """If entry spread is 0, only use absolute threshold."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0003"),  # Below absolute threshold
            entry_spread_pct=Decimal("0"),  # No entry spread recorded
            current_pnl=Decimal("2.00"),
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.20"),
            min_profit_usd=Decimal("0.50"),
        )
        assert result.passed
        assert "CONVERGED" in result.reason


class TestBasisConvergenceEdgeCases:
    """Edge case tests for basis convergence."""

    def test_exactly_at_threshold_triggers(self, sample_trade: Trade):
        """Spread exactly at threshold should trigger exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0005"),  # Exactly at threshold
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("1.00"),
            convergence_threshold_pct=Decimal("0.0005"),
        )
        assert result.passed

    def test_just_above_threshold_no_exit(self, sample_trade: Trade):
        """Spread just above threshold should not trigger."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.00051"),  # Just above
            entry_spread_pct=Decimal("0.005"),
            current_pnl=Decimal("1.00"),
            convergence_threshold_pct=Decimal("0.0005"),
            min_convergence_ratio=Decimal("0.10"),  # Set ratio so this doesn't trigger
        )
        # 0.00051 > 0.0005 (threshold) AND 0.00051 > 0.005 * 0.10 = 0.0005
        assert not result.passed

    def test_very_large_profit_with_convergence(self, sample_trade: Trade):
        """Large profit + convergence = definitely exit."""
        result = check_basis_convergence(
            sample_trade,
            current_spread_pct=Decimal("0.0001"),
            entry_spread_pct=Decimal("0.01"),  # 1% entry spread
            current_pnl=Decimal("100.00"),  # Large profit
        )
        assert result.passed
        assert "CONVERGED" in result.reason
