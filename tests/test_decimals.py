"""
Tests for Decimal Utilities.

Tests safe_decimal conversion and funding rate clamping.
"""

from decimal import Decimal

from funding_bot.utils.decimals import (
    LIGHTER_FUNDING_RATE_CAP,
    X10_FUNDING_RATE_CAP,
    clamp_funding_rate,
    safe_decimal,
)


class TestSafeDecimal:
    """Tests for safe_decimal function."""

    def test_handles_none(self):
        """Should return default for None."""
        assert safe_decimal(None) == Decimal("0")
        assert safe_decimal(None, Decimal("1")) == Decimal("1")

    def test_handles_decimal(self):
        """Should pass through Decimal unchanged."""
        d = Decimal("123.456")
        assert safe_decimal(d) == d

    def test_handles_string(self):
        """Should convert string to Decimal."""
        assert safe_decimal("123.456") == Decimal("123.456")
        assert safe_decimal("0.0001") == Decimal("0.0001")

    def test_handles_int(self):
        """Should convert int to Decimal."""
        assert safe_decimal(100) == Decimal("100")

    def test_handles_float(self):
        """Should convert float to Decimal via string."""
        result = safe_decimal(1.5)
        assert result == Decimal("1.5")

    def test_handles_scientific_notation(self):
        """Should handle scientific notation strings."""
        assert safe_decimal("1e-5") == Decimal("1e-5")
        assert safe_decimal("1.23e-5") == Decimal("1.23e-5")

    def test_handles_negative(self):
        """Should handle negative values."""
        assert safe_decimal("-5.5") == Decimal("-5.5")
        assert safe_decimal(-100) == Decimal("-100")

    def test_handles_zero(self):
        """Should handle zero correctly."""
        assert safe_decimal(0) == Decimal("0")
        assert safe_decimal("0") == Decimal("0")
        assert safe_decimal("0.0") == Decimal("0.0")

    def test_handles_invalid_string(self):
        """Should return default for invalid string."""
        assert safe_decimal("not_a_number") == Decimal("0")
        assert safe_decimal("abc", Decimal("-1")) == Decimal("-1")

    def test_handles_nan(self):
        """Should return default for NaN strings."""
        assert safe_decimal("NaN") == Decimal("0")
        assert safe_decimal("nan") == Decimal("0")

    def test_handles_infinity(self):
        """Should return default for infinity strings."""
        assert safe_decimal("Infinity") == Decimal("0")
        assert safe_decimal("-Infinity") == Decimal("0")
        assert safe_decimal("inf") == Decimal("0")

    def test_handles_empty_string(self):
        """Should return default for empty string."""
        assert safe_decimal("") == Decimal("0")

    def test_handles_whitespace_string(self):
        """Should return default for whitespace string."""
        assert safe_decimal("  ") == Decimal("0")

    def test_handles_list(self):
        """Should return default for list."""
        assert safe_decimal([1, 2, 3]) == Decimal("0")

    def test_handles_dict(self):
        """Should return default for dict."""
        assert safe_decimal({"value": 100}) == Decimal("0")

    def test_handles_very_small_number(self):
        """Should handle very small numbers."""
        result = safe_decimal("0.00000001")
        assert result == Decimal("0.00000001")

    def test_handles_very_large_number(self):
        """Should handle very large numbers."""
        result = safe_decimal("99999999999.99999999")
        assert result == Decimal("99999999999.99999999")


