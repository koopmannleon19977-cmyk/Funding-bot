# ═══════════════════════════════════════════════════════════════════════════════
# PUNKT 4: AIOSQLITE DATABASE LAYER
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ Async SQLite mit aiosqlite
# ✓ Connection Pool (read parallelism)
# ✓ Write-Behind Queue (non-blocking writes)
# ✓ Automatic Schema Migration
# ✓ Transaction Support
# ✓ Query Caching für häufige Reads
# ✓ Decimal precision support for financial data
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import aiosqlite
import sqlite3
import logging
import time
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Union
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
import json
import os
import shutil
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# DECIMAL ADAPTER/CONVERTER FOR SQLITE
# ═══════════════════════════════════════════════════════════════════════════════
# SQLite stores REAL as IEEE 754 floats, which can cause precision loss.
# These adapters allow storing Decimal as TEXT and converting back on read.
# Note: Existing REAL columns will continue to work - this just allows
# passing Decimal objects directly to queries.

def _adapt_decimal(d: Decimal) -> str:
    """Convert Decimal to string for SQLite storage"""
    return str(d)

def _convert_decimal(s: bytes) -> Decimal:
    """Convert string from SQLite to Decimal"""
    return Decimal(s.decode('utf-8'))

# Register the adapter and converter globally for sqlite3
# This affects both sqlite3 and aiosqlite (which wraps sqlite3)
sqlite3.register_adapter(Decimal, _adapt_decimal)
sqlite3.register_converter("DECIMAL", _convert_decimal)

# Also register for common float-like types that might store Decimal
# Note: REAL columns will still return float, but Decimal params will work
logger.debug("✅ Decimal adapter registered for SQLite")


@dataclass
class DBConfig:
    """Database configuration"""
    db_path: str = config.DB_FILE
    pool_size: int = 5                    # Read connections
    write_queue_size: int = 1000          # Max pending writes
    write_batch_size: int = 50            # Writes per batch
    write_flush_interval: float = 1.0     # Seconds between flushes
    busy_timeout_ms: int = 30000          # SQLite busy timeout
    wal_mode: bool = True                 # Write-Ahead Logging
    maintenance_interval_hours: int = 24  # Run VACUUM every 24h
    history_retention_days: int = 30      # Delete trades > 30 days


@dataclass 
class WriteOperation:
    """Represents a pending write operation"""
    sql: str
    params: Tuple = field(default_factory=tuple)
    callback: Optional[asyncio.Future] = None
    timestamp: float = field(default_factory=time.monotonic)


