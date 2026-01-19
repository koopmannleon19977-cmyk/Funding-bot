"""
SQLite Trade Store Implementation.

Features:
- WAL mode for concurrent reads
- In-memory cache with write-behind
- Automatic schema migrations
- Decimal precision handling
- Atomic updates
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

from funding_bot.adapters.store.sqlite.attempts import (
    create_execution_attempt,
    get_hedge_latency_stats,
    list_execution_attempts,
    update_execution_attempt,
)
from funding_bot.adapters.store.sqlite.events import append_event, list_events
from funding_bot.adapters.store.sqlite.funding import (
    get_funding_breakdown,
    get_recent_apy_history,
    get_total_funding,
    record_funding,
    record_funding_history,
    replace_funding_events,
)
from funding_bot.adapters.store.sqlite.historical import (
    get_volatility_profile,
    insert_funding_candles,
    update_volatility_profile,
)
from funding_bot.adapters.store.sqlite.migrations import _apply_schema_migrations
from funding_bot.adapters.store.sqlite.schema import SCHEMA_SQL
from funding_bot.adapters.store.sqlite.stats import cleanup_old_data, get_stats, save_pnl_snapshot
from funding_bot.adapters.store.sqlite.surge_trades import (
    _row_to_surge_trade,
    _surge_trade_to_row,
    create_surge_trade,
    get_daily_pnl,
    get_hourly_fill_rate,
    get_hourly_pnl,
    get_recent_trades,
    get_surge_trade,
    list_open_surge_trades,
    update_surge_trade,
)
from funding_bot.adapters.store.sqlite.trades import (
    _load_open_trades,
    _row_to_trade,
    _trade_to_row,
    create_trade,
    get_trade,
    get_trade_by_symbol,
    list_open_trades,
    list_trades,
    update_trade,
)
from funding_bot.adapters.store.sqlite.write_queue import (
    _flush_batch,
    _insert_event_row,
    _insert_event_rows_batch,
    _insert_funding_candles_rows,
    _insert_funding_history_row,
    _insert_funding_history_rows_batch,
    _insert_funding_row,
    _insert_funding_rows_batch,
    _insert_snapshot_row,
    _insert_snapshot_rows_batch,
    _replace_funding_events_rows,
    _upsert_trade_row,
    _upsert_trade_rows_batch,
    _upsert_volatility_profile_row,
    _write_loop,
)
from funding_bot.config.settings import Settings
from funding_bot.domain.models import Trade
from funding_bot.observability.logging import get_logger
from funding_bot.ports.store import TradeStorePort

logger = get_logger(__name__)


class SQLiteTradeStore(TradeStorePort):
    """
    SQLite-based trade store with in-memory cache.

    Uses write-behind pattern for performance:
    - Reads from cache
    - Writes go to cache immediately, then async to DB
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = Path(settings.database.path)
        self._conn: aiosqlite.Connection | None = None

        # In-memory cache
        self._trade_cache: dict[str, Trade] = {}
        self._cache_lock = asyncio.Lock()

        # Write-behind queue with backpressure (bounded queue)
        # 🚀 PERFORMANCE: Bounded queue prevents unbounded memory growth
        max_queue_size = getattr(settings.database, "write_queue_max_size", 1000)
        self._write_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=max_queue_size)
        self._writer_task: asyncio.Task | None = None

        # Statistics for backpressure monitoring
        self._write_queue_full_count = 0
        self._write_queue_total_puts = 0

        # 🚀 PERFORMANCE: Cache for list_open_trades() to avoid repeated queries
        # This is especially important for heartbeat which runs every 15s
        self._open_trades_cache: list[Trade] | None = None
        self._open_trades_cache_time: float = 0
        self._open_trades_cache_ttl: float = getattr(settings.database, "open_trades_cache_ttl", 5.0)

        self._initialized = False

    async def initialize(self) -> None:
        """Initialize database and load existing trades."""
        if self._initialized:
            return

        logger.info(f"Initializing SQLite store: {self.db_path}")

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Open connection
        self._conn = await aiosqlite.connect(
            str(self.db_path),
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )

        # Enable WAL mode for better concurrency
        if self.settings.database.wal_mode:
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=NORMAL")

        # Set busy timeout
        await self._conn.execute("PRAGMA busy_timeout=5000")

        # Create schema
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self._apply_schema_migrations()

        # Load open trades into cache
        await self._load_open_trades()

        # Start background writer
        self._writer_task = asyncio.create_task(self._write_loop(), name="sqlite_writer")

        self._initialized = True
        logger.info(f"SQLite store initialized with {len(self._trade_cache)} cached trades")

    async def close(self) -> None:
        """
        Close database connection.

        CRITICAL: This method MUST flush all pending writes before returning to prevent
        ghost positions (positions on exchange but not in DB). Any unflushed trade in
        the write queue will cause the reconciler to close positions as ghosts on startup.
        """
        if not self._initialized:
            return

        logger.info("Closing SQLite store...")

        # CRITICAL: Flush all pending writes before shutdown to prevent ghost positions
        # Any trade in the write queue that isn't flushed will result in positions
        # on exchange without DB records -> Reconciler will close them as ghosts
        if self._writer_task:
            # Send sentinel to stop writer
            await self._write_queue.put(None)

            # CRITICAL FIX: Don't timeout on graceful shutdown - wait for flush to complete
            # Previously, a 5s timeout would cancel the writer, losing all queued writes
            try:
                await asyncio.wait_for(self._writer_task, timeout=30.0)
            except TimeoutError:
                logger.error("Writer task timeout - forcing flush of remaining queue")
                # If we timeout, the writer might be stuck. Cancel and force flush.
                self._writer_task.cancel()
                try:
                    await self._writer_task
                except asyncio.CancelledError:
                    # Writer was cancelled - flush remaining items manually
                    remaining = []
                    while not self._write_queue.empty():
                        try:
                            item = self._write_queue.get_nowait()
                            if item is not None:  # Skip sentinel
                                remaining.append(item)
                        except asyncio.QueueEmpty:
                            break

                    if remaining and self._conn:
                        logger.warning(f"Force flushing {len(remaining)} remaining writes on shutdown")
                        await self._flush_batch(remaining)
                        await self._conn.commit()

        # Close connection
        if self._conn:
            await self._conn.close()
            self._conn = None

        self._initialized = False
        logger.info("SQLite store closed")

    async def _enqueue_write(self, item: dict[str, object]) -> None:
        """
        Safely enqueue a write operation with backpressure handling.

        If the queue is full, this will block until space is available,
        providing natural backpressure to prevent overwhelming the database.

        Args:
            item: Write operation to enqueue
        """
        self._write_queue_total_puts += 1

        try:
            # Try non-blocking put first
            self._write_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Queue is full, apply backpressure
            self._write_queue_full_count += 1

            # Log warning if queue is frequently full
            if self._write_queue_full_count % 10 == 1:
                logger.warning(
                    f"Write queue full (size={self._write_queue.qsize()}). "
                    f"Backpressure applied. Full count: {self._write_queue_full_count}"
                )

            # Block until space available (natural backpressure)
            await self._write_queue.put(item)

    def get_queue_stats(self) -> dict[str, object]:
        """Get write queue statistics for monitoring."""
        return {
            "queue_size": self._write_queue.qsize(),
            "queue_maxsize": self._write_queue.maxsize,
            "total_puts": self._write_queue_total_puts,
            "full_count": self._write_queue_full_count,
            "full_rate": (
                self._write_queue_full_count / self._write_queue_total_puts if self._write_queue_total_puts > 0 else 0
            ),
        }

    _apply_schema_migrations = _apply_schema_migrations
    _load_open_trades = _load_open_trades
    _row_to_trade = _row_to_trade
    _trade_to_row = _trade_to_row
    _write_loop = _write_loop
    _flush_batch = _flush_batch
    _upsert_trade_row = _upsert_trade_row
    _upsert_trade_rows_batch = _upsert_trade_rows_batch
    _insert_event_row = _insert_event_row
    _insert_event_rows_batch = _insert_event_rows_batch
    _insert_funding_row = _insert_funding_row
    _insert_funding_rows_batch = _insert_funding_rows_batch
    _insert_funding_history_row = _insert_funding_history_row
    _insert_funding_history_rows_batch = _insert_funding_history_rows_batch
    _insert_funding_candles_rows = _insert_funding_candles_rows
    _insert_snapshot_row = _insert_snapshot_row
    _insert_snapshot_rows_batch = _insert_snapshot_rows_batch
    _replace_funding_events_rows = _replace_funding_events_rows
    _upsert_volatility_profile_row = _upsert_volatility_profile_row

    create_trade = create_trade
    get_trade = get_trade
    get_trade_by_symbol = get_trade_by_symbol
    update_trade = update_trade
    list_open_trades = list_open_trades
    list_trades = list_trades

    async def list_open_trades_cached(self) -> list[Trade]:
        """
        Get open trades with TTL caching.

        🚀 PERFORMANCE: Caches results for 5 seconds (configurable) to avoid
        repeated database queries on every heartbeat (runs every 15s).
        Cache is automatically invalidated when trades are updated.
        """
        import time

        now = time.time()

        # Check if cache is valid
        if self._open_trades_cache is not None and (now - self._open_trades_cache_time) < self._open_trades_cache_ttl:
            return self._open_trades_cache

        # Cache miss or expired - fetch from database
        trades = await self.list_open_trades()

        # Update cache
        self._open_trades_cache = trades
        self._open_trades_cache_time = now

        return trades

    async def _invalidate_open_trades_cache(self) -> None:
        """Invalidate the open trades cache (call after trade updates)."""
        self._open_trades_cache = None
        self._open_trades_cache_time = 0

    append_event = append_event
    list_events = list_events

    record_funding = record_funding
    record_funding_history = record_funding_history
    get_total_funding = get_total_funding
    get_funding_breakdown = get_funding_breakdown
    replace_funding_events = replace_funding_events
    get_recent_apy_history = get_recent_apy_history

    save_pnl_snapshot = save_pnl_snapshot
    get_stats = get_stats
    cleanup_old_data = cleanup_old_data

    create_execution_attempt = create_execution_attempt
    update_execution_attempt = update_execution_attempt
    list_execution_attempts = list_execution_attempts
    get_hedge_latency_stats = get_hedge_latency_stats

    # Historical data methods
    insert_funding_candles = insert_funding_candles
    update_volatility_profile = update_volatility_profile
    get_volatility_profile = get_volatility_profile

    # Surge Pro trade methods
    _row_to_surge_trade = _row_to_surge_trade
    _surge_trade_to_row = _surge_trade_to_row
    create_surge_trade = create_surge_trade
    update_surge_trade = update_surge_trade
    get_surge_trade = get_surge_trade
    list_open_surge_trades = list_open_surge_trades

    # Surge Pro risk guard query methods
    get_daily_pnl = get_daily_pnl
    get_hourly_pnl = get_hourly_pnl
    get_recent_trades = get_recent_trades
    get_hourly_fill_rate = get_hourly_fill_rate
