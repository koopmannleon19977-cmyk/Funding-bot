"""
Unit tests for positions/close.py helper functions.

Tests the refactored helper functions extracted in Phase 2.5:
- Grid step close helpers
- Rebalance helpers
- Leg update helpers

OFFLINE-FIRST: These tests do NOT require exchange SDK or network access.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from funding_bot.domain.models import Exchange, Side, TradeLeg
from funding_bot.services.positions.close import (
    _calculate_grid_price_levels,
    _calculate_rebalance_target,
    _update_leg_after_fill,
)


# =============================================================================
# GRID STEP HELPER TESTS
# =============================================================================


class TestCalculateGridPriceLevels:
    """Tests for _calculate_grid_price_levels helper."""

    def _make_mock_grid_config(
        self,
        num_levels: int = 5,
        grid_step: Decimal = Decimal("0.001"),
        qty_per_level: Decimal = Decimal("1.0"),
    ) -> MagicMock:
        """Create mock grid config."""
        config = MagicMock()
        config.num_levels = num_levels
        config.grid_step = grid_step
        config.qty_per_level = qty_per_level
        return config

    def test_sell_side_prices_decrease(self):
        """SELL side (close long) should have decreasing prices."""
        config = self._make_mock_grid_config(num_levels=3, grid_step=Decimal("0.01"))
        base_price = Decimal("100")
        tick = Decimal("0.01")

        levels = _calculate_grid_price_levels(base_price, Side.SELL, config, tick)

        assert len(levels) == 3
        # Prices should decrease: 100, 99, 98
        assert levels[0] == Decimal("100")
        assert levels[1] == Decimal("99")
        assert levels[2] == Decimal("98")

    def test_buy_side_prices_increase(self):
        """BUY side (close short) should have increasing prices."""
        config = self._make_mock_grid_config(num_levels=3, grid_step=Decimal("0.01"))
        base_price = Decimal("100")
        tick = Decimal("0.01")

        levels = _calculate_grid_price_levels(base_price, Side.BUY, config, tick)

        assert len(levels) == 3
        # Prices should increase: 100, 101, 102
        assert levels[0] == Decimal("100")
        assert levels[1] == Decimal("101")
        assert levels[2] == Decimal("102")

    def test_prices_rounded_to_tick(self):
        """Prices should be rounded to tick size."""
        config = self._make_mock_grid_config(num_levels=2, grid_step=Decimal("0.0015"))
        base_price = Decimal("100")
        tick = Decimal("0.01")  # 2 decimal places

        levels = _calculate_grid_price_levels(base_price, Side.SELL, config, tick)

        # All prices should be multiples of tick
        for price in levels:
            # price / tick should be a whole number
            assert price % tick == Decimal("0")

    def test_single_level_grid(self):
        """Single level grid should return just base price."""
        config = self._make_mock_grid_config(num_levels=1)
        base_price = Decimal("100")
        tick = Decimal("0.01")

        levels = _calculate_grid_price_levels(base_price, Side.SELL, config, tick)

        assert len(levels) == 1
        assert levels[0] == Decimal("100")

    def test_small_tick_precision(self):
        """Should handle small tick sizes correctly."""
        config = self._make_mock_grid_config(num_levels=3, grid_step=Decimal("0.001"))
        base_price = Decimal("0.5432")
        tick = Decimal("0.0001")

        levels = _calculate_grid_price_levels(base_price, Side.BUY, config, tick)

        assert len(levels) == 3
        # Each level should still be valid
        for price in levels:
            assert price > Decimal("0")
            assert price % tick == Decimal("0")


# =============================================================================
# REBALANCE HELPER TESTS
# =============================================================================


class TestCalculateRebalanceTarget:
    """Tests for _calculate_rebalance_target helper."""

    def _make_mock_trade(
        self,
        leg1_qty: Decimal = Decimal("10"),
        leg1_entry: Decimal = Decimal("100"),
        leg1_side: Side = Side.BUY,
        leg2_qty: Decimal = Decimal("10"),
        leg2_entry: Decimal = Decimal("100"),
        leg2_side: Side = Side.SELL,
    ) -> MagicMock:
        """Create mock Trade with legs."""
        trade = MagicMock()

        # Leg 1 (Lighter)
        trade.leg1 = MagicMock(spec=TradeLeg)
        trade.leg1.filled_qty = leg1_qty
        trade.leg1.entry_price = leg1_entry
        trade.leg1.side = leg1_side

        # Leg 2 (X10)
        trade.leg2 = MagicMock(spec=TradeLeg)
        trade.leg2.filled_qty = leg2_qty
        trade.leg2.entry_price = leg2_entry
        trade.leg2.side = leg2_side

        return trade

    def test_balanced_position_zero_delta(self):
        """Balanced position should have minimal net delta."""
        trade = self._make_mock_trade(
            leg1_qty=Decimal("10"),
            leg1_entry=Decimal("100"),
            leg1_side=Side.BUY,
            leg2_qty=Decimal("10"),
            leg2_entry=Decimal("100"),
            leg2_side=Side.SELL,
        )

        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade, None, None
        )

        # Net delta: BUY 10*100 = +1000, SELL 10*100 = -1000, net = 0
        assert net_delta == Decimal("0")
        assert rebalance_notional == Decimal("0")

    def test_long_leg_larger_positive_delta(self):
        """Larger long leg creates positive delta, should reduce leg1."""
        trade = self._make_mock_trade(
            leg1_qty=Decimal("12"),  # Larger
            leg1_entry=Decimal("100"),
            leg1_side=Side.BUY,
            leg2_qty=Decimal("10"),
            leg2_entry=Decimal("100"),
            leg2_side=Side.SELL,
        )

        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade, None, None
        )

        # Net delta: BUY 12*100 = +1200, SELL 10*100 = -1000, net = +200
        assert net_delta == Decimal("200")
        assert rebalance_notional == Decimal("200")
        assert exchange == Exchange.LIGHTER
        assert leg == trade.leg1

    def test_short_leg_larger_negative_delta(self):
        """Larger short leg creates negative delta, should reduce leg2."""
        trade = self._make_mock_trade(
            leg1_qty=Decimal("10"),
            leg1_entry=Decimal("100"),
            leg1_side=Side.BUY,
            leg2_qty=Decimal("12"),  # Larger
            leg2_entry=Decimal("100"),
            leg2_side=Side.SELL,
        )

        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade, None, None
        )

        # Net delta: BUY 10*100 = +1000, SELL 12*100 = -1200, net = -200
        assert net_delta == Decimal("-200")
        assert rebalance_notional == Decimal("200")
        assert exchange == Exchange.X10
        assert leg == trade.leg2

    def test_mark_price_overrides_entry(self):
        """Mark price should be used instead of entry price when provided."""
        trade = self._make_mock_trade(
            leg1_qty=Decimal("10"),
            leg1_entry=Decimal("100"),
            leg1_side=Side.BUY,
            leg2_qty=Decimal("10"),
            leg2_entry=Decimal("100"),
            leg2_side=Side.SELL,
        )

        # Mark prices differ from entry
        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade,
            leg1_mark_price=Decimal("110"),  # Leg1 now worth more
            leg2_mark_price=Decimal("100"),
        )

        # Net delta: BUY 10*110 = +1100, SELL 10*100 = -1000, net = +100
        assert net_delta == Decimal("100")
        assert rebalance_notional == Decimal("100")

    def test_zero_mark_price_uses_entry(self):
        """Zero or None mark price should fall back to entry price."""
        trade = self._make_mock_trade()

        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade,
            leg1_mark_price=Decimal("0"),  # Invalid
            leg2_mark_price=None,  # Missing
        )

        # Should use entry prices, resulting in balanced delta
        assert net_delta == Decimal("0")

    def test_inverted_position_short_leg1(self):
        """Test with inverted position (short leg1, long leg2)."""
        trade = self._make_mock_trade(
            leg1_qty=Decimal("10"),
            leg1_side=Side.SELL,  # Short on Lighter
            leg2_qty=Decimal("12"),
            leg2_side=Side.BUY,  # Long on X10
        )

        exchange, leg, rebalance_notional, net_delta = _calculate_rebalance_target(
            trade, None, None
        )

        # Net delta: SELL 10*100 = -1000, BUY 12*100 = +1200, net = +200
        assert net_delta == Decimal("200")
        # leg2 is the long (positive) side, leg1 is short (negative)
        # To reduce positive delta, need to reduce the long side


# =============================================================================
# LEG UPDATE HELPER TESTS
# =============================================================================


class TestUpdateLegAfterFill:
    """Tests for _update_leg_after_fill helper."""

    def _make_mock_leg(
        self,
        filled_qty: Decimal = Decimal("10"),
        fees: Decimal = Decimal("0.5"),
    ) -> MagicMock:
        """Create mock TradeLeg."""
        leg = MagicMock(spec=TradeLeg)
        leg.filled_qty = filled_qty
        leg.fees = fees
        return leg

    def test_reduces_filled_qty(self):
        """Fill should reduce remaining quantity."""
        leg = self._make_mock_leg(filled_qty=Decimal("10"))

        _update_leg_after_fill(leg, filled_qty=Decimal("3"), fee=Decimal("0.1"))

        assert leg.filled_qty == Decimal("7")

    def test_accumulates_fees(self):
        """Fees should accumulate."""
        leg = self._make_mock_leg(filled_qty=Decimal("10"), fees=Decimal("0.5"))

        _update_leg_after_fill(leg, filled_qty=Decimal("3"), fee=Decimal("0.2"))

        assert leg.fees == Decimal("0.7")

    def test_qty_cannot_go_negative(self):
        """Filled qty should not go below zero."""
        leg = self._make_mock_leg(filled_qty=Decimal("5"))

        # Try to fill more than available
        _update_leg_after_fill(leg, filled_qty=Decimal("10"), fee=Decimal("0"))

        assert leg.filled_qty == Decimal("0")

    def test_zero_fill_no_change(self):
        """Zero fill should not change quantity."""
        leg = self._make_mock_leg(filled_qty=Decimal("10"), fees=Decimal("0.5"))

        _update_leg_after_fill(leg, filled_qty=Decimal("0"), fee=Decimal("0"))

        assert leg.filled_qty == Decimal("10")
        assert leg.fees == Decimal("0.5")

    def test_full_fill(self):
        """Full fill should reduce qty to zero."""
        leg = self._make_mock_leg(filled_qty=Decimal("10"))

        _update_leg_after_fill(leg, filled_qty=Decimal("10"), fee=Decimal("0.5"))

        assert leg.filled_qty == Decimal("0")
        assert leg.fees > Decimal("0")

    def test_small_decimal_precision(self):
        """Should handle small decimal values correctly."""
        leg = self._make_mock_leg(filled_qty=Decimal("0.00001"), fees=Decimal("0"))

        _update_leg_after_fill(
            leg,
            filled_qty=Decimal("0.000005"),
            fee=Decimal("0.00000001"),
        )

        assert leg.filled_qty == Decimal("0.000005")
        assert leg.fees == Decimal("0.00000001")
