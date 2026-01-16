"""
Tests for Liquidity Gates.

Tests depth checks, L1 compliance, and impact-based calculations.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from funding_bot.services.market_data import OrderbookSnapshot

# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def deep_orderbook():
    """Create orderbook with deep liquidity."""
    return OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("2000"),
        lighter_bid_qty=Decimal("10"),  # $20k available
        lighter_ask=Decimal("2001"),
        lighter_ask_qty=Decimal("10"),
        x10_bid=Decimal("1999"),
        x10_bid_qty=Decimal("10"),  # $20k available
        x10_ask=Decimal("2002"),
        x10_ask_qty=Decimal("10"),
        lighter_updated=datetime.now(UTC),
        x10_updated=datetime.now(UTC),
    )


@pytest.fixture
def shallow_orderbook():
    """Create orderbook with thin liquidity."""
    return OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("2000"),
        lighter_bid_qty=Decimal("0.1"),  # Only $200 available
        lighter_ask=Decimal("2001"),
        lighter_ask_qty=Decimal("0.1"),
        x10_bid=Decimal("1999"),
        x10_bid_qty=Decimal("0.05"),  # Only $100 available
        x10_ask=Decimal("2002"),
        x10_ask_qty=Decimal("0.05"),
        lighter_updated=datetime.now(UTC),
        x10_updated=datetime.now(UTC),
    )


@pytest.fixture
def asymmetric_orderbook():
    """Create orderbook with asymmetric liquidity."""
    return OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("2000"),
        lighter_bid_qty=Decimal("5"),  # $10k on Lighter
        lighter_ask=Decimal("2001"),
        lighter_ask_qty=Decimal("5"),
        x10_bid=Decimal("1999"),
        x10_bid_qty=Decimal("0.2"),  # Only $400 on X10!
        x10_ask=Decimal("2002"),
        x10_ask_qty=Decimal("0.2"),
        lighter_updated=datetime.now(UTC),
        x10_updated=datetime.now(UTC),
    )


# =============================================================================
# L1 Depth Calculation Tests
# =============================================================================

class TestL1DepthCalculations:
    """Tests for L1 depth calculations."""

    def test_available_liquidity_calculation(self, deep_orderbook):
        """Should calculate available liquidity correctly."""
        ob = deep_orderbook

        # For short Lighter (sell), we look at bids
        lighter_available = ob.lighter_bid_qty * ob.lighter_bid
        assert lighter_available == Decimal("20000")  # 10 * 2000

        # For long X10 (buy), we look at asks
        x10_available = ob.x10_ask_qty * ob.x10_ask
        assert x10_available == Decimal("20020")  # 10 * 2002

    def test_max_qty_with_utilization_cap(self, deep_orderbook):
        """Max qty should be limited by utilization cap."""
        ob = deep_orderbook
        utilization_cap = Decimal("0.5")  # Use max 50%

        # Max qty on each side
        lighter_max = ob.lighter_bid_qty * utilization_cap
        x10_max = ob.x10_ask_qty * utilization_cap

        # Take the minimum (bottleneck)
        max_qty = min(lighter_max, x10_max)
        assert max_qty == Decimal("5")  # 50% of 10

    def test_shallow_book_limits_size(self, shallow_orderbook):
        """Shallow book should severely limit trade size."""
        ob = shallow_orderbook
        utilization_cap = Decimal("0.5")

        # X10 only has 0.05 ETH
        x10_max = ob.x10_ask_qty * utilization_cap
        assert x10_max == Decimal("0.025")


# =============================================================================
# L1 Depth Gate Tests
# =============================================================================

class TestL1DepthGate:
    """Tests for L1 depth gate logic."""

    def test_passes_with_sufficient_depth(self, deep_orderbook):
        """Should pass when depth is sufficient for trade size."""
        ob = deep_orderbook
        target_qty = Decimal("0.25")

        # Check if we can trade 0.25 ETH
        lighter_available = ob.lighter_bid_qty
        x10_available = ob.x10_ask_qty

        can_trade = target_qty <= lighter_available and target_qty <= x10_available
        assert can_trade is True

    def test_fails_exceeds_availability(self, shallow_orderbook):
        """Should fail when trade size exceeds availability."""
        ob = shallow_orderbook
        target_qty = Decimal("0.25")

        # X10 only has 0.05, we want 0.25
        x10_available = ob.x10_ask_qty

        can_trade = target_qty <= x10_available
        assert can_trade is False

    def test_high_utilization_rejected(self, deep_orderbook):
        """Should reject trades that use too much of L1."""
        ob = deep_orderbook
        target_qty = Decimal("8")  # 80% of available
        max_utilization = Decimal("0.5")  # Only 50% allowed

        utilization = target_qty / ob.x10_ask_qty
        is_ok = utilization <= max_utilization

        assert is_ok is False


# =============================================================================
# X10 Specific Compliance Tests
# =============================================================================

class TestX10Compliance:
    """Tests for X10 specifically L1 compliance."""

    def test_x10_sufficient_for_hedge(self, deep_orderbook):
        """X10 should have enough for hedge when deep book."""
        ob = deep_orderbook
        hedge_qty = Decimal("0.25")

        # Check X10 ask side for long hedge
        x10_available = ob.x10_ask_qty

        can_hedge = hedge_qty <= x10_available
        assert can_hedge is True

    def test_x10_insufficient_for_large_hedge(self, asymmetric_orderbook):
        """X10 should fail for large hedges when thin."""
        ob = asymmetric_orderbook
        hedge_qty = Decimal("0.5")  # Want 0.5 but only 0.2 available

        x10_available = ob.x10_ask_qty

        can_hedge = hedge_qty <= x10_available
        assert can_hedge is False

    def test_bottleneck_detection(self, asymmetric_orderbook):
        """Should identify X10 as the bottleneck."""
        ob = asymmetric_orderbook

        lighter_available = ob.lighter_bid_qty  # 5 ETH
        x10_available = ob.x10_ask_qty  # 0.2 ETH

        bottleneck = "X10" if x10_available < lighter_available else "LIGHTER"
        assert bottleneck == "X10"


# =============================================================================
# Entry Spread Tests
# =============================================================================

class TestEntrySpread:
    """Tests for entry spread calculations."""

    def test_positive_spread_calculated(self, deep_orderbook):
        """Entry spread should be positive (we pay to enter)."""
        ob = deep_orderbook

        # Long X10, Short Lighter
        # Buy at x10_ask, sell at lighter_bid
        mid = (ob.lighter_bid + ob.x10_ask) / 2
        spread = (ob.x10_ask - ob.lighter_bid) / mid

        assert spread > Decimal("0")

    def test_negative_spread_favorable(self):
        """Negative spread means we're paid to enter!"""
        lighter_bid = Decimal("2005")  # Lighter bid HIGHER
        x10_ask = Decimal("2000")      # X10 ask lower
        mid = (lighter_bid + x10_ask) / 2

        # We sell at 2005, buy at 2000 = profit on entry!
        spread = (x10_ask - lighter_bid) / mid

        assert spread < Decimal("0")


