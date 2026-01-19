"""
Funding event helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.adapters.store.sqlite.utils import _maybe_decimal
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.adapters.store.sqlite.store import SQLiteTradeStore

logger = get_logger(__name__)

# Constants
HOURS_PER_YEAR = Decimal("8760")  # 24 * 365
DEFAULT_APY_HISTORY_HOURS = 168  # 7 days


async def record_funding(
    self: SQLiteTradeStore,
    trade_id: str,
    exchange: str,
    amount: Decimal,
    timestamp: datetime,
) -> None:
    """Record a funding payment for a trade."""
    await self._write_queue.put(
        {
            "action": "record_funding",
            "data": {
                "trade_id": trade_id,
                "exchange": exchange,
                "amount": amount,
                "timestamp": timestamp.isoformat(),
            },
        }
    )

    # Also update the trade's total funding
    async with self._cache_lock:
        if trade_id in self._trade_cache:
            trade = self._trade_cache[trade_id]
            trade.update_funding(amount)


async def get_total_funding(self: SQLiteTradeStore, trade_id: str) -> Decimal:
    """Get total funding collected for a trade."""
    if not self._conn:
        return Decimal("0")

    cursor = await self._conn.execute(
        "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM funding_events WHERE trade_id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    return Decimal(str(row[0])) if row else Decimal("0")


async def get_funding_breakdown(self: SQLiteTradeStore, trade_id: str) -> dict[str, Decimal]:
    """Get funding totals grouped by exchange label for a trade."""
    if not self._conn:
        return {}

    cursor = await self._conn.execute(
        """
        SELECT exchange, COALESCE(SUM(CAST(amount AS REAL)), 0)
        FROM funding_events
        WHERE trade_id = ?
        GROUP BY exchange
        """,
        (trade_id,),
    )
    rows = await cursor.fetchall()
    result: dict[str, Decimal] = {}
    for ex, amt in rows:
        result[str(ex)] = Decimal(str(amt or 0))
    return result


async def replace_funding_events(
    self: SQLiteTradeStore,
    trade_id: str,
    events: list[dict[str, Any]],
) -> None:
    """
    Replace all funding_events rows for a trade.

    Each event dict must include: exchange (str), amount (Decimal), timestamp (datetime).
    """
    # Queue for write
    await self._write_queue.put(
        {
            "action": "replace_funding_events",
            "data": {
                "trade_id": trade_id,
                "events": [
                    {
                        "exchange": str(e["exchange"]),
                        "amount": e["amount"],
                        "timestamp": e["timestamp"].isoformat(),
                    }
                    for e in events
                ],
            },
        }
    )

    # Update cached trade totals to stay consistent with the replaced event set.
    total = Decimal("0")
    last_ts: datetime | None = None
    for e in events:
        total += _maybe_decimal(e.get("amount")) or Decimal("0")
        last_ts = e.get("timestamp") if isinstance(e.get("timestamp"), datetime) else last_ts

    async with self._cache_lock:
        trade = self._trade_cache.get(trade_id)
        if trade:
            trade.funding_collected = total
            trade.last_funding_update = last_ts

    # Persist updated funding fields
    await self.update_trade(
        trade_id,
        {
            "funding_collected": total,
            "last_funding_update": last_ts,
        },
    )


async def record_funding_history(
    self: SQLiteTradeStore,
    symbol: str,
    exchange: str,
    rate_hourly: Decimal,
    rate_apy: Decimal | None = None,
    next_funding_time: datetime | None = None,
    timestamp: datetime | None = None,
) -> None:
    """
    Record a funding rate snapshot for historical analysis.

    This stores raw funding rates from both exchanges for:
    - APY stability detection (was 80% APY stable or a spike?)
    - PnL attribution (how much came from funding vs spread)
    - Backtesting and strategy optimization

    Uses INSERT OR IGNORE with unique constraint on (symbol, exchange, timestamp)
    to avoid duplicates when refresh runs multiple times per hour.
    """
    ts = timestamp or datetime.now(UTC)
    ts_str = ts.isoformat()

    # Calculate APY if not provided: rate * hours per year
    if rate_apy is None:
        rate_apy = rate_hourly * HOURS_PER_YEAR

    await self._write_queue.put(
        {
            "action": "record_funding_history",
            "data": {
                "timestamp": ts_str,
                "symbol": symbol,
                "exchange": exchange,
                "rate_hourly": rate_hourly,
                "rate_apy": rate_apy,
                "next_funding_time": next_funding_time.isoformat() if next_funding_time else None,
            },
        }
    )


async def get_recent_apy_history(
    self: SQLiteTradeStore,
    symbol: str,
    hours_back: int = DEFAULT_APY_HISTORY_HOURS,
) -> list[Decimal]:
    """
    Get historical net APY series for velocity/Z-score exits.

    Fetches hourly funding rates from both exchanges, computes the net APY
    (arbitrage spread) at each timestamp, and returns the time series.

    IMPORTANT: Only timestamps with data from BOTH exchanges are included.
    This prevents artificial spikes when one exchange has missing data.

    Net APY = abs(lighter_apy - x10_apy) at each hourly timestamp.

    Args:
        symbol: Trading symbol (e.g., "BTC-USD")
        hours_back: How many hours of history to return (default 168 = 7 days)

    Returns:
        List[Decimal]: Hourly net APY values, oldest first (ASC chronological)
        Empty list if insufficient data or database unavailable

    Example:
        >>> history = await store.get_recent_apy_history("BTC-USD", hours_back=24)
        >>> history
        [Decimal('0.14'), Decimal('0.15'), Decimal('0.16'), ...]  # 24 hourly values
    """
    if not self._conn:
        return []

    # Calculate cutoff time
    since = datetime.now(UTC) - timedelta(hours=hours_back)
    since_str = since.isoformat()

    try:
        # Fetch Lighter rates from BOTH tables (backfill + live data)
        # - funding_candles_minute: 90-day backfill data
        # - funding_history: live data from record_funding_history
        # UNION ensures we get data from both sources, deduped by timestamp
        lighter_cursor = await self._conn.execute(
            """
            SELECT timestamp, funding_apy FROM (
                SELECT timestamp, funding_apy
                FROM funding_candles_minute
                WHERE symbol = ?
                  AND exchange = 'LIGHTER'
                  AND timestamp >= ?
                UNION
                SELECT timestamp, rate_apy as funding_apy
                FROM funding_history
                WHERE symbol = ?
                  AND exchange = 'LIGHTER'
                  AND timestamp >= ?
            )
            ORDER BY timestamp DESC
            """,
            (symbol, since_str, symbol, since_str),
        )
        lighter_rows = await lighter_cursor.fetchall()

        # Fetch X10 rates from BOTH tables
        x10_cursor = await self._conn.execute(
            """
            SELECT timestamp, funding_apy FROM (
                SELECT timestamp, funding_apy
                FROM funding_candles_minute
                WHERE symbol = ?
                  AND exchange = 'X10'
                  AND timestamp >= ?
                UNION
                SELECT timestamp, rate_apy as funding_apy
                FROM funding_history
                WHERE symbol = ?
                  AND exchange = 'X10'
                  AND timestamp >= ?
            )
            ORDER BY timestamp DESC
            """,
            (symbol, since_str, symbol, since_str),
        )
        x10_rows = await x10_cursor.fetchall()

        # Build dictionaries for fast lookup
        lighter_map: dict[str, Decimal] = {}
        for ts, rate in lighter_rows:
            if rate is not None:
                lighter_map[ts] = Decimal(str(rate))

        x10_map: dict[str, Decimal] = {}
        for ts, rate in x10_rows:
            if rate is not None:
                x10_map[ts] = Decimal(str(rate))

        # INTERSECTION: Only timestamps with data from BOTH exchanges
        # This prevents artificial spikes when one exchange has missing data
        all_timestamps = set(lighter_map.keys()) & set(x10_map.keys())
        sorted_timestamps = sorted(all_timestamps)  # ASC: oldest first

        # Compute net APY series: abs(lighter - x10)
        net_apy_series: list[Decimal] = []
        for ts in sorted_timestamps:
            # No 0-fallback needed: both exchanges guaranteed to have data
            lighter_apy = lighter_map[ts]
            x10_apy = x10_map[ts]
            net_apy = abs(lighter_apy - x10_apy)
            net_apy_series.append(net_apy)

        return net_apy_series

    except Exception as e:
        logger.warning(f"Failed to fetch historical APY for {symbol}: {e}")
        return []
