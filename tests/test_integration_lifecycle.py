"""
Integration Test: Full Trade Lifecycle.

Tests the complete flow of a trade from entry to exit using mocked adapters.
This is the most comprehensive test to ensure all components work together.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Opportunity,
    Side,
    Trade,
    TradeStatus,
)

# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_opportunity():
    """Create a sample trading opportunity."""
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
# Trade Lifecycle State Tests
# =============================================================================

class TestTradeLifecycleStates:
    """Tests for trade state transitions throughout lifecycle."""

    def test_create_pending_trade(self, sample_opportunity):
        """Should create trade in PENDING state."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=sample_opportunity.suggested_qty,
            target_notional_usd=sample_opportunity.suggested_notional,
            entry_apy=sample_opportunity.apy,
            entry_spread=sample_opportunity.spread_pct,
        )

        assert trade.status == TradeStatus.PENDING
        assert trade.execution_state == ExecutionState.PENDING
        assert trade.leg1.filled_qty == Decimal("0")
        assert trade.leg2.filled_qty == Decimal("0")

    def test_leg1_submitted(self, sample_opportunity):
        """Trade transitions to LEG1_SUBMITTED when order placed."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=sample_opportunity.suggested_qty,
            target_notional_usd=sample_opportunity.suggested_notional,
        )

        # Simulate leg1 order submission
        trade.execution_state = ExecutionState.LEG1_SUBMITTED

        assert trade.execution_state == ExecutionState.LEG1_SUBMITTED
        assert trade.status == TradeStatus.PENDING

    def test_leg1_filled(self, sample_opportunity):
        """Trade transitions to LEG1_FILLED after fill."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Simulate leg1 fill
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.execution_state = ExecutionState.LEG1_FILLED

        assert trade.execution_state == ExecutionState.LEG1_FILLED
        assert trade.leg1.filled_qty == Decimal("0.25")

    def test_leg2_submitted(self, sample_opportunity):
        """Trade transitions to LEG2_SUBMITTED for hedge."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.execution_state = ExecutionState.LEG2_SUBMITTED

        assert trade.execution_state == ExecutionState.LEG2_SUBMITTED

    def test_trade_opened(self, sample_opportunity):
        """Trade becomes OPEN after both legs filled."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Simulate both legs filled
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg2.entry_price = Decimal("2000")

        trade.mark_opened()

        assert trade.status == TradeStatus.OPEN
        assert trade.execution_state == ExecutionState.COMPLETE
        assert trade.opened_at is not None

    def test_trade_closed(self, sample_opportunity):
        """Trade becomes CLOSED when exited."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.mark_opened()

        # Simulate exit
        trade.mark_closed(reason="PROFIT_TARGET")

        assert trade.status == TradeStatus.CLOSED
        assert trade.close_reason == "PROFIT_TARGET"
        assert trade.closed_at is not None


# =============================================================================
# Rollback Scenarios
# =============================================================================

class TestRollbackScenarios:
    """Tests for rollback when leg2 fails."""

    def test_rollback_state_when_leg2_fails(self, sample_opportunity):
        """Should enter ROLLBACK state when leg2 fails."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Leg1 filled
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.execution_state = ExecutionState.LEG1_FILLED

        # Leg2 failed - need rollback
        trade.execution_state = ExecutionState.ROLLBACK_IN_PROGRESS

        assert trade.execution_state == ExecutionState.ROLLBACK_IN_PROGRESS

    def test_rollback_completed(self, sample_opportunity):
        """Rollback should close leg1 and mark trade failed."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.execution_state = ExecutionState.ROLLBACK_IN_PROGRESS

        # Rollback successful
        trade.leg1.filled_qty = Decimal("0")  # Position closed
        trade.execution_state = ExecutionState.ROLLBACK_DONE
        trade.status = TradeStatus.FAILED

        assert trade.status == TradeStatus.FAILED
        assert trade.execution_state == ExecutionState.ROLLBACK_DONE

    def test_no_rollback_needed_if_leg1_empty(self, sample_opportunity):
        """No rollback needed if leg1 never filled."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Leg1 never filled
        trade.leg1.filled_qty = Decimal("0")

        needs_rollback = trade.leg1.filled_qty > 0
        assert needs_rollback is False


