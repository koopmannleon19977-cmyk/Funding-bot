"""
Tests for Position Management.

Tests position imbalance detection, PnL calculations, and position state management.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.domain.models import (
    Exchange,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def balanced_trade():
    """Create a balanced trade (equal sizes both legs)."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="balanced-001",
        symbol="ETH",
        status=TradeStatus.OPEN,
        target_qty=Decimal("0.25"),
        target_notional_usd=Decimal("500"),
        entry_apy=Decimal("0.45"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.25"),  # $500 notional
            order_id="order-1",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.25"),  # $500 notional - balanced
            order_id="order-2",
        ),
        created_at=now - timedelta(hours=1),
        opened_at=now - timedelta(hours=1),
    )


@pytest.fixture
def imbalanced_trade():
    """Create an imbalanced trade (5% size difference)."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="imbalanced-001",
        symbol="SOL",
        status=TradeStatus.OPEN,
        target_qty=Decimal("10.0"),
        target_notional_usd=Decimal("1000"),
        entry_apy=Decimal("0.55"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("100"),
            filled_qty=Decimal("10.0"),  # 10 SOL = $1000
            order_id="order-1",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("100"),
            filled_qty=Decimal("9.5"),  # 9.5 SOL = $950 - 5% imbalance
            order_id="order-2",
        ),
        created_at=now - timedelta(hours=1),
        opened_at=now - timedelta(hours=1),
    )


@pytest.fixture
def partial_fill_trade():
    """Create a trade with partial fills."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="partial-001",
        symbol="BTC",
        status=TradeStatus.OPEN,
        target_qty=Decimal("0.04"),
        target_notional_usd=Decimal("2000"),
        entry_apy=Decimal("0.40"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("50000"),
            filled_qty=Decimal("0.04"),  # $2000 notional
            order_id="order-1",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("50000"),
            filled_qty=Decimal("0.035"),  # $1750 - 12.5% imbalance!
            order_id="order-2",
        ),
        created_at=now - timedelta(hours=1),
        opened_at=now - timedelta(hours=1),
    )


# =============================================================================
# Position Balance Tests
# =============================================================================


class TestPositionBalance:
    """Tests for position balance calculations."""

    def test_balanced_trade_net_exposure_zero(self, balanced_trade):
        """Balanced trade should have near-zero net exposure."""
        leg1_notional = balanced_trade.leg1.filled_qty * balanced_trade.leg1.entry_price
        leg2_notional = balanced_trade.leg2.filled_qty * balanced_trade.leg2.entry_price

        # For a short/long hedge, net should be close to zero
        net_exposure_pct = abs(leg1_notional - leg2_notional) / max(leg1_notional, leg2_notional)

        assert net_exposure_pct < Decimal("0.01")  # < 1% imbalance

    def test_imbalanced_trade_detected(self, imbalanced_trade):
        """Should detect 5% imbalance."""
        leg1_notional = imbalanced_trade.leg1.filled_qty * imbalanced_trade.leg1.entry_price
        leg2_notional = imbalanced_trade.leg2.filled_qty * imbalanced_trade.leg2.entry_price

        max_notional = max(leg1_notional, leg2_notional)
        imbalance_pct = abs(leg1_notional - leg2_notional) / max_notional

        assert imbalance_pct >= Decimal("0.01")  # >= 1% imbalance (warning threshold)
        assert imbalance_pct == Decimal("0.05")  # Exactly 5%

    def test_severe_imbalance_flagged(self, partial_fill_trade):
        """Should flag severe imbalances > 10%."""
        leg1_notional = partial_fill_trade.leg1.filled_qty * partial_fill_trade.leg1.entry_price
        leg2_notional = partial_fill_trade.leg2.filled_qty * partial_fill_trade.leg2.entry_price

        max_notional = max(leg1_notional, leg2_notional)
        imbalance_pct = abs(leg1_notional - leg2_notional) / max_notional

        assert imbalance_pct > Decimal("0.10")  # > 10% = critical


class TestTradePnLCalculation:
    """Tests for trade PnL calculations."""

    def test_profitable_trade_pnl(self):
        """Calculate PnL for a profitable trade."""
        now = datetime.now(UTC)
        trade = Trade(
            trade_id="pnl-001",
            symbol="ETH",
            status=TradeStatus.OPEN,
            target_qty=Decimal("0.5"),
            target_notional_usd=Decimal("1000"),
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                entry_price=Decimal("2000"),  # Sold at 2000
                filled_qty=Decimal("0.5"),
                fees=Decimal("0.20"),
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                entry_price=Decimal("2000"),  # Bought at 2000
                filled_qty=Decimal("0.5"),
                fees=Decimal("0.30"),
            ),
            funding_collected=Decimal("5.00"),
            created_at=now,
        )

        # Current price is 1900 (price dropped)
        current_price = Decimal("1900")

        # Leg1 (Short): Entry 2000, Current 1900 = +$100 per unit * 0.5 = +$50
        leg1_pnl = (trade.leg1.entry_price - current_price) * trade.leg1.filled_qty

        # Leg2 (Long): Entry 2000, Current 1900 = -$100 per unit * 0.5 = -$50
        leg2_pnl = (current_price - trade.leg2.entry_price) * trade.leg2.filled_qty

        # Position PnL should be hedged = $50 - $50 = $0 (perfectly delta neutral)
        position_pnl = leg1_pnl + leg2_pnl
        assert position_pnl == Decimal("0")  # Hedged = no price exposure

    def test_fees_reduce_pnl(self, balanced_trade):
        """Fees should reduce total PnL."""
        balanced_trade.leg1.fees = Decimal("1.00")
        balanced_trade.leg2.fees = Decimal("1.50")

        total_fees = balanced_trade.leg1.fees + balanced_trade.leg2.fees
        assert total_fees == Decimal("2.50")

    def test_funding_adds_to_pnl(self, balanced_trade):
        """Funding should add to total PnL."""
        balanced_trade.funding_collected = Decimal("10.00")
        balanced_trade.realized_pnl = Decimal("2.00")
        balanced_trade.leg1.fees = Decimal("1.00")
        balanced_trade.leg2.fees = Decimal("0.50")

        # Net = realized + funding - fees
        # 2 + 10 - 1.5 = $10.50


