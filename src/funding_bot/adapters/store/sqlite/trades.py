"""
Trade CRUD and cache helpers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def _load_open_trades(self) -> None:
    """Load open trades into cache."""
    if not self._conn:
        return

    async with self._cache_lock:
        cursor = await self._conn.execute(
            "SELECT * FROM trades WHERE status IN (?, ?, ?, ?)",
            (TradeStatus.PENDING.value, TradeStatus.OPENING.value, TradeStatus.OPEN.value, TradeStatus.CLOSING.value),
        )
        rows = await cursor.fetchall()

        for row in rows:
            trade = self._row_to_trade(row, cursor.description)
            self._trade_cache[trade.trade_id] = trade


def _row_to_trade(self, row: tuple, description: Any) -> Trade:
    """Convert a database row to a Trade object."""
    # Build column name -> value mapping
    columns = [col[0] for col in description]
    data = dict(zip(columns, row, strict=False))

    # Parse leg 1
    leg1 = TradeLeg(
        exchange=Exchange(data["leg1_exchange"]),
        side=Side(data["leg1_side"]),
        order_id=data["leg1_order_id"],
        qty=Decimal(str(data["leg1_qty"])) if data["leg1_qty"] else Decimal("0"),
        filled_qty=Decimal(str(data["leg1_filled_qty"])) if data["leg1_filled_qty"] else Decimal("0"),
        entry_price=Decimal(str(data["leg1_entry_price"])) if data["leg1_entry_price"] else Decimal("0"),
        exit_price=Decimal(str(data["leg1_exit_price"])) if data["leg1_exit_price"] else Decimal("0"),
        fees=Decimal(str(data["leg1_fees"])) if data["leg1_fees"] else Decimal("0"),
    )

    # Parse leg 2
    leg2 = TradeLeg(
        exchange=Exchange(data["leg2_exchange"]),
        side=Side(data["leg2_side"]),
        order_id=data["leg2_order_id"],
        qty=Decimal(str(data["leg2_qty"])) if data["leg2_qty"] else Decimal("0"),
        filled_qty=Decimal(str(data["leg2_filled_qty"])) if data["leg2_filled_qty"] else Decimal("0"),
        entry_price=Decimal(str(data["leg2_entry_price"])) if data["leg2_entry_price"] else Decimal("0"),
        exit_price=Decimal(str(data["leg2_exit_price"])) if data["leg2_exit_price"] else Decimal("0"),
        fees=Decimal(str(data["leg2_fees"])) if data["leg2_fees"] else Decimal("0"),
    )

    # Parse timestamps
    created_at = datetime.fromisoformat(data["created_at"]) if data["created_at"] else datetime.now(UTC)
    opened_at = datetime.fromisoformat(data["opened_at"]) if data["opened_at"] else None
    closed_at = datetime.fromisoformat(data["closed_at"]) if data["closed_at"] else None
    last_funding = datetime.fromisoformat(data["last_funding_update"]) if data["last_funding_update"] else None
    last_eval_at = datetime.fromisoformat(data["last_eval_at"]) if data.get("last_eval_at") else None

    # Parse events
    events = json.loads(data["events"]) if data["events"] else []

    return Trade(
        trade_id=data["trade_id"],
        symbol=data["symbol"],
        leg1=leg1,
        leg2=leg2,
        target_qty=Decimal(str(data["target_qty"])),
        target_notional_usd=Decimal(str(data["target_notional_usd"])),
        status=TradeStatus(data["status"]),
        execution_state=ExecutionState(data["execution_state"]),
        funding_collected=Decimal(str(data["funding_collected"])) if data["funding_collected"] else Decimal("0"),
        last_funding_update=last_funding,
        realized_pnl=Decimal(str(data["realized_pnl"])) if data["realized_pnl"] else Decimal("0"),
        unrealized_pnl=Decimal(str(data["unrealized_pnl"])) if data["unrealized_pnl"] else Decimal("0"),
        high_water_mark=Decimal(str(data.get("high_water_mark") or 0)),
        entry_apy=Decimal(str(data["entry_apy"])) if data["entry_apy"] else Decimal("0"),
        entry_spread=Decimal(str(data["entry_spread"])) if data["entry_spread"] else Decimal("0"),
        current_apy=Decimal(str(data.get("current_apy") or 0)),
        current_lighter_rate=Decimal(str(data.get("current_lighter_rate") or 0)),
        current_x10_rate=Decimal(str(data.get("current_x10_rate") or 0)),
        last_eval_at=last_eval_at,
        close_reason=data["close_reason"],
        created_at=created_at,
        opened_at=opened_at,
        closed_at=closed_at,
        events=events,
    )


def _trade_to_row(self, trade: Trade) -> dict[str, Any]:
    """Convert a Trade object to a database row dict."""
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "status": trade.status.value,
        "execution_state": trade.execution_state.value,
        "leg1_exchange": trade.leg1.exchange.value,
        "leg1_side": trade.leg1.side.value,
        "leg1_order_id": trade.leg1.order_id,
        "leg1_qty": str(trade.leg1.qty),
        "leg1_filled_qty": str(trade.leg1.filled_qty),
        "leg1_entry_price": str(trade.leg1.entry_price),
        "leg1_exit_price": str(trade.leg1.exit_price),
        "leg1_fees": str(trade.leg1.fees),
        "leg2_exchange": trade.leg2.exchange.value,
        "leg2_side": trade.leg2.side.value,
        "leg2_order_id": trade.leg2.order_id,
        "leg2_qty": str(trade.leg2.qty),
        "leg2_filled_qty": str(trade.leg2.filled_qty),
        "leg2_entry_price": str(trade.leg2.entry_price),
        "leg2_exit_price": str(trade.leg2.exit_price),
        "leg2_fees": str(trade.leg2.fees),
        "target_qty": str(trade.target_qty),
        "target_notional_usd": str(trade.target_notional_usd),
        "funding_collected": str(trade.funding_collected),
        "last_funding_update": trade.last_funding_update.isoformat() if trade.last_funding_update else None,
        "realized_pnl": str(trade.realized_pnl),
        "unrealized_pnl": str(trade.unrealized_pnl),
        "high_water_mark": str(trade.high_water_mark),
        "entry_apy": str(trade.entry_apy),
        "entry_spread": str(trade.entry_spread),
        "current_apy": str(getattr(trade, "current_apy", Decimal("0"))),
        "current_lighter_rate": str(getattr(trade, "current_lighter_rate", Decimal("0"))),
        "current_x10_rate": str(getattr(trade, "current_x10_rate", Decimal("0"))),
        "last_eval_at": trade.last_eval_at.isoformat() if getattr(trade, "last_eval_at", None) else None,
        "close_reason": trade.close_reason,
        "created_at": trade.created_at.isoformat(),
        "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
        "events": json.dumps(trade.events),
        # ✅ FIX Bug #3: Persist total_fees to DB (was missing, causing total_fees column to stay 0)
        # Trade.total_fees is a @property (leg1.fees + leg2.fees), but DB needs explicit write
        "total_fees": str(trade.total_fees),
        # Slippage tracking (performance analytics)
        "estimated_exit_pnl": str(getattr(trade, "estimated_exit_pnl", Decimal("0"))),
        "actual_exit_pnl": str(getattr(trade, "actual_exit_pnl", Decimal("0"))),
        "exit_slippage_usd": str(getattr(trade, "exit_slippage_usd", Decimal("0"))),
        "exit_spread_pct": str(getattr(trade, "exit_spread_pct", Decimal("0"))),
        "close_attempts": int(getattr(trade, "close_attempts", 0)),
        "close_duration_seconds": str(getattr(trade, "close_duration_seconds", Decimal("0"))),
    }


async def create_trade(self, trade: Trade, sync: bool = True) -> str:
    """
    Create a new trade.

    Args:
        trade: The trade to create
        sync: If True, write synchronously to prevent data loss on crash (default: True)
              Only set to False for non-critical operations where eventual consistency is acceptable.
    """
    async with self._cache_lock:
        self._trade_cache[trade.trade_id] = trade

    item = {
        "action": "upsert_trade",
        "data": self._trade_to_row(trade),
    }

    if sync:
        # CRITICAL: Synchronous write for trade creation to prevent ghost positions
        # If the bot crashes after placing orders but before DB write, we end up
        # with positions on exchange but no record in DB -> Reconciler closes them as ghosts
        if not self._conn:
            raise RuntimeError("Database not connected")
        await self._upsert_trade_row(item["data"])
        await self._conn.commit()
        logger.debug(f"Created trade {trade.trade_id} for {trade.symbol} (sync write)")
    else:
        # Queue for async write (only for non-critical updates)
        await self._write_queue.put(item)
        logger.debug(f"Created trade {trade.trade_id} for {trade.symbol} (async write)")

    return trade.trade_id


async def get_trade(self, trade_id: str) -> Trade | None:
    """Get a trade by ID."""
    async with self._cache_lock:
        if trade_id in self._trade_cache:
            return self._trade_cache[trade_id]

    # Check database
    if self._conn:
        cursor = await self._conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row:
            trade = self._row_to_trade(row, cursor.description)
            async with self._cache_lock:
                self._trade_cache[trade_id] = trade
            return trade

    return None


async def get_trade_by_symbol(self, symbol: str) -> Trade | None:
    """Get the active trade for a symbol."""
    async with self._cache_lock:
        for trade in self._trade_cache.values():
            if trade.symbol == symbol and trade.status in (
                TradeStatus.PENDING,
                TradeStatus.OPENING,
                TradeStatus.OPEN,
                TradeStatus.CLOSING,
            ):
                return trade

    # Check database
    if self._conn:
        cursor = await self._conn.execute(
            """
            SELECT * FROM trades
            WHERE symbol = ? AND status IN (?, ?, ?, ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                symbol,
                TradeStatus.PENDING.value,
                TradeStatus.OPENING.value,
                TradeStatus.OPEN.value,
                TradeStatus.CLOSING.value,
            ),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_trade(row, cursor.description)

    return None