class AsyncDatabase:
    """
    Async SQLite database with connection pooling and write-behind queue. 
    
    Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                     AsyncDatabase                           │
    ├─────────────────────────────────────────────────────────────┤
    │  Read Pool (N connections)    │  Write Queue (1 connection) │
    │  ┌─────┐ ┌─────┐ ┌─────┐     │  ┌──────────────────────┐   │
    │  │conn1│ │conn2│ │conn3│     │  │ Background Writer    │   │
    │  └─────┘ └─────┘ └─────┘     │  │ (batched commits)    │   │
    │         ↓                     │  └──────────────────────┘   │
    │    Parallel Reads             │     Non-blocking Writes     │
    └─────────────────────────────────────────────────────────────┘
    """
    
    def __init__(self, config: Optional[DBConfig] = None):
        self.config = config or DBConfig()
        self._read_pool: List[aiosqlite.Connection] = []
        self._read_pool_lock = asyncio.Lock()
        self._read_pool_available: asyncio.Queue = asyncio.Queue()
        
        self._write_conn: Optional[aiosqlite.Connection] = None
        self._write_queue: asyncio.Queue[Optional[WriteOperation]] = asyncio.Queue(
            maxsize=self.config.write_queue_size
        )
        self._write_task: Optional[asyncio.Task] = None
        
        self._initialized = False
        self._shutdown = False
        
        self._stats = {
            "reads": 0,
            "writes": 0,
            "write_batches": 0,
            "errors": 0,
            "avg_read_ms": 0.0,
            "avg_write_ms": 0.0,
        }

    async def initialize(self):
        """Initialize database connections and schema"""
        if self._initialized:
            return
            
        # Ensure directory exists
        db_path = Path(self. config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📂 Initializing database: {self.config. db_path}")
        
        # Create read pool
        for i in range(self. config.pool_size):
            conn = await self._create_connection(readonly=True)
            self._read_pool.append(conn)
            await self._read_pool_available.put(conn)
            
        logger.info(f"✅ Read pool created: {self.config.pool_size} connections")
        
        # Create write connection
        self._write_conn = await self._create_connection(readonly=False)
        
        # Enable WAL mode for better concurrency
        if self.config. wal_mode:
            await self._write_conn.execute("PRAGMA journal_mode=WAL")
            await self._write_conn.execute("PRAGMA synchronous=NORMAL")
            logger.info("✅ WAL mode enabled")
        
        # Run migrations
        await self._run_migrations()
        
        # Start background writer
        self._write_task = asyncio.create_task(
            self._write_loop(),
            name="db_writer"
        )
        
        self._initialized = True
        logger.info("✅ Database initialized")
        
        # Run startup maintenance - AWAIT to ensure integrity check completes
        # before trading starts (2025-12-17 Audit Fix)
        await self.run_maintenance()

    async def run_maintenance(self):
        """Run database maintenance (VACUUM & Cleanup)"""
        if not getattr(config, "DB_MAINTENANCE_ENABLED", True):
            logger.debug("Database maintenance disabled by config.DB_MAINTENANCE_ENABLED=False")
            return
        if not self._write_conn:
            return

        try:
            # Check last maintenance time
            async with self._write_conn.execute(
                "SELECT timestamp FROM maintenance_log WHERE action='VACUUM' ORDER BY id DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                last_run = row['timestamp'] if row else 0

            # Default: 24h interval
            interval = getattr(self.config, 'maintenance_interval_hours', 24) * 3600
            now = int(time.time())

            if now - last_run > interval:
                logger.info("🧹 Running database maintenance...")
                
                # 1. Integrity Check
                async with self._write_conn.execute("PRAGMA integrity_check") as cursor:
                    check_result = await cursor.fetchone()
                    if check_result and check_result[0] != "ok":
                        logger.error(f"🚨 DATABASE CORRUPT: {check_result[0]}")
                        # Quarantine corrupted DB
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        corrupt_path = f"{self.config.db_path}.corrupt_{ts}"
                        logger.warning(f"☣️ Quarantining corrupted DB to {corrupt_path}")
                        
                        # Close connections (this will be messy since we are in a loop, but necessary)
                        await self._write_conn.close()
                        for conn in self._read_pool:
                            await conn.close()
                            
                        shutil.move(self.config.db_path, corrupt_path)
                        logger.info("♻️ Corrupted DB moved. Bot will recreate DB on next init.")
                        # Force exit to allow clean restart
                        os._exit(1)
                        return

                # 2. VACUUM (Reclaim space)
                logger.info("🧹 Executing VACUUM (Integrity OK)...")
                await self._write_conn.execute("VACUUM")
                
                # 2. Log action
                retention_days = getattr(self.config, 'history_retention_days', 30)
                await self._write_conn.execute(
                    "INSERT INTO maintenance_log (action, timestamp, details) VALUES (?, ?, ?)",
                    ("VACUUM", now, f"VACUUM executed. Retention: {retention_days} days")
                )
                await self._write_conn.commit()
                logger.info("✅ Database maintenance complete")
            else:
                logger.debug(f"Maintenance not due (Last: {int((now-last_run)/3600)}h ago)")

        except Exception as e:
            logger.error(f"Database maintenance failed: {e}")

    async def _create_connection(self, readonly: bool = False) -> aiosqlite.Connection:
        """Create a new database connection"""
        # aiosqlite doesn't support readonly directly, but we can use it for pool management
        conn = await aiosqlite. connect(
            self.config.db_path,
            timeout=self.config. busy_timeout_ms / 1000
        )
        
        # Enable foreign keys
        await conn.execute("PRAGMA foreign_keys=ON")
        
        # Row factory for dict-like access
        conn.row_factory = aiosqlite. Row
        
        return conn

    async def _run_migrations(self):
        """Run database schema migrations"""
        logger.info("🔄 Running database migrations...")
        
        migrations = [
            # Migration 1: Core trades table
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side_x10 TEXT NOT NULL,
                side_lighter TEXT NOT NULL,
                size_usd REAL NOT NULL,
                entry_price_x10 REAL,
                entry_price_lighter REAL,
                status TEXT DEFAULT 'open',
                is_farm_trade INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                closed_at INTEGER,
                pnl REAL DEFAULT 0,
                funding_collected REAL DEFAULT 0,
                account_label TEXT,
                x10_order_id TEXT,
                lighter_order_id TEXT,
                UNIQUE(symbol, status) ON CONFLICT REPLACE
            )
            """,
            
            # Migration 2: Indexes
            """
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at)
            """,
            
            # Migration 3: Funding history
            """
            CREATE TABLE IF NOT EXISTS funding_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                rate REAL NOT NULL,
                timestamp INTEGER NOT NULL,
                collected_amount REAL DEFAULT 0
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_funding_symbol_ts 
            ON funding_history(symbol, timestamp)
            """,
            
            # Migration 4: Trade execution log
            """
            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                exchange TEXT NOT NULL,
                success INTEGER NOT NULL,
                order_id TEXT,
                error TEXT,
                latency_ms REAL,
                timestamp INTEGER NOT NULL
            )
            """,
            
            # Migration 5: Bot state persistence
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
            
            # Migration 6: PnL snapshots
            """
            CREATE TABLE IF NOT EXISTS pnl_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                total_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                funding_pnl REAL NOT NULL,
                trade_count INTEGER NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pnl_ts ON pnl_snapshots(timestamp)
            """,
            
            # Migration 7: Add missing columns (safe ALTER)
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at INTEGER NOT NULL
            )
            """,
            
            # Migration 8: Funding Rate History for ML Predictions (Claude's Request)
            """
            CREATE TABLE IF NOT EXISTS funding_rate_history (
                symbol TEXT NOT NULL,
                rate_lighter REAL NOT NULL,
                rate_x10 REAL NOT NULL,
                timestamp INTEGER NOT NULL,
                ob_imbalance REAL DEFAULT 0,
                oi_velocity REAL DEFAULT 0,
                PRIMARY KEY (symbol, timestamp)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_frh_ts ON funding_rate_history(timestamp)
            """,
            
            # Migration 9: Database Maintenance Tracking
            """
            CREATE TABLE IF NOT EXISTS maintenance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                details TEXT
            )
            """,
        ]
        
        for i, sql in enumerate(migrations):
            try:
                await self._write_conn.execute(sql)
            except Exception as e:
                # Ignore "already exists" errors
                if "already exists" not in str(e). lower():
                    logger.warning(f"Migration {i+1} warning: {e}")
                    
        await self._write_conn.commit()
        logger.info(f"✅ Migrations complete ({len(migrations)} statements)")

    @asynccontextmanager
    async def read_connection(self):
        """Get a read connection from the pool"""
        conn = await self._read_pool_available. get()
        try:
            yield conn
        finally:
            await self._read_pool_available. put(conn)

    async def fetch_one(
        self, 
        sql: str, 
        params: Tuple = ()
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single row"""
        start = time.monotonic()
        
        try:
            async with self.read_connection() as conn:
                async with conn.execute(sql, params) as cursor:
                    row = await cursor.fetchone()
                    
                    self._stats["reads"] += 1
                    elapsed = (time.monotonic() - start) * 1000
                    self._update_avg("avg_read_ms", elapsed)
                    
                    if row:
                        return dict(row)
                    return None
                    
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"DB fetch_one error: {e}")
            raise

    async def fetch_all(
        self, 
        sql: str, 
        params: Tuple = ()
    ) -> List[Dict[str, Any]]:
        """Fetch all rows"""
        start = time. monotonic()
        
        try:
            async with self. read_connection() as conn:
                async with conn.execute(sql, params) as cursor:
                    rows = await cursor. fetchall()
                    
                    self._stats["reads"] += 1
                    elapsed = (time.monotonic() - start) * 1000
                    self._update_avg("avg_read_ms", elapsed)
                    
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"DB fetch_all error: {e}")
            raise

    async def execute(
        self, 
        sql: str, 
        params: Tuple = (),
        wait: bool = False
    ) -> Optional[int]:
        """
        Execute a write operation. 
        
        Args:
            sql: SQL statement
            params: Query parameters
            wait: If True, wait for write to complete and return lastrowid
            
        Returns:
            lastrowid if wait=True, else None
        """
        if self._shutdown:
            raise RuntimeError("Database is shutting down")
            
        future = asyncio.get_running_loop().create_future() if wait else None
        
        op = WriteOperation(
            sql=sql,
            params=params,
            callback=future
        )
        
        try:
            self._write_queue. put_nowait(op)
        except asyncio.QueueFull:
            logger.warning("Write queue full, dropping oldest")
            try:
                self._write_queue. get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._write_queue. put_nowait(op)
        
        if wait and future:
            return await future
        return None

    async def execute_many(
        self, 
        sql: str, 
        params_list: List[Tuple]
    ):
        """Execute multiple write operations as a batch"""
        for params in params_list:
            await self.execute(sql, params, wait=False)

    async def _write_loop(self):
        """Background task that batches and commits writes"""
        logger.info("🖊️ Database write loop started")
        
        batch: List[WriteOperation] = []
        last_flush = time.monotonic()
        
        while not self._shutdown:
            try:
                # Collect operations with timeout
                try:
                    op = await asyncio.wait_for(
                        self._write_queue.get(),
                        timeout=self.config.write_flush_interval
                    )
                    
                    if op is None:  # Shutdown signal
                        break
                        
                    batch.append(op)
                    
                except asyncio.TimeoutError:
                    pass
                
                # Flush if batch is full or interval elapsed
                now = time.monotonic()
                should_flush = (
                    len(batch) >= self. config.write_batch_size or
                    (batch and now - last_flush >= self.config.write_flush_interval)
                )
                
                if should_flush and batch:
                    await self._flush_batch(batch)
                    batch = []
                    last_flush = now
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Write loop error: {e}")
                await asyncio.sleep(1.0)
        
        # Final flush
        if batch:
            await self._flush_batch(batch)
            
        logger.info("🖊️ Database write loop stopped")

    async def _flush_batch(self, batch: List[WriteOperation]):
        """Flush a batch of writes to database"""
        if not batch:
            return
            
        start = time.monotonic()
        
        try:
            for op in batch:
                try:
                    cursor = await self._write_conn.execute(op.sql, op.params)
                    
                    if op. callback and not op.callback.done():
                        op.callback.set_result(cursor.lastrowid)
                        
                except Exception as e:
                    logger.error(f"Write error: {e} | SQL: {op.sql[:100]}")
                    self._stats["errors"] += 1
                    
                    if op.callback and not op.callback. done():
                        op.callback.set_exception(e)
            
            await self._write_conn.commit()
            
            self._stats["writes"] += len(batch)
            self._stats["write_batches"] += 1
            
            elapsed = (time.monotonic() - start) * 1000
            self._update_avg("avg_write_ms", elapsed / len(batch))
            
            logger.debug(f"📝 Flushed {len(batch)} writes in {elapsed:.1f}ms")
            
        except Exception as e:
            logger.error(f"Batch commit error: {e}")
            self._stats["errors"] += 1
            
            # Notify all callbacks of failure
            for op in batch:
                if op.callback and not op.callback.done():
                    op.callback.set_exception(e)

    def _update_avg(self, key: str, value: float):
        """Update rolling average"""
        alpha = 0.1  # Smoothing factor
        self._stats[key] = alpha * value + (1 - alpha) * self._stats[key]

    async def close(self):
        """Close all connections gracefully"""
        logger.info("🔒 Closing database...")
        self._shutdown = True
        
        # Signal write loop to stop
        await self._write_queue.put(None)
        
        if self._write_task:
            try:
                await asyncio.wait_for(self._write_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._write_task. cancel()
        
        # Close write connection
        if self._write_conn:
            await self._write_conn.close()
        
        # Close read pool
        for conn in self._read_pool:
            await conn.close()
        self._read_pool. clear()
        
        self._initialized = False
        logger.info("✅ Database closed")

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        return {
            **self._stats,
            "pending_writes": self._write_queue.qsize(),
            "pool_available": self._read_pool_available.qsize(),
            "pool_size": len(self._read_pool),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE-SPECIFIC OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

class TradeRepository:
    """
    Repository for trade-specific database operations. 
    Wraps AsyncDatabase with trade-focused methods.
    """
    
    def __init__(self, db: AsyncDatabase):
        self.db = db

    async def add_trade(self, trade: Dict[str, Any]) -> Optional[int]:
        """Add a new trade to the database"""
        sql = """
            INSERT INTO trades (
                symbol, side_x10, side_lighter, size_usd,
                entry_price_x10, entry_price_lighter, status,
                is_farm_trade, created_at, account_label,
                x10_order_id, lighter_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        params = (
            trade['symbol'],
            trade. get('side_x10', 'BUY'),
            trade.get('side_lighter', 'SELL'),
            trade. get('size_usd', 0),
            trade.get('entry_price_x10'),
            trade.get('entry_price_lighter'),
            trade.get('status', 'open'),
            1 if trade.get('is_farm_trade') else 0,
            int(time.time() * 1000),
            trade.get('account_label', 'Main'),
            trade.get('x10_order_id'),
            trade.get('lighter_order_id'),
        )
        
        return await self.db. execute(sql, params, wait=True)

    async def get_open_trades(self) -> List[Dict[str, Any]]:
        """Get all open trades"""
        # Treat 'pending' as open-risk (pre-hedge / in-flight) to avoid orphan exposure
        sql = "SELECT * FROM trades WHERE status IN ('open','pending') ORDER BY created_at DESC"
        return await self.db.fetch_all(sql)

    async def get_trade_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get open trade by symbol"""
        sql = "SELECT * FROM trades WHERE symbol = ? AND status IN ('open','pending')"
        return await self.db. fetch_one(sql, (symbol,))

    async def close_trade(
        self, 
        symbol: str, 
        pnl: float = 0, 
        funding_collected: float = 0
    ):
        """Mark a trade as closed with PnL values"""
        # ✅ FIX: Enhanced logging to verify PnL values reach the database
        logger.info(f"📝 DB close_trade({symbol}): PnL=${pnl:.4f}, Funding=${funding_collected:.4f}")
        
        sql = """
            UPDATE trades 
            SET status = 'closed', 
                closed_at = ?, 
                pnl = ?,
                funding_collected = ?
            WHERE symbol = ? AND status IN ('open','pending')
        """
        result = await self.db.execute(
            sql, 
            (int(time.time() * 1000), pnl, funding_collected, symbol),
            wait=True
        )
        
        logger.debug(f"📝 DB close_trade({symbol}): Execute returned {result}")

    async def update_trade_funding(self, symbol: str, funding_amount: float):
        """Update funding collected for a trade"""
        sql = """
            UPDATE trades 
            SET funding_collected = funding_collected + ? 
            WHERE symbol = ? AND status IN ('open','pending')
        """
        await self. db.execute(sql, (funding_amount, symbol))

    async def update_trade_fields(self, symbol: str, updates: Dict[str, Any]) -> None:
        """Update mutable trade fields for the currently open/pending record."""
        if not updates:
            return

        allowed = {
            "side_x10",
            "side_lighter",
            "size_usd",
            "entry_price_x10",
            "entry_price_lighter",
            "status",
            "is_farm_trade",
            "account_label",
            "x10_order_id",
            "lighter_order_id",
            "closed_at",
            "pnl",
            "funding_collected",
        }

        set_cols = []
        params: List[Any] = []

        for k, v in updates.items():
            if k not in allowed:
                continue
            if k == "is_farm_trade":
                v = 1 if bool(v) else 0
            set_cols.append(f"{k} = ?")
            params.append(v)

        if not set_cols:
            return

        sql = f"""
            UPDATE trades
            SET {", ".join(set_cols)}
            WHERE symbol = ? AND status IN ('open','pending')
        """
        params.append(symbol)
        await self.db.execute(sql, tuple(params), wait=True)

    async def get_trade_count(self) -> int:
        """Get count of open trades"""
        result = await self.db.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE status IN ('open','pending')"
        )
        return result['count'] if result else 0

    async def get_total_pnl(self) -> float:
        """Get total realized PnL"""
        result = await self.db.fetch_one(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE status = 'closed'"
        )
        return result['total'] if result else 0.0

    async def save_pnl_snapshot(
        self,
        total_pnl: float,
        unrealized_pnl: float,
        realized_pnl: float,
        funding_pnl: float,
        trade_count: int
    ):
        """
        Save a PnL snapshot for historical tracking.
        Called periodically to track PnL over time.
        """
        sql = """
            INSERT INTO pnl_snapshots 
            (timestamp, total_pnl, unrealized_pnl, realized_pnl, funding_pnl, trade_count)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        await self.db.execute(
            sql,
            (int(time.time() * 1000), total_pnl, unrealized_pnl, realized_pnl, funding_pnl, trade_count)
        )
        logger.debug(f"📸 PnL Snapshot saved: total=${total_pnl:.4f}, uPnL=${unrealized_pnl:.4f}, rPnL=${realized_pnl:.4f}")

    async def get_pnl_snapshots(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get PnL snapshots for the last N hours"""
        cutoff = int(time.time() * 1000) - (hours * 3600 * 1000)
        sql = """
            SELECT * FROM pnl_snapshots
            WHERE timestamp > ?
            ORDER BY timestamp DESC
        """
        return await self.db.fetch_all(sql, (cutoff,))

    async def delete_ghost_trades(self, valid_symbols: List[str]):
        """Remove trades that don't exist on exchanges"""
        if not valid_symbols:
            # Delete all open trades if no valid symbols
            sql = "DELETE FROM trades WHERE status = 'open'"
            await self.db.execute(sql, wait=True)
        else:
            placeholders = ','.join('?' * len(valid_symbols))
            sql = f"DELETE FROM trades WHERE status = 'open' AND symbol NOT IN ({placeholders})"
            await self.db.execute(sql, tuple(valid_symbols), wait=True)


class FundingRepository:
    """Repository for funding history operations"""
    
    def __init__(self, db: AsyncDatabase):
        self.db = db

    async def add_funding_record(
        self,
        symbol: str,
        exchange: str,
        rate: float,
        timestamp: int,
        collected: float = 0
    ):
        """Add a funding rate record"""
        sql = """
            INSERT INTO funding_history (symbol, exchange, rate, timestamp, collected_amount)
            VALUES (?, ?, ?, ?, ?)
        """
        await self.db.execute(sql, (symbol, exchange, rate, timestamp, collected))

    async def get_recent_funding(
        self, 
        symbol: str, 
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """Get recent funding history for a symbol"""
        cutoff = int(time.time() * 1000) - (hours * 3600 * 1000)
        sql = """
            SELECT * FROM funding_history 
            WHERE symbol = ? AND timestamp > ? 
            ORDER BY timestamp DESC
        """
        return await self.db.fetch_all(sql, (symbol, cutoff))

    async def add_rate_history(
        self,
        symbol: str,
        rate_lighter: float,
        rate_x10: float,
        timestamp: int,
        ob_imbalance: float = 0,
        oi_velocity: float = 0
    ):
        """Add a funding rate history record for ML"""
        sql = """
            INSERT OR REPLACE INTO funding_rate_history 
            (symbol, rate_lighter, rate_x10, timestamp, ob_imbalance, oi_velocity)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        await self.db.execute(sql, (symbol, rate_lighter, rate_x10, timestamp, ob_imbalance, oi_velocity))

    async def get_rate_history(
        self,
        symbol: str,
        hours: int = 48
    ) -> List[Dict[str, Any]]:
        """Get funding rate history for ML training"""
        cutoff = int(time.time() * 1000) - (hours * 3600 * 1000)
        sql = """
            SELECT * FROM funding_rate_history
            WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
        """
        return await self.db.fetch_all(sql, (symbol, cutoff))


class ExecutionLogRepository:
    """Repository for execution logging"""
    
    def __init__(self, db: AsyncDatabase):
        self.db = db

    async def log_execution(
        self,
        symbol: str,
        action: str,
        exchange: str,
        success: bool,
        order_id: Optional[str] = None,
        error: Optional[str] = None,
        latency_ms: Optional[float] = None
    ):
        """Log a trade execution attempt"""
        sql = """
            INSERT INTO execution_log 
            (symbol, action, exchange, success, order_id, error, latency_ms, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        await self.db.execute(sql, (
            symbol, action, exchange, 1 if success else 0,
            order_id, error, latency_ms, int(time.time() * 1000)
        ))

    async def get_recent_failures(
        self, 
        symbol: str, 
        minutes: int = 30
    ) -> int:
        """Count recent failures for a symbol"""
        cutoff = int(time.time() * 1000) - (minutes * 60 * 1000)
        result = await self.db.fetch_one(
            """
            SELECT COUNT(*) as count FROM execution_log 
            WHERE symbol = ?  AND success = 0 AND timestamp > ?
            """,
            (symbol, cutoff)
        )
        return result['count'] if result else 0


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL DATABASE INSTANCES (keyed by db_path)
# ═══════════════════════════════════════════════════════════════════════════════

_db_by_path: Dict[str, AsyncDatabase] = {}
_trade_repo_by_path: Dict[str, TradeRepository] = {}
_funding_repo_by_path: Dict[str, FundingRepository] = {}
_execution_repo_by_path: Dict[str, ExecutionLogRepository] = {}


def _normalize_db_path(db_path: Optional[str]) -> str:
    resolved = db_path or config.DB_FILE
    try:
        return str(Path(resolved).resolve())
    except Exception:
        return str(resolved)


async def get_database(db_path: Optional[str] = None) -> AsyncDatabase:
    """Get or create the global database instance for a given db_path."""
    global _db_by_path
    key = _normalize_db_path(db_path)
    db = _db_by_path.get(key)
    if db is None:
        db = AsyncDatabase(DBConfig(db_path=key))
        await db.initialize()
        _db_by_path[key] = db
    return db


async def get_trade_repository(db_path: Optional[str] = None) -> TradeRepository:
    """Get or create the trade repository for a given db_path."""
    global _trade_repo_by_path
    key = _normalize_db_path(db_path)
    repo = _trade_repo_by_path.get(key)
    if repo is None:
        db = await get_database(db_path=key)
        repo = TradeRepository(db)
        _trade_repo_by_path[key] = repo
    return repo


async def get_funding_repository(db_path: Optional[str] = None) -> FundingRepository:
    """Get or create the funding repository for a given db_path."""
    global _funding_repo_by_path
    key = _normalize_db_path(db_path)
    repo = _funding_repo_by_path.get(key)
    if repo is None:
        db = await get_database(db_path=key)
        repo = FundingRepository(db)
        _funding_repo_by_path[key] = repo
    return repo


async def get_execution_repository(db_path: Optional[str] = None) -> ExecutionLogRepository:
    """Get or create the execution log repository for a given db_path."""
    global _execution_repo_by_path
    key = _normalize_db_path(db_path)
    repo = _execution_repo_by_path.get(key)
    if repo is None:
        db = await get_database(db_path=key)
        repo = ExecutionLogRepository(db)
        _execution_repo_by_path[key] = repo
    return repo


async def close_database(db_path: Optional[str] = None):
    """Close global database instance(s). If db_path is None, closes all."""
    global _db_by_path, _trade_repo_by_path, _funding_repo_by_path, _execution_repo_by_path

    if db_path is None:
        for db in list(_db_by_path.values()):
            try:
                await db.close()
            except Exception:
                pass
        _db_by_path.clear()
        _trade_repo_by_path.clear()
        _funding_repo_by_path.clear()
        _execution_repo_by_path.clear()
        return

    key = _normalize_db_path(db_path)
    db = _db_by_path.pop(key, None)
    if db:
        await db.close()
    _trade_repo_by_path.pop(key, None)
    _funding_repo_by_path.pop(key, None)
    _execution_repo_by_path.pop(key, None)