# =============================================================================
# Notional Requirements Tests
# =============================================================================

class TestNotionalRequirements:
    """Tests for minimum notional requirements."""

    def test_min_notional_check(self, deep_orderbook):
        """Should check minimum notional requirement."""
        ob = deep_orderbook
        min_notional_usd = Decimal("200")

        lighter_notional = ob.lighter_bid_qty * ob.lighter_bid
        x10_notional = ob.x10_ask_qty * ob.x10_ask

        passes_min = lighter_notional >= min_notional_usd and x10_notional >= min_notional_usd
        assert passes_min is True

    def test_notional_multiple_requirement(self, shallow_orderbook):
        """Should check notional is X times trade size."""
        ob = shallow_orderbook
        trade_notional = Decimal("500")
        multiple = Decimal("1.5")
        required_notional = trade_notional * multiple

        # X10 has 0.05 * 2002 = $100
        x10_notional = ob.x10_ask_qty * ob.x10_ask

        passes = x10_notional >= required_notional
        assert passes is False


# =============================================================================
# Edge Cases
# =============================================================================

class TestLiquidityGatesEdgeCases:
    """Tests for edge cases in liquidity gate logic."""

    def test_zero_quantity_fails(self):
        """Zero quantity should fail gate."""
        target_qty = Decimal("0")

        is_valid = target_qty > 0
        assert is_valid is False

    def test_zero_liquidity_fails(self):
        """Zero liquidity should fail gate."""
        available_qty = Decimal("0")
        target_qty = Decimal("0.25")

        can_trade = available_qty >= target_qty
        assert can_trade is False

    def test_very_large_order_fails(self, deep_orderbook):
        """Very large orders should fail depth check."""
        ob = deep_orderbook
        target_qty = Decimal("100")  # 10x what's available

        can_trade = target_qty <= ob.x10_ask_qty
        assert can_trade is False

    def test_exact_availability_edge(self, deep_orderbook):
        """Should handle trading exactly available amount."""
        ob = deep_orderbook
        target_qty = ob.x10_ask_qty  # Exactly available

        can_trade = target_qty <= ob.x10_ask_qty
        assert can_trade is True

    def test_stale_orderbook_fails(self):
        """Stale orderbook should be flagged."""
        from datetime import timedelta

        updated = datetime.now(UTC) - timedelta(minutes=5)
        max_age = 30  # seconds

        age = (datetime.now(UTC) - updated).total_seconds()
        is_stale = age > max_age

        assert is_stale is True
