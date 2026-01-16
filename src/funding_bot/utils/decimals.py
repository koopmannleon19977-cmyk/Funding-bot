"""
Decimal helpers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# Exchange-documented funding rate caps (hourly)
# Lighter: ±0.5%/hour (docs.lighter.xyz/perpetual-futures/funding)
# Extended/X10: ±1-3%/hour depending on asset group (we use the max: 3%)
LIGHTER_FUNDING_RATE_CAP = Decimal("0.005")  # 0.5%
X10_FUNDING_RATE_CAP = Decimal("0.03")  # 3% (conservative max for all groups)


def safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safely convert a value to Decimal.

    Returns default for None, NaN, Infinity, and invalid values.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return default
        return value
    try:
        result = Decimal(str(value))
        # Reject NaN and Infinity
        if result.is_nan() or result.is_infinite():
            return default
        return result
    except Exception:
        return default


def clamp_funding_rate(
    rate: Decimal,
    cap: Decimal,
    symbol: str = "",
    exchange: str = "",
) -> Decimal:
    """
    Validate funding rate against documented exchange caps.

    Rates outside the cap are anomalies (API errors, manipulation).
    This function logs a warning but returns the ORIGINAL rate for calculations.

    The rate caps are for validation only - exchanges may temporarily exceed
    their documented caps during extreme market conditions. We log these anomalies
    but still use the actual market rate for trading decisions.

    Args:
        rate: The hourly funding rate (decimal, e.g. 0.005 = 0.5%)
        cap: The exchange-specific cap (e.g. LIGHTER_FUNDING_RATE_CAP)
        symbol: For logging
        exchange: For logging

    Returns:
        The original rate (unmodified), with a warning if it exceeds cap.
    """
    if abs(rate) <= cap:
        return rate

    # Import here to avoid circular dependency
    from funding_bot.observability.logging import get_logger
    logger = get_logger(__name__)

    clamped = max(-cap, min(cap, rate))
    logger.warning(
        f"[RATE_CAP] {exchange} {symbol}: Rate {rate:.6f} exceeds cap ±{cap:.4f}, "
        f"would clamp to {clamped:.6f}. Using original rate for calculations."
    )
    # Return original rate, not clamped value
    return rate

