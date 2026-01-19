"""
Historical data persistence for funding candles and crash events.

Provides hourly-level data storage for APY crash detection and
recovery time prediction.

NOTE: Exchanges only provide 1h resolution - minute-level data is not available.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.historical import FundingCandle


async def insert_funding_candles(
    self,
    candles: list[FundingCandle],
) -> int:
    """
    Batch insert funding candles with UPSERT to handle duplicates.

    Returns the number of records inserted.
    """
    if not candles:
        return 0

    now = datetime.now(UTC).isoformat()

    # Build batch insert rows
    rows = []
    for candle in candles:
        rows.append(
            (
                candle.timestamp.isoformat(),
                str(candle.symbol),
                candle.exchange,
                str(candle.mark_price) if candle.mark_price is not None else None,
                str(candle.index_price) if candle.index_price is not None else None,
                str(candle.spread_bps) if candle.spread_bps is not None else None,
                str(candle.funding_rate_hourly),
                str(candle.funding_apy),
                now,
                "backfill",
            )
        )

    # Execute batch insert with UPSERT
    await self._write_queue.put(
        {
            "action": "insert_funding_candles",
            "rows": rows,
        }
    )

    return len(rows)


async def update_volatility_profile(
    self,
    symbol: str,
    profile: dict[str, object],
) -> None:
    """Update volatility metrics for a symbol."""
    await self._write_queue.put(
        {
            "action": "update_volatility_profile",
            "symbol": symbol,
            "period_days": profile["period_days"],
            "calculated_at": datetime.now(UTC).isoformat(),
            "hourly_std_dev": str(profile["hourly_std_dev"]),
            "hourly_range_avg": str(profile["hourly_range_avg"]),
            "crash_frequency": profile["crash_frequency"],
            "avg_crash_duration_minutes": str(profile["avg_crash_duration_minutes"]),
            "avg_recovery_time_minutes": str(profile["avg_recovery_time_minutes"]),
            "p25_apy": str(profile["p25_apy"]),
            "p50_apy": str(profile["p50_apy"]),
            "p75_apy": str(profile["p75_apy"]),
            "p90_apy": str(profile["p90_apy"]),
            "p95_apy": str(profile["p95_apy"]),
        }
    )


async def get_volatility_profile(
    self,
    symbol: str,
    period_days: int = 90,
) -> dict[str, object] | None:
    """
    Get the most recent volatility profile for a symbol.

    Returns dict with volatility metrics or None if not found.
    """
    cursor = await self._conn.execute(
        """
        SELECT symbol, period_days, calculated_at,
               hourly_std_dev, hourly_range_avg, crash_frequency,
               avg_crash_duration_minutes, avg_recovery_time_minutes,
               p25_apy, p50_apy, p75_apy, p90_apy, p95_apy
        FROM funding_volatility_metrics
        WHERE symbol = ?
          AND period_days = ?
        ORDER BY calculated_at DESC
        LIMIT 1
        """,
        (symbol, period_days),
    )

    row = await cursor.fetchone()

    if row is None:
        return None

    return {
        "symbol": row[0],
        "period_days": row[1],
        "calculated_at": row[2],
        "hourly_std_dev": Decimal(row[3]),
        "hourly_range_avg": Decimal(row[4]),
        "crash_frequency": row[5],
        "avg_crash_duration_minutes": Decimal(row[6]),
        "avg_recovery_time_minutes": Decimal(row[7]),
        "p25_apy": Decimal(row[8]),
        "p50_apy": Decimal(row[9]),
        "p75_apy": Decimal(row[10]),
        "p90_apy": Decimal(row[11]),
        "p95_apy": Decimal(row[12]),
    }
