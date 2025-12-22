import asyncio
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TradeStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class TradeState:
    symbol: str
    side_x10: str
    side_lighter: str
    size_usd: float
    entry_price_x10: float
    entry_price_lighter: float
    status: TradeStatus
    created_at: int
    pnl: float = 0.0
    funding_collected: float = 0.0
    closed_at: Optional[int] = None


class InMemoryStateManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def start(self):
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                symbol TEXT PRIMARY KEY,
                side_x10 TEXT,
                side_lighter TEXT,
                size_usd REAL,
                entry_price_x10 REAL,
                entry_price_lighter REAL,
                status TEXT,
                created_at INTEGER,
                pnl REAL,
                funding_collected REAL,
                closed_at INTEGER
            )
            """
        )
        self._conn.commit()

    async def stop(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    async def add_trade(self, trade: TradeState):
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO trades (symbol, side_x10, side_lighter, size_usd, entry_price_x10, entry_price_lighter, status, created_at, pnl, funding_collected, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade.symbol,
                trade.side_x10,
                trade.side_lighter,
                trade.size_usd,
                trade.entry_price_x10,
                trade.entry_price_lighter,
                trade.status.value,
                trade.created_at,
                trade.pnl,
                trade.funding_collected,
                trade.closed_at,
            ),
        )
        self._conn.commit()

    async def close_trade(self, symbol: str, pnl: float, funding: float):
        assert self._conn is not None
        closed_at = int(asyncio.get_event_loop().time() * 1000)
        self._conn.execute(
            "UPDATE trades SET status=?, pnl=?, funding_collected=?, closed_at=? WHERE symbol=?",
            (TradeStatus.CLOSED.value, pnl, funding, closed_at, symbol),
        )
        self._conn.commit()

    async def _flush_writes(self):
        if self._conn:
            self._conn.commit()
