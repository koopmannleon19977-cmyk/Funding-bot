"""
Domain models for historical APY and volatility analysis.

These models support hourly-level crash detection and recovery time prediction.
NOTE: Exchanges only provide 1h funding rate resolution, not minute-level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class FundingCandle:
    """Hourly-level funding and price data for crash detection (exchanges only provide 1h resolution)."""

    timestamp: datetime
    symbol: str
    exchange: str  # "LIGHTER" or "X10"
    mark_price: Decimal | None
    index_price: Decimal | None
    spread_bps: Decimal | None  # (mark - index) / index * 10000
    funding_rate_hourly: Decimal
    funding_apy: Decimal
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class VolatilityProfile:
    """Statistical volatility profile for a symbol."""

    symbol: str
    period_days: int
    calculated_at: datetime

    # Volatility metrics
    hourly_std_dev: Decimal
    hourly_range_avg: Decimal
    crash_frequency: int
    avg_crash_duration_minutes: Decimal
    avg_recovery_time_minutes: Decimal

    # Quantiles for prediction
    p25_apy: Decimal
    p50_apy: Decimal
    p75_apy: Decimal
    p90_apy: Decimal
    p95_apy: Decimal
