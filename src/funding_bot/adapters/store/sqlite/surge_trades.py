"""
Surge Pro trade CRUD and risk query helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import (
    Exchange,
    Side,
    SurgeTrade,
    SurgeTradeStatus,
)
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


def _row_to_surge_trade(self, row: tuple, description: Any) -> SurgeTrade:
    """Convert a database row to a SurgeTrade object."""
    columns = [col[0] for col in description]
    data = dict(zip(columns, row, strict=False))

    # Parse timestamps
    created_at = datetime.fromisoformat(data["created_at"]) if data["created_at"] else datetime.now(UTC)
    opened_at = datetime.fromisoformat(data["opened_at"]) if data.get("opened_at") else None
    closed_at = datetime.fromisoformat(data["closed_at"]) if data.get("closed_at") else None

    return SurgeTrade(
        trade_id=data["trade_id"],
        symbol=data["symbol"],
        exchange=Exchange(data["exchange"]),
        side=Side(data["side"]),
        qty=Decimal(str(data["qty"])) if data["qty"] else Decimal("0"),
        entry_price=Decimal(str(data["entry_price"])) if data.get("entry_price") else Decimal("0"),
        exit_price=Decimal(str(data["exit_price"])) if data.get("exit_price") else Decimal("0"),
        fees=Decimal(str(data["fees"])) if data.get("fees") else Decimal("0"),
        status=SurgeTradeStatus(data["status"]),
        entry_order_id=data.get("entry_order_id"),
        entry_client_order_id=data.get("entry_client_order_id"),
        exit_order_id=data.get("exit_order_id"),
        exit_client_order_id=data.get("exit_client_order_id"),
        entry_reason=data.get("entry_reason"),
        exit_reason=data.get("exit_reason"),
        created_at=created_at,
        opened_at=opened_at,
        closed_at=closed_at,
    )


def _surge_trade_to_row(self, trade: SurgeTrade) -> dict[str, Any]:
    """Convert a SurgeTrade object to a database row dict."""
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "exchange": trade.exchange.value,
        "side": trade.side.value,
        "qty": str(trade.qty),
        "entry_price": str(trade.entry_price),
        "exit_price": str(trade.exit_price),
        "fees": str(trade.fees),
        "status": trade.status.value,
        "entry_order_id": trade.entry_order_id,
        "entry_client_order_id": trade.entry_client_order_id,
        "exit_order_id": trade.exit_order_id,
        "exit_client_order_id": trade.exit_client_order_id,
        "entry_reason": trade.entry_reason,
        "exit_reason": trade.exit_reason,
        "created_at": trade.created_at.isoformat(),
        "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
    }


async def create_surge_trade(self, trade: SurgeTrade) -> str:
    """
    Create a new surge trade.

    Args:
        trade: The surge trade to create
    """
    if not self._conn:
        raise RuntimeError("Database not connected")

    row = self._surge_trade_to_row(trade)
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_names = ", ".join(columns)

    await self._conn.execute(
        f"INSERT OR REPLACE INTO surge_trades ({column_names}) VALUES ({placeholders})",
        tuple(row[col] for col in columns),
    )
    await self._conn.commit()

    logger.debug(f"Created surge trade {trade.trade_id} for {trade.symbol}")
    return trade.trade_id


async def update_surge_trade(self, trade: SurgeTrade) -> None:
    """Update an existing surge trade."""
    if not self._conn:
        raise RuntimeError("Database not connected")

    row = self._surge_trade_to_row(trade)
    columns = list(row.keys())
    set_clause = ", ".join(f"{col} = ?" for col in columns if col != "trade_id")
    values = [row[col] for col in columns if col != "trade_id"]
    values.append(trade.trade_id)

    await self._conn.execute(
        f"UPDATE surge_trades SET {set_clause} WHERE trade_id = ?",
        tuple(values),
    )
    await self._conn.commit()


async def get_surge_trade(self, trade_id: str) -> SurgeTrade | None:
    """Get a surge trade by ID."""
    if not self._conn:
        return None

    cursor = await self._conn.execute(
        "SELECT * FROM surge_trades WHERE trade_id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    if row:
        return self._row_to_surge_trade(row, cursor.description)
    return None


async def get_daily_pnl(self) -> Decimal:
    """Get total PnL for today (UTC)."""
    if not self._conn:
        return Decimal("0")

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    cursor = await self._conn.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN side = 'BUY'
                THEN (exit_price - entry_price) * qty
                ELSE (entry_price - exit_price) * qty
            END - COALESCE(fees, 0)
        ), 0) as pnl
        FROM surge_trades
        WHERE status = 'CLOSED'
        AND closed_at >= ?
        """,
        (today_start.isoformat(),),
    )
    row = await cursor.fetchone()
    return Decimal(str(row[0])) if row else Decimal("0")


async def get_hourly_pnl(self) -> Decimal:
    """Get total PnL for last hour."""
    if not self._conn:
        return Decimal("0")

    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    cursor = await self._conn.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN side = 'BUY'
                THEN (exit_price - entry_price) * qty
                ELSE (entry_price - exit_price) * qty
            END - COALESCE(fees, 0)
        ), 0) as pnl
        FROM surge_trades
        WHERE status = 'CLOSED'
        AND closed_at >= ?
        """,
        (hour_ago.isoformat(),),
    )
    row = await cursor.fetchone()
    return Decimal(str(row[0])) if row else Decimal("0")


async def get_recent_trades(self, count: int = 10) -> list[SurgeTrade]:
    """Get N most recent closed trades."""
    if not self._conn:
        return []

    cursor = await self._conn.execute(
        """
        SELECT * FROM surge_trades
        WHERE status = 'CLOSED'
        ORDER BY closed_at DESC
        LIMIT ?
        """,
        (count,),
    )
    rows = await cursor.fetchall()
    return [self._row_to_surge_trade(row, cursor.description) for row in rows]


async def get_hourly_fill_rate(self) -> float:
    """Get fill rate (filled / attempts) for last hour."""
    if not self._conn:
        return 0.0

    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    cursor = await self._conn.execute(
        """
        SELECT
            COUNT(CASE WHEN status IN ('OPEN', 'CLOSED') THEN 1 END) as filled,
            COUNT(*) as total
        FROM surge_trades
        WHERE opened_at >= ?
        """,
        (hour_ago.isoformat(),),
    )
    row = await cursor.fetchone()
    if row and row[1] > 0:
        return row[0] / row[1]
    return 0.0


async def list_open_surge_trades(self) -> list[SurgeTrade]:
    """Get all surge trades with status OPEN or PENDING."""
    if not self._conn:
        return []

    cursor = await self._conn.execute(
        """
        SELECT * FROM surge_trades
        WHERE status IN ('PENDING', 'OPEN')
        ORDER BY created_at DESC
        """,
    )
    rows = await cursor.fetchall()
    return [self._row_to_surge_trade(row, cursor.description) for row in rows]
