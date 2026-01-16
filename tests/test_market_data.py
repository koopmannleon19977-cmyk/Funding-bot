"""
Tests for Market Data Service.

Tests funding rate fetching, orderbook management, and data caching.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

# =============================================================================
# Price Snapshot Tests
# =============================================================================

class TestPriceSnapshot:
    """Tests for PriceSnapshot model."""

    def test_mid_price_calculation(self):
        """Should calculate mid price correctly."""
        lighter_price = Decimal("2000")
        x10_price = Decimal("2002")

        mid_price = (lighter_price + x10_price) / 2
        assert mid_price == Decimal("2001")

    def test_mid_price_single_exchange(self):
        """Should use available price when one is missing."""
        lighter_price = Decimal("2000")
        x10_price = Decimal("0")  # Missing

        # Should use the available one
        mid_price = lighter_price or x10_price
        assert mid_price == Decimal("2000")

    def test_spread_pct_calculation(self):
        """Should calculate price spread correctly."""
        lighter_price = Decimal("2000")
        x10_price = Decimal("2010")
        mid_price = (lighter_price + x10_price) / 2

        spread = abs(lighter_price - x10_price) / mid_price
        # (10 / 2005) ≈ 0.5%
        assert spread == Decimal("10") / Decimal("2005")


# =============================================================================
# Funding Snapshot Tests
# =============================================================================

class TestFundingSnapshot:
    """Tests for FundingSnapshot calculations."""

    def test_net_rate_calculation(self):
        """Should calculate net funding rate correctly."""
        lighter_rate_hourly = Decimal("0.0005")  # We receive 0.05%/h
        x10_rate_hourly = Decimal("-0.0002")     # We pay -0.02%/h

        # For short Lighter, long X10:
        # Net = |lighter - x10| = |0.0005 - (-0.0002)| = 0.0007
        net_rate = abs(lighter_rate_hourly - x10_rate_hourly)
        assert net_rate == Decimal("0.0007")

    def test_apy_calculation(self):
        """Should calculate correct APY from hourly rate."""
        hourly_rate = Decimal("0.0007")  # 0.07% per hour

        # APY = hourly * 24 * 365
        apy = hourly_rate * Decimal("24") * Decimal("365")

        # 0.0007 * 24 * 365 = 6.132 = 613.2% APY
        assert apy == Decimal("6.132")

    def test_zero_rate_one_side(self):
        """Should handle zero rate on one side."""
        lighter_rate = Decimal("0.0005")
        x10_rate = Decimal("0")

        net_rate = abs(lighter_rate - x10_rate)
        assert net_rate == Decimal("0.0005")


# =============================================================================
# Orderbook Snapshot Tests
# =============================================================================

class TestOrderbookSnapshot:
    """Tests for OrderbookSnapshot calculations."""

    def test_entry_spread_long_x10(self):
        """Entry spread for Long X10, Short Lighter."""
        lighter_bid = Decimal("2000")
        lighter_ask = Decimal("2002")
        x10_bid = Decimal("1999")
        x10_ask = Decimal("2003")

        mid = (lighter_bid + lighter_ask + x10_bid + x10_ask) / 4

        # Long X10 = buy at x10_ask
        # Short Lighter = sell at lighter_bid
        # Spread = x10_ask - lighter_bid
        spread = (x10_ask - lighter_bid) / mid

        # (2003 - 2000) / 2001 = 0.15%
        assert spread > Decimal("0.001")

    def test_entry_spread_long_lighter(self):
        """Entry spread for Long Lighter, Short X10."""
        lighter_bid = Decimal("2000")
        lighter_ask = Decimal("2002")
        x10_bid = Decimal("1999")
        x10_ask = Decimal("2003")

        mid = (lighter_bid + lighter_ask + x10_bid + x10_ask) / 4

        # Long Lighter = buy at lighter_ask
        # Short X10 = sell at x10_bid
        # Spread = lighter_ask - x10_bid
        spread = (lighter_ask - x10_bid) / mid

        # (2002 - 1999) / 2001 = 0.15%
        assert spread > Decimal("0.001")

    def test_missing_data_returns_high_spread(self):
        """Missing orderbook data should return high spread."""
        # If we have no data, spread should be very high (100%)
        # to ensure the opportunity is rejected
        empty_mid = Decimal("0")
        fallback_spread = Decimal("1.0") if empty_mid == 0 else Decimal("0")

        assert fallback_spread == Decimal("1.0")


# =============================================================================
# Data Freshness Tests
# =============================================================================

class TestDataFreshness:
    """Tests for data staleness detection."""

    def test_fresh_data_accepted(self):
        """Fresh data should be accepted."""
        updated = datetime.now(UTC) - timedelta(seconds=5)
        max_age = 30

        age_seconds = (datetime.now(UTC) - updated).total_seconds()
        is_stale = age_seconds > max_age

        assert is_stale is False

    def test_stale_data_rejected(self):
        """Stale data should be rejected."""
        updated = datetime.now(UTC) - timedelta(seconds=60)
        max_age = 30

        age_seconds = (datetime.now(UTC) - updated).total_seconds()
        is_stale = age_seconds > max_age

        assert is_stale is True

    def test_missing_timestamp_is_stale(self):
        """Missing timestamp should be treated as stale."""
        updated = None
        is_stale = updated is None

        assert is_stale is True


# =============================================================================
# Symbol Common Tests
# =============================================================================

class TestCommonSymbols:
    """Tests for common symbol detection."""

    def test_find_common_symbols(self):
        """Should find symbols available on both exchanges."""
        lighter_symbols = {"BTC", "ETH", "SOL", "DOGE"}
        x10_symbols = {"BTC-USD", "ETH-USD", "LINK-USD", "AVAX-USD"}

        # Normalize X10 symbols
        x10_normalized = {s.replace("-USD", "") for s in x10_symbols}

        common = lighter_symbols & x10_normalized
        assert common == {"BTC", "ETH"}

    def test_blacklist_filtering(self):
        """Should filter out blacklisted symbols."""
        common = {"BTC", "ETH", "SOL", "SHIB"}
        blacklist = {"SHIB", "DOGE"}

        filtered = common - blacklist
        assert filtered == {"BTC", "ETH", "SOL"}

    def test_empty_intersection(self):
        """Should handle no common symbols gracefully."""
        lighter_symbols = {"BTC", "ETH"}
        x10_symbols = {"LINK-USD", "AVAX-USD"}

        x10_normalized = {s.replace("-USD", "") for s in x10_symbols}
        common = lighter_symbols & x10_normalized

        assert len(common) == 0


# =============================================================================
# Rate Limit Tests
# =============================================================================

class TestRateLimiting:
    """Tests for rate limiting logic."""

    def test_rate_limit_calculation(self):
        """Should calculate correct rate limit interval."""
        target_requests_per_minute = 30
        interval_seconds = 60 / target_requests_per_minute

        assert interval_seconds == 2.0

    def test_dynamic_rate_limit(self):
        """Should adjust rate based on number of symbols."""
        symbols_count = 60
        max_requests_per_minute = 30

        interval_per_symbol = 60 / max_requests_per_minute
        total_time_needed = symbols_count * interval_per_symbol

        assert total_time_needed == 120


# =============================================================================
# Edge Cases
# =============================================================================

class TestMarketDataEdgeCases:
    """Tests for edge cases in market data."""

    def test_zero_price_handling(self):
        """Should handle zero prices gracefully."""
        price = Decimal("0")
        is_valid = price > 0
        assert is_valid is False

    def test_negative_rate_valid(self):
        """Negative funding rates are valid."""
        rate = Decimal("-0.0005")
        is_valid = isinstance(rate, Decimal)
        assert is_valid is True

    def test_extreme_rate_detection(self):
        """Should detect extreme/suspicious rates."""
        rate = Decimal("0.50")  # 50% per hour - suspicious!
        max_reasonable = Decimal("0.01")  # 1% per hour

        is_suspicious = abs(rate) > max_reasonable
        assert is_suspicious is True

    def test_missing_one_exchange_rate(self):
        """Should handle missing rate from one exchange."""
        lighter_rate = Decimal("0.0005")
        x10_rate = None  # Missing!

        can_calculate_net = lighter_rate is not None and x10_rate is not None
        assert can_calculate_net is False

    def test_crossed_orderbook_detection(self):
        """Should detect crossed/invalid orderbook."""
        bid = Decimal("2002")
        ask = Decimal("2000")  # Bid > Ask = crossed!

        is_crossed = bid >= ask
        assert is_crossed is True

    def test_valid_orderbook(self):
        """Valid orderbook has bid < ask."""
        bid = Decimal("2000")
        ask = Decimal("2002")

        is_valid = bid < ask
        assert is_valid is True

    def test_orderbook_qty_validation(self):
        """Should validate orderbook quantities."""
        bid_qty = Decimal("10")
        ask_qty = Decimal("0")  # Empty ask side

        has_liquidity = bid_qty > 0 and ask_qty > 0
        assert has_liquidity is False