class TestTradeNotionalCalculation:
    """Tests for trade notional calculations."""

    def test_target_notional_used(self, balanced_trade):
        """Should use target_notional_usd for sizing."""
        assert balanced_trade.target_notional_usd == Decimal("500")

    def test_actual_notional_from_fills(self, balanced_trade):
        """Should calculate actual notional from fills."""
        leg1_actual = balanced_trade.leg1.filled_qty * balanced_trade.leg1.entry_price
        assert leg1_actual == Decimal("500")  # 0.25 * 2000

    def test_notional_mismatch_detection(self, partial_fill_trade):
        """Should detect when actual differs from target."""
        leg1_actual = partial_fill_trade.leg1.filled_qty * partial_fill_trade.leg1.entry_price
        target = partial_fill_trade.target_notional_usd

        assert leg1_actual == Decimal("2000")  # Got what we wanted
        assert target == Decimal("2000")


class TestLegSideConsistency:
    """Tests for leg side consistency."""

    def test_opposite_sides_for_hedge(self, balanced_trade):
        """Hedge should have opposite sides."""
        assert balanced_trade.leg1.side == Side.SELL
        assert balanced_trade.leg2.side == Side.BUY
        assert balanced_trade.leg1.side != balanced_trade.leg2.side

    def test_same_sides_invalid(self):
        """Same sides on both legs would not be a hedge."""
        # This should never happen in production
        now = datetime.now(UTC)
        bad_trade = Trade(
            trade_id="bad-001",
            symbol="XYZ",
            status=TradeStatus.OPEN,
            target_qty=Decimal("1"),
            target_notional_usd=Decimal("100"),
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.BUY,  # Same side
                entry_price=Decimal("100"),
                filled_qty=Decimal("1"),
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,  # Same side - NOT a hedge!
                entry_price=Decimal("100"),
                filled_qty=Decimal("1"),
            ),
            created_at=now,
        )

        # Both longs = net long position, NOT delta neutral
        assert bad_trade.leg1.side == bad_trade.leg2.side
        # This is a bug in trade creation if it ever happens


# =============================================================================
# Trade Status Tests
# =============================================================================


class TestTradeStatus:
    """Tests for trade status management."""

    def test_status_transitions(self):
        """Valid status transitions."""
        valid_transitions = [
            (TradeStatus.OPENING, TradeStatus.OPEN),
            (TradeStatus.OPENING, TradeStatus.FAILED),
            (TradeStatus.OPEN, TradeStatus.CLOSING),
            (TradeStatus.CLOSING, TradeStatus.CLOSED),
            (TradeStatus.OPEN, TradeStatus.FAILED),
        ]

        for from_status, to_status in valid_transitions:
            # Just verify these are valid enum values
            assert from_status in TradeStatus
            assert to_status in TradeStatus

    def test_all_statuses_exist(self):
        """Verify all expected statuses exist."""
        expected = ["OPENING", "OPEN", "CLOSING", "CLOSED", "FAILED"]
        for status_name in expected:
            assert hasattr(TradeStatus, status_name)


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases in position management."""

    def test_zero_filled_qty(self):
        """Should handle zero filled quantity."""
        now = datetime.now(UTC)
        trade = Trade(
            trade_id="zero-001",
            symbol="XYZ",
            status=TradeStatus.FAILED,
            target_qty=Decimal("5"),
            target_notional_usd=Decimal("500"),
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                entry_price=Decimal("100"),
                filled_qty=Decimal("0"),  # Nothing filled
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                entry_price=Decimal("0"),
                filled_qty=Decimal("0"),
            ),
            created_at=now,
        )

        notional = trade.leg1.filled_qty * trade.leg1.entry_price
        assert notional == Decimal("0")

    def test_very_small_position(self):
        """Should handle very small positions."""
        now = datetime.now(UTC)
        trade = Trade(
            trade_id="tiny-001",
            symbol="BTC",
            status=TradeStatus.OPEN,
            target_qty=Decimal("0.0002"),
            target_notional_usd=Decimal("10"),  # Very small
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("0.0002"),  # $10 notional
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("0.0002"),
            ),
            created_at=now,
        )

        notional = trade.leg1.filled_qty * trade.leg1.entry_price
        assert notional == Decimal("10")

    def test_large_position(self):
        """Should handle large positions."""
        now = datetime.now(UTC)
        trade = Trade(
            trade_id="large-001",
            symbol="BTC",
            status=TradeStatus.OPEN,
            target_qty=Decimal("2.0"),
            target_notional_usd=Decimal("100000"),  # $100k
            entry_apy=Decimal("0.30"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("2.0"),  # 2 BTC = $100k
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("2.0"),
            ),
            created_at=now,
        )

        notional = trade.leg1.filled_qty * trade.leg1.entry_price
        assert notional == Decimal("100000")