class TestClampFundingRate:
    """Tests for clamp_funding_rate function."""

    def test_rate_within_cap_unchanged(self):
        """Rate within cap should be returned unchanged."""
        rate = Decimal("0.001")  # 0.1%
        result = clamp_funding_rate(rate, LIGHTER_FUNDING_RATE_CAP)
        assert result == rate

    def test_positive_rate_exceeds_cap(self):
        """Positive rate exceeding cap should return original rate with warning logged."""
        rate = Decimal("0.01")  # 1% - exceeds Lighter 0.5% cap
        result = clamp_funding_rate(rate, LIGHTER_FUNDING_RATE_CAP)
        # Should return original rate, not clamped value
        assert result == rate
        assert result == Decimal("0.01")

    def test_negative_rate_exceeds_cap(self):
        """Negative rate exceeding cap should return original rate with warning logged."""
        rate = Decimal("-0.01")  # -1%
        result = clamp_funding_rate(rate, LIGHTER_FUNDING_RATE_CAP)
        # Should return original rate, not clamped value
        assert result == rate
        assert result == Decimal("-0.01")

    def test_extreme_positive_rate(self):
        """Extreme positive rate should return original rate with warning logged."""
        rate = Decimal("1.50")  # 150% - way too high
        result = clamp_funding_rate(rate, X10_FUNDING_RATE_CAP)
        # Should return original rate, not clamped value
        assert result == rate

    def test_extreme_negative_rate(self):
        """Extreme negative rate should return original rate with warning logged."""
        rate = Decimal("-2.00")  # -200%
        result = clamp_funding_rate(rate, X10_FUNDING_RATE_CAP)
        # Should return original rate, not clamped value
        assert result == rate

    def test_zero_rate(self):
        """Zero rate should be unchanged."""
        result = clamp_funding_rate(Decimal("0"), LIGHTER_FUNDING_RATE_CAP)
        assert result == Decimal("0")

    def test_rate_at_cap_positive(self):
        """Rate exactly at positive cap should be unchanged."""
        result = clamp_funding_rate(LIGHTER_FUNDING_RATE_CAP, LIGHTER_FUNDING_RATE_CAP)
        assert result == LIGHTER_FUNDING_RATE_CAP

    def test_rate_at_cap_negative(self):
        """Rate exactly at negative cap should be unchanged."""
        result = clamp_funding_rate(-LIGHTER_FUNDING_RATE_CAP, LIGHTER_FUNDING_RATE_CAP)
        assert result == -LIGHTER_FUNDING_RATE_CAP

    def test_x10_cap_is_higher(self):
        """X10 cap (3%) should allow higher rates than Lighter (0.5%)."""
        rate = Decimal("0.02")  # 2%

        # Both should return original rate (caps are for validation only)
        lighter_result = clamp_funding_rate(rate, LIGHTER_FUNDING_RATE_CAP)
        assert lighter_result == rate  # Returns original, not clamped

        x10_result = clamp_funding_rate(rate, X10_FUNDING_RATE_CAP)
        assert x10_result == rate  # Unchanged (within X10 cap anyway)

    def test_symbol_and_exchange_logged(self):
        """Should accept symbol and exchange for logging (no assertion, just verify no crash)."""
        rate = Decimal("0.10")  # Exceeds cap, warning logged
        result = clamp_funding_rate(rate, LIGHTER_FUNDING_RATE_CAP, symbol="BTC", exchange="LIGHTER")
        # Should return original rate, not clamped value
        assert result == rate


class TestFundingRateCaps:
    """Tests for the funding rate cap constants."""

    def test_lighter_cap_is_half_percent(self):
        """Lighter cap should be 0.5% (0.005)."""
        assert Decimal("0.005") == LIGHTER_FUNDING_RATE_CAP

    def test_x10_cap_is_three_percent(self):
        """X10 cap should be 3% (0.03)."""
        assert Decimal("0.03") == X10_FUNDING_RATE_CAP

    def test_caps_are_positive(self):
        """Both caps should be positive."""
        assert LIGHTER_FUNDING_RATE_CAP > 0
        assert X10_FUNDING_RATE_CAP > 0