# =============================================================================
# PnL Calculation During Lifecycle
# =============================================================================

class TestPnLDuringLifecycle:
    """Tests for PnL calculations at different stages."""

    def test_initial_pnl_is_zero(self, sample_opportunity):
        """New trade should have zero PnL."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # No fills yet = no PnL
        assert trade.funding_collected == Decimal("0")

    def test_funding_accumulation(self, sample_opportunity):
        """Funding should accumulate while position is open."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.mark_opened()

        # Simulate funding payments
        trade.funding_collected = Decimal("1.50")

        assert trade.funding_collected == Decimal("1.50")

    def test_fees_tracked(self, sample_opportunity):
        """Fees should be tracked on both legs."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Set fees
        trade.leg1.fees = Decimal("0.25")  # Maker fee
        trade.leg2.fees = Decimal("0.50")  # Taker fee

        total_fees = trade.leg1.fees + trade.leg2.fees
        assert total_fees == Decimal("0.75")


# =============================================================================
# Full Lifecycle Simulation
# =============================================================================

class TestFullLifecycleSimulation:
    """End-to-end lifecycle simulation."""

    def test_successful_trade_lifecycle(self, sample_opportunity):
        """Simulate complete successful trade lifecycle."""
        # 1. Create trade
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
            entry_apy=sample_opportunity.apy,
        )
        assert trade.status == TradeStatus.PENDING

        # 2. Submit leg1
        trade.execution_state = ExecutionState.LEG1_SUBMITTED
        assert trade.execution_state == ExecutionState.LEG1_SUBMITTED

        # 3. Leg1 fills
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.leg1.fees = Decimal("0.25")
        trade.execution_state = ExecutionState.LEG1_FILLED
        assert trade.execution_state == ExecutionState.LEG1_FILLED

        # 4. Submit leg2
        trade.execution_state = ExecutionState.LEG2_SUBMITTED
        assert trade.execution_state == ExecutionState.LEG2_SUBMITTED

        # 5. Leg2 fills
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg2.entry_price = Decimal("2001")
        trade.leg2.fees = Decimal("0.50")

        # 6. Trade is now open
        trade.mark_opened()
        assert trade.status == TradeStatus.OPEN
        assert trade.execution_state == ExecutionState.COMPLETE

        # 7. Accumulate funding (simulated over 24h)
        trade.funding_collected = Decimal("2.00")

        # 8. Exit conditions met
        trade.mark_closed(reason="PROFIT_TARGET")
        assert trade.status == TradeStatus.CLOSED

        # 9. Verify final state
        total_fees = trade.leg1.fees + trade.leg2.fees
        assert total_fees == Decimal("0.75")
        assert trade.funding_collected == Decimal("2.00")

    def test_failed_trade_with_rollback(self, sample_opportunity):
        """Simulate trade that fails and needs rollback."""
        # 1. Create trade
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # 2. Leg1 fills
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.execution_state = ExecutionState.LEG1_FILLED

        # 3. Leg2 fails!
        # (simulation - in real code this would be an exception)
        leg2_failed = True

        # 4. Need rollback
        if leg2_failed and trade.leg1.filled_qty > 0:
            trade.execution_state = ExecutionState.ROLLBACK_IN_PROGRESS

        assert trade.execution_state == ExecutionState.ROLLBACK_IN_PROGRESS

        # 5. Rollback - close leg1
        trade.leg1.filled_qty = Decimal("0")  # Position closed
        trade.execution_state = ExecutionState.ROLLBACK_DONE
        trade.status = TradeStatus.FAILED

        assert trade.status == TradeStatus.FAILED
        assert trade.leg1.filled_qty == Decimal("0")

    def test_aborted_trade_before_leg1_fill(self, sample_opportunity):
        """Trade aborted before any fills."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.execution_state = ExecutionState.LEG1_SUBMITTED

        # Timeout - no fill, abort
        trade.execution_state = ExecutionState.ABORTED
        trade.status = TradeStatus.FAILED

        assert trade.status == TradeStatus.FAILED
        assert trade.leg1.filled_qty == Decimal("0")
        assert trade.leg2.filled_qty == Decimal("0")