async def update_trade(self, trade_id: str, updates: dict[str, Any]) -> bool:
    """Update a trade with partial data."""
    async with self._cache_lock:
        trade = self._trade_cache.get(trade_id)
        if not trade:
            return False

        # Apply updates to cached trade
        for key, value in updates.items():
            if hasattr(trade, key):
                setattr(trade, key, value)
            elif key.startswith("leg1_"):
                leg_attr = key[5:]
                if hasattr(trade.leg1, leg_attr):
                    setattr(trade.leg1, leg_attr, value)
            elif key.startswith("leg2_"):
                leg_attr = key[5:]
                if hasattr(trade.leg2, leg_attr):
                    setattr(trade.leg2, leg_attr, value)

    # Queue for write
    await self._write_queue.put({
        "action": "upsert_trade",
        "data": self._trade_to_row(trade),
    })

    return True


async def list_open_trades(self) -> list[Trade]:
    """Get all trades with status OPEN or OPENING."""
    async with self._cache_lock:
        return [
            trade for trade in self._trade_cache.values()
            if trade.status in (TradeStatus.PENDING, TradeStatus.OPENING, TradeStatus.OPEN, TradeStatus.CLOSING)
        ]


async def list_trades(
    self,
    status: TradeStatus | None = None,
    symbol: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Trade]:
    """List trades with optional filters."""
    if not self._conn:
        return []

    conditions = []
    params: list[Any] = []

    if status:
        conditions.append("status = ?")
        params.append(status.value)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if since:
        conditions.append("created_at >= ?")
        params.append(since.isoformat())

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    cursor = await self._conn.execute(
        f"SELECT * FROM trades WHERE {where} ORDER BY created_at DESC LIMIT ?",
        params,
    )
    rows = await cursor.fetchall()

    return [self._row_to_trade(row, cursor.description) for row in rows]
