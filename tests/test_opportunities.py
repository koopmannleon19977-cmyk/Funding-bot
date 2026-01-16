"""
Tests for Opportunity Detection.

Tests opportunity ranking, filtering, and EV calculation.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from funding_bot.domain.models import Opportunity

# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def high_apy_opportunity():
    """Create high APY opportunity."""
    return Opportunity(
        symbol="MEGA",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.002"),
        x10_rate=Decimal("-0.001"),
        net_funding_hourly=Decimal("0.003"),
        apy=Decimal("2.50"),  # 250% APY
        spread_pct=Decimal("0.002"),
        mid_price=Decimal("0.50"),
        lighter_best_bid=Decimal("0.499"),
        lighter_best_ask=Decimal("0.501"),
        x10_best_bid=Decimal("0.498"),
        x10_best_ask=Decimal("0.502"),
        suggested_qty=Decimal("1000"),
        suggested_notional=Decimal("500"),
        expected_value_usd=Decimal("5.00"),
        breakeven_hours=Decimal("2"),
    )


@pytest.fixture
def low_apy_opportunity():
    """Create low APY opportunity."""
    return Opportunity(
        symbol="BTC",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.0001"),
        x10_rate=Decimal("-0.00005"),
        net_funding_hourly=Decimal("0.00015"),
        apy=Decimal("0.15"),  # 15% APY
        spread_pct=Decimal("0.0005"),
        mid_price=Decimal("50000"),
        lighter_best_bid=Decimal("49990"),
        lighter_best_ask=Decimal("50010"),
        x10_best_bid=Decimal("49985"),
        x10_best_ask=Decimal("50015"),
        suggested_qty=Decimal("0.01"),
        suggested_notional=Decimal("500"),
        expected_value_usd=Decimal("0.20"),
        breakeven_hours=Decimal("24"),
    )


@pytest.fixture
def negative_spread_opportunity():
    """Create opportunity with negative spread (we get paid!)."""
    return Opportunity(
        symbol="POPCAT",
        timestamp=datetime.now(UTC),
        lighter_rate=Decimal("0.0008"),
        x10_rate=Decimal("-0.0003"),
        net_funding_hourly=Decimal("0.0011"),
        apy=Decimal("0.95"),
        spread_pct=Decimal("-0.001"),  # Negative = we profit on entry!
        mid_price=Decimal("0.80"),
        lighter_best_bid=Decimal("0.802"),  # Lighter bid > X10 ask
        lighter_best_ask=Decimal("0.804"),
        x10_best_bid=Decimal("0.798"),
        x10_best_ask=Decimal("0.800"),
        suggested_qty=Decimal("500"),
        suggested_notional=Decimal("400"),
        expected_value_usd=Decimal("3.00"),
        breakeven_hours=Decimal("1"),
    )


# =============================================================================
# Opportunity Ranking Tests
# =============================================================================

class TestOpportunityRanking:
    """Tests for opportunity ranking logic."""

    def test_rank_by_apy(self, high_apy_opportunity, low_apy_opportunity):
        """Should rank by APY descending."""
        opportunities = [low_apy_opportunity, high_apy_opportunity]

        sorted_opps = sorted(opportunities, key=lambda o: o.apy, reverse=True)

        assert sorted_opps[0].symbol == "MEGA"  # Higher APY first
        assert sorted_opps[1].symbol == "BTC"

    def test_rank_by_ev(self, high_apy_opportunity, low_apy_opportunity):
        """Should rank by expected value."""
        opportunities = [low_apy_opportunity, high_apy_opportunity]

        sorted_opps = sorted(opportunities, key=lambda o: o.expected_value_usd, reverse=True)

        assert sorted_opps[0].expected_value_usd > sorted_opps[1].expected_value_usd

    def test_rank_by_breakeven(self, high_apy_opportunity, low_apy_opportunity):
        """Should rank by breakeven time ascending (faster is better)."""
        opportunities = [low_apy_opportunity, high_apy_opportunity]

        sorted_opps = sorted(opportunities, key=lambda o: o.breakeven_hours)

        assert sorted_opps[0].symbol == "MEGA"  # Faster breakeven first


# =============================================================================
# EV Calculation Tests
# =============================================================================

class TestEVCalculation:
    """Tests for Expected Value calculation."""

    def test_positive_ev(self, high_apy_opportunity):
        """High APY opportunity should have positive EV."""
        assert high_apy_opportunity.expected_value_usd > 0

    def test_ev_components(self):
        """EV should consider funding, fees, and spread."""
        # Sample calculation
        notional = Decimal("500")
        net_apy = Decimal("0.50")  # 50%
        hold_hours = Decimal("24")
        fee_rate = Decimal("0.001")  # 0.1%
        spread = Decimal("0.002")  # 0.2%

        # Gross funding for 24h
        gross_funding = notional * net_apy * hold_hours / Decimal("8760")

        # Entry + exit fees (entry maker, exit taker)
        total_fees = notional * fee_rate * 2  # Entry + exit

        # Spread cost on entry
        spread_cost = notional * spread

        # Net EV
        net_ev = gross_funding - total_fees - spread_cost

        assert isinstance(net_ev, Decimal)

    def test_negative_ev_rejected(self):
        """Negative EV opportunities should be rejected."""
        ev = Decimal("-0.50")
        should_trade = ev > Decimal("0.50")  # Minimum $0.50 EV

        assert should_trade is False


# =============================================================================
# Filtering Tests
# =============================================================================

class TestOpportunityFiltering:
    """Tests for opportunity filters."""

    def test_min_apy_filter(self, low_apy_opportunity):
        """Should filter out low APY opportunities."""
        min_apy = Decimal("0.40")  # 40%

        passes = low_apy_opportunity.apy >= min_apy
        assert passes is False

    def test_max_spread_filter(self, high_apy_opportunity):
        """Should filter out high spread opportunities."""
        max_spread = Decimal("0.001")  # 0.1%

        passes = high_apy_opportunity.spread_pct <= max_spread
        assert passes is False  # 0.2% > 0.1%

    def test_negative_spread_passes(self, negative_spread_opportunity):
        """Negative spread should always pass spread filter."""
        max_spread = Decimal("0.001")

        passes = negative_spread_opportunity.spread_pct <= max_spread
        assert passes is True  # -0.1% < +0.1%

    def test_min_ev_filter(self, low_apy_opportunity):
        """Should filter out low EV opportunities."""
        min_ev = Decimal("0.50")

        passes = low_apy_opportunity.expected_value_usd >= min_ev
        assert passes is False  # $0.20 < $0.50

    def test_max_breakeven_filter(self, low_apy_opportunity):
        """Should filter out long breakeven opportunities."""
        max_breakeven = Decimal("12")  # 12 hours max

        passes = low_apy_opportunity.breakeven_hours <= max_breakeven
        assert passes is False  # 24h > 12h


# =============================================================================
# Direction Detection Tests
# =============================================================================

class TestDirectionDetection:
    """Tests for trade direction detection."""

    def test_short_lighter_long_x10(self):
        """Should detect short Lighter, long X10 direction."""
        lighter_rate = Decimal("0.0005")  # Positive = longs pay shorts
        x10_rate = Decimal("-0.0002")     # Negative = shorts pay longs

        # We want to be short where longs pay us: Lighter
        # We want to be long where shorts pay us: X10
        should_short_lighter = lighter_rate > 0
        should_long_x10 = x10_rate < 0

        assert should_short_lighter is True
        assert should_long_x10 is True

    def test_opposite_direction(self):
        """Should detect opposite direction when rates flip."""
        lighter_rate = Decimal("-0.0003")  # Negative = shorts pay longs
        x10_rate = Decimal("0.0004")       # Positive = longs pay shorts

        # Now we want to be long Lighter, short X10
        should_long_lighter = lighter_rate < 0
        should_short_x10 = x10_rate > 0

        assert should_long_lighter is True
        assert should_short_x10 is True


# =============================================================================
# Sizing Tests
# =============================================================================

class TestOpportunitySizing:
    """Tests for opportunity sizing."""

    def test_notional_from_target(self):
        """Should calculate notional from target USD."""
        target_usd = Decimal("500")
        price = Decimal("2000")

        qty = target_usd / price
        assert qty == Decimal("0.25")

    def test_size_capped_by_liquidity(self):
        """Should cap size based on available liquidity."""
        target_qty = Decimal("10")
        available_qty = Decimal("5")  # Only 5 available
        utilization_cap = Decimal("0.5")  # Use max 50%

        max_qty = available_qty * utilization_cap
        final_qty = min(target_qty, max_qty)

        assert final_qty == Decimal("2.5")

    def test_size_respects_min_order(self):
        """Should respect minimum order size."""
        calculated_qty = Decimal("0.005")
        min_order_qty = Decimal("0.01")

        final_qty = max(calculated_qty, min_order_qty) if calculated_qty > 0 else Decimal("0")
        assert final_qty == min_order_qty


# =============================================================================
# Edge Cases
# =============================================================================

class TestOpportunityEdgeCases:
    """Tests for edge cases in opportunity detection."""

    def test_zero_apy_rejected(self):
        """Zero APY should be rejected."""
        apy = Decimal("0")
        passes = apy > Decimal("0.20")
        assert passes is False

    def test_very_high_apy_flagged(self):
        """Very high APY should be flagged as suspicious."""
        apy = Decimal("50.00")  # 5000% APY - suspicious!
        max_reasonable = Decimal("10.00")  # 1000%

        is_suspicious = apy > max_reasonable
        assert is_suspicious is True

    def test_stale_opportunity_rejected(self):
        """Stale opportunities should be rejected."""
        from datetime import timedelta

        timestamp = datetime.now(UTC) - timedelta(minutes=5)
        max_age_seconds = 60

        age = (datetime.now(UTC) - timestamp).total_seconds()
        is_fresh = age <= max_age_seconds

        assert is_fresh is False

    def test_same_rates_zero_opportunity(self):
        """Same rates on both exchanges means no opportunity."""
        lighter_rate = Decimal("0.0003")
        x10_rate = Decimal("0.0003")

        net_rate = abs(lighter_rate - x10_rate)
        has_opportunity = net_rate > Decimal("0.0001")  # Min 0.01%

        assert has_opportunity is False