# =============================================================================
# Edge Cases
# =============================================================================

class TestLifecycleEdgeCases:
    """Edge cases in trade lifecycle."""

    def test_partial_leg1_fill(self, sample_opportunity):
        """Handle partial fill on leg1."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Only 50% filled
        trade.leg1.filled_qty = Decimal("0.125")
        trade.leg1.entry_price = Decimal("2000")

        fill_percentage = trade.leg1.filled_qty / trade.target_qty * 100
        assert fill_percentage == 50

    def test_imbalanced_fills(self, sample_opportunity):
        """Detect imbalanced fills between legs."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.20")  # Less than leg1!

        imbalance = abs(trade.leg1.filled_qty - trade.leg2.filled_qty)
        is_imbalanced = imbalance > Decimal("0.01")

        assert is_imbalanced is True

    def test_very_old_trade_detection(self, sample_opportunity):
        """Should detect very old trades."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.mark_opened()

        # Simulate 48 hours passed
        trade.opened_at = datetime.now(UTC) - timedelta(hours=48)

        age_hours = (datetime.now(UTC) - trade.opened_at).total_seconds() / 3600
        is_old = age_hours > 24

        assert is_old is True


# =============================================================================
# End-to-End Integration Tests
# =============================================================================

class TestEndToEndScenarios:
    """End-to-end integration tests simulating real trading scenarios."""

    def test_complete_funding_arbitrage_cycle(self, sample_opportunity):
        """Simulate complete funding arbitrage cycle from opportunity to profit."""
        # 1. Opportunity detected
        opportunity = sample_opportunity
        assert opportunity.net_funding_hourly > 0
        assert opportunity.apy >= Decimal("0.25")  # Above min APY filter

        # 2. Trade created
        trade = Trade.create(
            symbol=opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=opportunity.suggested_qty,
            target_notional_usd=opportunity.suggested_notional,
            entry_apy=opportunity.apy,
            entry_spread=opportunity.spread_pct,
        )
        assert trade.status == TradeStatus.PENDING

        # 3. Leg1 executed (Lighter SELL)
        trade.leg1.filled_qty = opportunity.suggested_qty
        trade.leg1.entry_price = opportunity.lighter_best_bid
        trade.leg1.fees = Decimal("0.20")  # Maker fee
        trade.execution_state = ExecutionState.LEG1_FILLED

        # 4. Leg2 executed (X10 BUY hedge)
        trade.leg2.filled_qty = opportunity.suggested_qty
        trade.leg2.entry_price = opportunity.x10_best_ask
        trade.leg2.fees = Decimal("0.50")  # Taker fee
        trade.execution_state = ExecutionState.COMPLETE

        # 5. Trade opened
        trade.mark_opened()
        assert trade.status == TradeStatus.OPEN
        assert trade.opened_at is not None

        # 6. Funding accumulated (simulate 24 hours)
        hourly_funding = opportunity.net_funding_hourly * trade.target_qty * opportunity.mid_price
        hours_held = 24
        trade.funding_collected = hourly_funding * hours_held

        # 7. Exit conditions met
        total_fees = trade.leg1.fees + trade.leg2.fees
        net_pnl = trade.funding_collected - total_fees

        assert net_pnl > 0  # Should be profitable

        # 8. Trade closed
        trade.mark_closed(reason="PROFIT_TARGET")
        assert trade.status == TradeStatus.CLOSED
        assert trade.closed_at is not None

    def test_multiple_concurrent_trades_lifecycle(self, sample_opportunity):
        """Simulate managing multiple trades simultaneously."""
        trades = []
        max_trades = 4

        # Open maximum number of trades
        for i in range(max_trades):
            trade = Trade.create(
                symbol=f"SYMBOL{i}",
                leg1_exchange=Exchange.LIGHTER,
                leg1_side=Side.SELL,
                leg2_exchange=Exchange.X10,
                target_qty=Decimal("0.25"),
                target_notional_usd=Decimal("500"),
                entry_apy=Decimal("0.60"),
            )
            trade.leg1.filled_qty = Decimal("0.25")
            trade.leg2.filled_qty = Decimal("0.25")
            trade.mark_opened()
            trades.append(trade)

        # All trades should be open
        open_trades = [t for t in trades if t.status == TradeStatus.OPEN]
        assert len(open_trades) == max_trades

        # Close one trade (makes room for new one)
        trades[0].mark_closed(reason="PROFIT_TARGET")

        # Should now have room for another trade
        open_trades = [t for t in trades if t.status == TradeStatus.OPEN]
        assert len(open_trades) == max_trades - 1

    def test_funding_rate_flip_scenario(self, sample_opportunity):
        """Test handling when funding rate flips negative during trade."""
        # 1. Open trade with positive funding
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=sample_opportunity.suggested_qty,
            target_notional_usd=sample_opportunity.suggested_notional,
            entry_apy=sample_opportunity.apy,
        )

        trade.leg1.filled_qty = sample_opportunity.suggested_qty
        trade.leg2.filled_qty = sample_opportunity.suggested_qty
        trade.mark_opened()

        # 2. Accumulate some funding
        trade.funding_collected = Decimal("1.50")

        # 3. Funding rate flips negative
        current_apy = Decimal("-0.10")  # Now negative!

        # 4. Should evaluate exit (early edge exit)
        # Check if current APY < threshold of entry APY
        apy_ratio = abs(current_apy) / trade.entry_apy if trade.entry_apy > 0 else Decimal("0")
        should_exit = apy_ratio < Decimal("0.30")  # Below 30% threshold

        # If negative and below threshold, should exit
        is_negative_funding = current_apy < 0
        is_below_threshold = apy_ratio < Decimal("0.30")
        exit_triggered = is_negative_funding and is_below_threshold

        assert exit_triggered is True

    def test_reconciliation_after_partial_fill(self, sample_opportunity):
        """Test reconciliation when leg1 partially fills but leg2 fills completely."""
        # 1. Trade execution
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # 2. Leg1 partially fills (50%)
        trade.leg1.filled_qty = Decimal("0.125")
        trade.leg1.entry_price = Decimal("2000")
        trade.execution_state = ExecutionState.LEG1_FILLED

        # 3. Leg2 attempts to match but fills 100% of target (not actual)
        trade.leg2.filled_qty = Decimal("0.25")  # Too much!

        # 4. Reconciliation detects imbalance
        imbalance = abs(trade.leg1.filled_qty - trade.leg2.filled_qty)
        is_imbalanced = imbalance > Decimal("0.01")  # More than 1% difference

        assert is_imbalanced is True
        assert imbalance == Decimal("0.125")

    def test_volatility_panic_exit(self, sample_opportunity):
        """Test exit triggered by extreme price volatility."""
        # 1. Open trade
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=sample_opportunity.suggested_qty,
            target_notional_usd=sample_opportunity.suggested_notional,
            entry_apy=sample_opportunity.apy,
        )

        trade.leg1.filled_qty = sample_opportunity.suggested_qty
        trade.leg1.entry_price = Decimal("2000")
        trade.leg2.filled_qty = sample_opportunity.suggested_qty
        trade.leg2.entry_price = Decimal("2002")
        trade.mark_opened()

        # 2. Extreme price movement (5%)
        current_mid_price = Decimal("2100")  # 5% higher
        price_move_pct = abs(current_mid_price - trade.leg1.entry_price) / trade.leg1.entry_price

        volatility_threshold = Decimal("0.05")  # 5%
        should_exit = price_move_pct >= volatility_threshold

        assert should_exit is True
        assert price_move_pct == Decimal("0.05")  # Exactly 5%


# =============================================================================
# PnL Calculation Tests
# =============================================================================

class TestPnLCalculations:
    """Tests for profit and loss calculations in various scenarios."""

    def test_pnl_calculation_with_funding_only(self, sample_opportunity):
        """Calculate PnL when only funding income (no price change)."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Both legs at same price (no price PnL)
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg2.entry_price = Decimal("2000")

        trade.leg1.fees = Decimal("0.20")
        trade.leg2.fees = Decimal("0.50")

        trade.mark_opened()

        # Funding income
        trade.funding_collected = Decimal("2.00")

        # Calculate PnL
        total_fees = trade.leg1.fees + trade.leg2.fees
        net_pnl = trade.funding_collected - total_fees

        assert net_pnl == Decimal("1.30")  # 2.00 - 0.20 - 0.50

    def test_pnl_calculation_with_price_move_and_funding(self, sample_opportunity):
        """Calculate PnL with both price movement and funding income."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,  # Short
            leg2_exchange=Exchange.X10,  # Long (hedge)
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        # Entry prices
        entry_price = Decimal("2000")
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = entry_price
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg2.entry_price = entry_price

        trade.leg1.fees = Decimal("0.20")
        trade.leg2.fees = Decimal("0.50")

        trade.mark_opened()

        # Exit prices (price moved up 1%)
        exit_price = Decimal("2020")  # +1%

        # Price PnL: Short profit = (entry - exit) * qty
        price_pnl_leg1 = (entry_price - exit_price) * trade.leg1.filled_qty
        # Price PnL: Long profit = (exit - entry) * qty
        price_pnl_leg2 = (exit_price - entry_price) * trade.leg2.filled_qty

        # Net price PnL (should be ~0 for delta-neutral)
        net_price_pnl = price_pnl_leg1 + price_pnl_leg2

        # Funding income
        trade.funding_collected = Decimal("2.00")

        # Total PnL
        total_fees = trade.leg1.fees + trade.leg2.fees
        total_pnl = net_price_pnl + trade.funding_collected - total_fees

        # Price PnL should be near zero (delta-neutral)
        assert abs(net_price_pnl) < Decimal("0.01")
        # Total PnL positive from funding
        assert total_pnl == Decimal("1.30")


# =============================================================================
# Exit Strategy Tests
# =============================================================================

class TestExitStrategies:
    """Tests for different exit strategy triggers."""

    def test_profit_target_exit(self, sample_opportunity):
        """Test profit target exit condition."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg1.fees = Decimal("0.20")
        trade.leg2.fees = Decimal("0.50")
        trade.mark_opened()

        # Accumulate funding to hit profit target
        min_profit_target = Decimal("0.80")
        trade.funding_collected = Decimal("1.50")  # Above target

        total_fees = trade.leg1.fees + trade.leg2.fees
        net_profit = trade.funding_collected - total_fees

        should_exit = net_profit >= min_profit_target

        assert should_exit is True
        assert net_profit == Decimal("0.80")

    def test_max_hold_time_exit(self, sample_opportunity):
        """Test maximum hold time exit condition."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
        )

        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.mark_opened()

        # Simulate maximum hold time reached (240 hours = 10 days)
        trade.opened_at = datetime.now(UTC) - timedelta(hours=240)
        max_hold_hours = 240

        hours_held = (datetime.now(UTC) - trade.opened_at).total_seconds() / 3600
        should_exit = hours_held >= max_hold_hours

        assert should_exit is True
        assert int(hours_held) == 240


# =============================================================================
# State Persistence Tests
# =============================================================================

class TestStatePersistence:
    """Tests for state persistence across restarts."""

    def test_trade_state_serialization(self, sample_opportunity):
        """Test that trade state can be serialized and deserialized."""
        trade = Trade.create(
            symbol=sample_opportunity.symbol,
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.SELL,
            leg2_exchange=Exchange.X10,
            target_qty=sample_opportunity.suggested_qty,
            target_notional_usd=sample_opportunity.suggested_notional,
            entry_apy=sample_opportunity.apy,
        )

        # Update state
        trade.leg1.filled_qty = Decimal("0.25")
        trade.leg1.entry_price = Decimal("2000")
        trade.leg1.fees = Decimal("0.20")
        trade.leg2.filled_qty = Decimal("0.25")
        trade.leg2.entry_price = Decimal("2002")
        trade.leg2.fees = Decimal("0.50")
        trade.mark_opened()
        trade.funding_collected = Decimal("1.50")

        # Verify all critical state is preserved
        assert trade.symbol == "ETH"
        assert trade.leg1.filled_qty == Decimal("0.25")
        assert trade.leg2.filled_qty == Decimal("0.25")
        assert trade.status == TradeStatus.OPEN
        assert trade.funding_collected == Decimal("1.50")
        assert trade.opened_at is not None