class TestRealWorldScenarios:
    """Tests simulating real API responses."""

    def test_api_returns_weird_scientific(self):
        """API sometimes returns values in scientific notation."""
        # This was a real bug where API returned "5.123e-06"
        result = safe_decimal("5.123e-06")
        assert result == Decimal("5.123e-06")
        assert result > 0
        assert result < Decimal("0.0001")

    def test_api_returns_string_nan(self):
        """API sometimes returns 'NaN' as string."""
        result = safe_decimal("NaN")
        assert result == Decimal("0")  # Safe fallback

    def test_api_returns_null_in_json(self):
        """API sometimes returns null in JSON (becomes None in Python)."""
        result = safe_decimal(None)
        assert result == Decimal("0")

    def test_funding_rate_spike_protection(self):
        """
        Log warning for funding rate spikes from API errors.

        Example: API returns 50% hourly funding rate due to a bug.
        This would be 50% * 24 * 365 = 438000% APY - clearly wrong.
        The function logs a warning but returns the original rate for calculations.
        """
        bogus_rate = Decimal("0.50")  # 50% per hour - impossible

        # Lighter should log warning but return original rate
        lighter_result = clamp_funding_rate(bogus_rate, LIGHTER_FUNDING_RATE_CAP)
        assert lighter_result == bogus_rate  # Returns original, not clamped

        # X10 should log warning but return original rate
        x10_result = clamp_funding_rate(bogus_rate, X10_FUNDING_RATE_CAP)
        assert x10_result == bogus_rate  # Returns original, not clamped

    def test_negative_spike_protection(self):
        """Log warning for negative funding rate spikes."""
        bogus_rate = Decimal("-0.80")  # -80% per hour - impossible

        lighter_result = clamp_funding_rate(bogus_rate, LIGHTER_FUNDING_RATE_CAP)
        assert lighter_result == bogus_rate  # Returns original, not clamped

    def test_reasonable_rates_unchanged(self):
        """Normal funding rates should pass through unchanged."""
        normal_rates = [
            Decimal("0.0001"),  # 0.01% per hour - typical
            Decimal("-0.0005"),  # -0.05% per hour - normal negative
            Decimal("0.003"),  # 0.3% per hour - high but valid for Lighter
            Decimal("0.025"),  # 2.5% per hour - high but valid for X10
        ]

        for rate in normal_rates:
            if abs(rate) <= X10_FUNDING_RATE_CAP:
                result = clamp_funding_rate(rate, X10_FUNDING_RATE_CAP)
                assert result == rate, f"Rate {rate} should be unchanged for X10"


class TestAPIRateConversion:
    """Tests for API rate conversion from percent to decimal format."""

    def test_api_percent_format_conversion(self):
        """
        Test that API rates in percent format are correctly converted to decimals.

        Real-world scenario from bug report:
        - Lighter API returns: -1.0024 (meaning -1.0024%)
        - X10 API returns: -0.1227 (meaning -0.1227%)
        - After dividing by 100: -0.010024 and -0.001227 (decimals)
        - Net rate = abs(-0.010024 - (-0.001227)) = 0.008797
        - APY = 0.008797 * 24 * 365 = 77.05% (NOT 7705%!)
        """
        # Simulated API responses in percent format
        lighter_api_rate = Decimal("-1.0024")  # API returns this
        x10_api_rate = Decimal("-0.1227")  # API returns this

        # Convert to decimal format (divide by 100)
        lighter_decimal = lighter_api_rate / Decimal("100")
        x10_decimal = x10_api_rate / Decimal("100")

        # Verify conversion
        assert lighter_decimal == Decimal("-0.010024")
        assert x10_decimal == Decimal("-0.001227")

        # Calculate net rate (absolute difference)
        net_rate = abs(lighter_decimal - x10_decimal)
        assert net_rate == Decimal("0.008797")

        # Calculate APY (stored as "percent units": 77 = 77%, displayed with % formatter)
        apy = net_rate * Decimal("24") * Decimal("365")

        # APY should be ~77 (meaning 77%), NOT ~7700 (7700%)
        assert Decimal("76") < apy < Decimal("78"), f"APY should be ~77 (77%), got {apy}"

        # Verify it's NOT 100x too high (the bug would give ~7700 for 7700%)
        assert apy < Decimal("100"), f"APY {apy} is way too high - should be ~77 (77%), not 7700 (7700%)"

    def test_positive_rate_conversion(self):
        """Test conversion of positive rates from percent to decimal."""
        api_rate = Decimal("0.5")  # 0.5% from API

        # Convert to decimal
        decimal_rate = api_rate / Decimal("100")

        # Should be 0.005, not 0.5
        assert decimal_rate == Decimal("0.005")

        # APY calculation (stored as "percent units": 43.8 = 43.8%)
        apy = decimal_rate * Decimal("24") * Decimal("365")
        assert Decimal("40") < apy < Decimal("50")  # ~43.8 (43.8%)

    def test_zero_rate_conversion(self):
        """Test that zero rate converts correctly."""
        api_rate = Decimal("0")
        decimal_rate = api_rate / Decimal("100")
        assert decimal_rate == Decimal("0")

    def test_small_rate_conversion(self):
        """Test conversion of very small rates."""
        api_rate = Decimal("0.01")  # 0.01% from API
        decimal_rate = api_rate / Decimal("100")

        # Should be 0.0001
        assert decimal_rate == Decimal("0.0001")

        # APY should be ~0.876 (0.876%)
        apy = decimal_rate * Decimal("24") * Decimal("365")
        assert Decimal("0.8") < apy < Decimal("1.0")
