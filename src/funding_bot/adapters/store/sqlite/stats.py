"""
Stats, snapshots, and cleanup helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def save_pnl_snapshot(
    self,
    trade_id: str,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    funding: Decimal,
    fees: Decimal,
    timestamp: datetime,
) -> None:
    """Save a PnL snapshot for a trade."""
    await self._write_queue.put({
        "action": "save_snapshot",
        "data": {
            "trade_id": trade_id,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "funding": funding,
            "fees": fees,
            "timestamp": timestamp.isoformat(),
        },
    })


async def get_stats(self) -> dict[str, Any]:
    """Get aggregate statistics."""
    if not self._conn:
        return {}

    cursor = await self._conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_trades,
            SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as closed_trades,
            COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) as total_pnl,
            COALESCE(SUM(CAST(funding_collected AS REAL)), 0) as total_funding,
            COALESCE(SUM(CAST(leg1_fees AS REAL) + CAST(leg2_fees AS REAL)), 0) as total_fees
        FROM trades
    """)
    row = await cursor.fetchone()

    if not row:
        return {}

    return {
        "total_trades": row[0] or 0,
        "open_trades": row[1] or 0,
        "closed_trades": row[2] or 0,
        "total_pnl": Decimal(str(row[3] or 0)),
        "total_funding": Decimal(str(row[4] or 0)),
        "total_fees": Decimal(str(row[5] or 0)),
    }


async def cleanup_old_data(self, days: int = 30) -> int:
    """Clean up old data (snapshots, events)."""
    if not self._conn:
        return 0

    cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff_str = cutoff.isoformat()

    # Delete old snapshots
    cursor = await self._conn.execute(
        "DELETE FROM pnl_snapshots WHERE timestamp < ?",
        (cutoff_str,),
    )
    deleted = cursor.rowcount

    # Delete old events (keep last 30 days)
    cursor = await self._conn.execute(
        "DELETE FROM trade_events WHERE timestamp < ?",
        (cutoff_str,),
    )
    deleted += cursor.rowcount

    # Delete old attempts
    cursor = await self._conn.execute(
        "DELETE FROM execution_attempts WHERE created_at < ?",
        (cutoff_str,),
    )
    deleted += cursor.rowcount

    await self._conn.commit()
    logger.info(f"Cleaned up {deleted} old records")
    return deleted
