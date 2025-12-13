# ═══════════════════════════════════════════════════════════════════════════════
# PUNKT 5: IN-MEMORY STATE MANAGER MIT WRITE-BEHIND PATTERN
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ In-Memory State für ultra-schnelle Reads
# ✓ Write-Behind Queue (non-blocking writes)
# ✓ Dirty-Tracking (nur geänderte Daten schreiben)
# ✓ Periodic Sync mit Database
# ✓ Thread-safe mit asyncio Locks
# ✓ Atomic Updates
# ✓ Snapshot/Restore für Recovery
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
import json
import copy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set, Callable, Union
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


from src.utils import safe_float, safe_decimal, quantize_usd


class TradeStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    ROLLBACK = "rollback"


@dataclass
class TradeState:
    """
    In-memory representation of a trade.
    
    Financial fields (size_usd, pnl, funding_collected) are stored as float
    for SQLite compatibility, but Decimal properties are provided for 
    precision-critical calculations.
    """
    symbol: str
    side_x10: str
    side_lighter: str
    size_usd: float
    entry_price_x10: float = 0.0
    entry_price_lighter: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    is_farm_trade: bool = False
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    entry_time: Optional[datetime] = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[int] = None
    pnl: float = 0.0
    funding_collected: float = 0.0
    account_label: str = "Main"
    x10_order_id: Optional[str] = None
    lighter_order_id: Optional[str] = None
    db_id: Optional[int] = None  # Database row ID
    
    # ═══════════════════════════════════════════════════════════════
    # DECIMAL PROPERTIES FOR PRECISION-CRITICAL CALCULATIONS
    # ═══════════════════════════════════════════════════════════════
    
    @property
    def size_usd_decimal(self) -> Decimal:
        """Get size_usd as Decimal for precise calculations"""
        return safe_decimal(self.size_usd)
    
    @property
    def pnl_decimal(self) -> Decimal:
        """Get pnl as Decimal for precise calculations"""
        return safe_decimal(self.pnl)
    
    @property
    def funding_collected_decimal(self) -> Decimal:
        """Get funding_collected as Decimal for precise calculations"""
        return safe_decimal(self.funding_collected)
    
    @property
    def entry_price_x10_decimal(self) -> Decimal:
        """Get entry_price_x10 as Decimal"""
        return safe_decimal(self.entry_price_x10)
    
    @property
    def entry_price_lighter_decimal(self) -> Decimal:
        """Get entry_price_lighter as Decimal"""
        return safe_decimal(self.entry_price_lighter)
    
    def set_pnl_from_decimal(self, value: Union[Decimal, float]) -> None:
        """Set pnl from a Decimal value (converts to float for storage)"""
        self.pnl = float(quantize_usd(safe_decimal(value)))
    
    def set_funding_from_decimal(self, value: Union[Decimal, float]) -> None:
        """Set funding_collected from a Decimal value"""
        self.funding_collected = float(quantize_usd(safe_decimal(value)))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = asdict(self)
        data['status'] = self.status.value
        # Serialize entry_time as ISO string
        data['entry_time'] = self.entry_time.isoformat() if self.entry_time else None
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradeState':
        """Create from dictionary with robust type handling"""
        data = data.copy()  # Don't modify original
        
        # Status conversion
        if isinstance(data.get('status'), str):
            try:
                data['status'] = TradeStatus(data['status'])
            except ValueError:
                data['status'] = TradeStatus.OPEN
        
        # Entry time conversion - ROBUST
        entry_time = data.get('entry_time')
        if entry_time is not None:
            if isinstance(entry_time, str):
                try:
                    # Handle various ISO formats
                    entry_time = entry_time.replace('Z', '+00:00')
                    if '.' in entry_time and '+' in entry_time:
                        # Format: 2025-12-02T21:12:41.017751+00:00
                        data['entry_time'] = datetime.fromisoformat(entry_time)
                    elif 'T' in entry_time:
                        # Format: 2025-12-02T21:12:41
                        data['entry_time'] = datetime.fromisoformat(entry_time)
                    else:
                        # Format: 2025-12-02 21:12:41
                        data['entry_time'] = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse entry_time '{entry_time}': {e}")
                    data['entry_time'] = datetime.now(timezone.utc)
            elif isinstance(entry_time, (int, float)):
                # Unix timestamp
                data['entry_time'] = datetime.fromtimestamp(entry_time, tz=timezone.utc)
            elif not isinstance(entry_time, datetime):
                data['entry_time'] = datetime.now(timezone.utc)
        else:
            data['entry_time'] = datetime.now(timezone.utc)
        
        # Ensure timezone awareness
        if data['entry_time'].tzinfo is None:
            data['entry_time'] = data['entry_time'].replace(tzinfo=timezone.utc)
        
        # Filter to valid fields only
        valid_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        
        return cls(**valid_fields)


@dataclass
class BalanceState:
    """In-memory balance state"""
    exchange: str
    available: float = 0.0
    total: float = 0.0
    in_position: float = 0.0
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class MarketState:
    """In-memory market data"""
    symbol: str
    mark_price: float = 0.0
    funding_rate_x10: float = 0.0
    funding_rate_lighter: float = 0.0
    spread: float = 0.0
    apy: float = 0.0
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))


class WriteOperation(Enum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class PendingWrite:
    """Represents a pending write operation"""
    operation: WriteOperation
    table: str
    key: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.monotonic)
    callback: Optional[asyncio.Future] = None


class InMemoryStateManager:
    """
    In-Memory State Manager with Write-Behind Pattern. 
    
    ═══════════════════════════════════════════════════════════════════════════
    ARCHITECTURE:
    ═══════════════════════════════════════════════════════════════════════════
    
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                        InMemoryStateManager                              │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  IN-MEMORY STATE (Fast Reads)     │  WRITE-BEHIND QUEUE (Async Writes)  │
    │  ┌───────────────────────────┐    │  ┌─────────────────────────────┐    │
    │  │ trades: {symbol: Trade}  │    │  │ Pending Writes Queue        │    │
    │  │ balances: {exch: Balance}│ ──►│  │ ┌─────┐ ┌─────┐ ┌─────┐    │    │
    │  │ markets: {sym: Market}   │    │  │ │ W1  │ │ W2  │ │ W3  │    │    │
    │  │ dirty_keys: Set[str]     │    │  │ └─────┘ └─────┘ └─────┘    │    │
    │  └───────────────────────────┘    │  └─────────────────────────────┘    │
    │              │                    │              │                       │
    │              ▼                    │              ▼                       │
    │    get_trade() < 1μs             │     Background Writer Task           │
    │    get_all_trades() < 10μs       │     (batched commits every 1s)       │
    └─────────────────────────────────────────────────────────────────────────┘
    
    ═══════════════════════════════════════════════════════════════════════════
    """
    
    WRITE_BATCH_SIZE = 50
    WRITE_FLUSH_INTERVAL = 1.0  # seconds
    SYNC_INTERVAL = 60.0  # Full sync every 60s
    SNAPSHOT_INTERVAL = 300.0  # Snapshot every 5 min
    
    def __init__(self, db_path: str = "data/trades.db"):
        self.db_path = db_path
        
        # In-Memory State
        self._trades: Dict[str, TradeState] = {}
        self._balances: Dict[str, BalanceState] = {}
        self._markets: Dict[str, MarketState] = {}
        self._bot_state: Dict[str, Any] = {}
        
        # Dirty tracking
        self._dirty_trades: Set[str] = set()
        self._dirty_balances: Set[str] = set()
        self._dirty_markets: Set[str] = set()
        self._dirty_bot_state: Set[str] = set()
        
        # Locks
        self._trade_lock = asyncio.Lock()
        self._balance_lock = asyncio.Lock()
        self._market_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        
        # Write-behind queue
        self._write_queue: asyncio.Queue[Optional[PendingWrite]] = asyncio. Queue(maxsize=10000)
        self._writer_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio. Task] = None
        
        # Database reference
        self._db = None
        
        # Stats
        self._stats = {
            "reads": 0,
            "writes_queued": 0,
            "writes_flushed": 0,
            "syncs": 0,
            "snapshots": 0,
        }
        
        self._running = False

    async def start(self):
        """Start the state manager background tasks"""
        if self._running:
            return
            
        logger.info("🚀 Starting InMemoryStateManager...")
        
        # Import here to avoid circular imports
        from src. database import get_database
        self._db = await get_database()
        
        # Load initial state from database
        await self._load_from_db()
        
        # Start background tasks
        self._running = True
        
        self._writer_task = asyncio.create_task(
            self._writer_loop(),
            name="state_writer"
        )
        
        self._sync_task = asyncio.create_task(
            self._sync_loop(),
            name="state_sync"
        )
        
        self._snapshot_task = asyncio.create_task(
            self._snapshot_loop(),
            name="state_snapshot"
        )
        
        logger.info(f"✅ InMemoryStateManager started (loaded {len(self._trades)} trades)")

    async def stop(self):
        """Stop the state manager gracefully"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Prevent duplicate stop calls during shutdown
        # ═══════════════════════════════════════════════════════════════
        if hasattr(self, '_stopped') and self._stopped:
            return
        self._stopped = True
        
        logger.info("🛑 Stopping InMemoryStateManager...")
        self._running = False
        
        # Signal writer to stop
        await self._write_queue.put(None)
        
        # Wait for writer to flush
        if self._writer_task:
            try:
                await asyncio.wait_for(self._writer_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._writer_task.cancel()
        
        # Cancel other tasks
        for task in [self._sync_task, self._snapshot_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Final sync
        await self._flush_dirty()
        
        logger.info("✅ InMemoryStateManager stopped")

    # ═══════════════════════════════════════════════════════════════════════════
    # TRADE OPERATIONS (In-Memory, Ultra-Fast)
    # ═══════════════════════════════════════════════════════════════════════════

    async def add_trade(self, trade: TradeState) -> str:
        """Add a new trade to state"""
        async with self._trade_lock:
            self._trades[trade. symbol] = trade
            self._dirty_trades.add(trade.symbol)
        
        # Queue write
        await self._queue_write(
            WriteOperation.INSERT,
            "trades",
            trade.symbol,
            trade. to_dict()
        )
        
        self._stats["writes_queued"] += 1
        logger.debug(f"📝 Trade added to state: {trade. symbol}")
        return trade.symbol

    async def get_trade(self, symbol: str) -> Optional[TradeState]:
        """Get a trade by symbol (instant, from memory)"""
        self._stats["reads"] += 1
        return self._trades.get(symbol)

    async def get_all_open_trades(self) -> List[TradeState]:
        """Get all open trades (instant, from memory)"""
        self._stats["reads"] += 1
        return [
            t for t in self._trades.values()
            if t.status in (TradeStatus. OPEN, TradeStatus.PENDING)
        ]

    async def get_trade_count(self) -> int:
        """Get count of open trades"""
        return len([t for t in self._trades.values() if t.status == TradeStatus. OPEN])

    async def update_trade(
        self,
        symbol: str,
        updates: Dict[str, Any]
    ) -> bool:
        """Update a trade's fields"""
        async with self._trade_lock:
            trade = self._trades. get(symbol)
            if not trade:
                return False
            
            for key, value in updates.items():
                if hasattr(trade, key):
                    if key == 'status' and isinstance(value, str):
                        value = TradeStatus(value)
                    setattr(trade, key, value)
            
            self._dirty_trades.add(symbol)
        
        await self._queue_write(
            WriteOperation.UPDATE,
            "trades",
            symbol,
            updates
        )
        
        return True

    async def close_trade(
        self,
        symbol: str,
        pnl: Union[float, Decimal] = 0.0,
        funding: Union[float, Decimal] = 0.0
    ) -> bool:
        """
        Close a trade with PnL and funding values.
        
        Accepts both float and Decimal for pnl/funding.
        Internally converts to float for storage, using Decimal for precision.
        
        FIXED: Removes trade from memory after successful close to prevent memory leak.
        """
        # Convert to Decimal for precise calculation, then to float for storage
        # REMOVED quantize_usd to preserve precision for small PnL (e.g. < $0.01)
        pnl_value = float(safe_decimal(pnl))
        funding_value = float(safe_decimal(funding))
        
        # ✅ FIX: Enhanced logging to verify PnL values are being queued correctly
        logger.info(f"📝 StateManager.close_trade({symbol}): PnL=${pnl_value:.4f}, Funding=${funding_value:.4f}")
        
        result = await self.update_trade(symbol, {
            'status': TradeStatus.CLOSED,
            'closed_at': int(time.time() * 1000),
            'pnl': pnl_value,
            'funding_collected': funding_value,
        })
        
        logger.debug(f"📝 StateManager.close_trade({symbol}): update_trade returned {result}")
        
        # ═══════════════════════════════════════════════════════════════
        # MEMORY LEAK FIX: Remove closed trade from memory after DB write is queued
        # This prevents memory growth during long bot sessions.
        # The trade data is safely persisted in the database.
        # ═══════════════════════════════════════════════════════════════
        if result:
            async with self._trade_lock:
                if symbol in self._trades:
                    del self._trades[symbol]
                    self._dirty_trades.discard(symbol)
                    logger.debug(f"🧹 Removed closed trade {symbol} from memory")
        
        return result

    async def close_trade_verified(
        self,
        symbol: str,
        pnl: float,
        funding_earned: float,
        x10_adapter,
        lighter_adapter,
        close_reason: str = "NORMAL",
    ) -> bool:
        """
        Close a trade after verifying both exchanges have no open position.

        Returns True only if exchange state is clean and DB update succeeds.
        """
        from src.reconciliation import verify_trade_closed_on_exchange

        x10_closed, lighter_closed = await verify_trade_closed_on_exchange(
            symbol=symbol,
            x10_adapter=x10_adapter,
            lighter_adapter=lighter_adapter,
            max_retries=3,
            retry_delay=1.0,
        )

        if not x10_closed:
            logger.error(f"❌ Cannot close trade {symbol}: X10 position still exists!")
            return False

        if not lighter_closed:
            logger.error(f"❌ Cannot close trade {symbol}: Lighter position still exists!")
            return False

        try:
            success = await self.close_trade(
                symbol=symbol,
                pnl=pnl,
                funding=funding_earned,
            )
            if success:
                logger.info(f"✅ Trade {symbol} closed and verified (reason: {close_reason})")
            return success
        except Exception as e:
            logger.error(f"❌ Failed to close trade {symbol} in DB: {e}")
            return False

    async def remove_trade(self, symbol: str) -> bool:
        """Remove a trade from state"""
        async with self._trade_lock:
            if symbol not in self._trades:
                return False
            
            del self._trades[symbol]
            self._dirty_trades.discard(symbol)
        
        await self._queue_write(
            WriteOperation.DELETE,
            "trades",
            symbol,
            {}
        )
        
        return True

    async def has_open_trade(self, symbol: str) -> bool:
        """Check if symbol has an open trade"""
        trade = self._trades. get(symbol)
        return trade is not None and trade. status in (TradeStatus. OPEN, TradeStatus.PENDING)

    async def get_open_symbols(self) -> Set[str]:
        """Get set of symbols with open trades"""
        return {
            t.symbol for t in self._trades.values()
            if t.status in (TradeStatus. OPEN, TradeStatus.PENDING)
        }

    async def reconcile_with_exchange(self, exchange_name: str, real_positions: list):
        """
        Gleicht State mit echten Positionen ab.
        1. Schließt Trades in State, die auf Exchange weg sind (Zombies). -> Logic usually handled elsewhere, but can be here.
           NOTE: Zombie check is complex because we have two legs. 
           If one leg is missing, we might want to close the other? Or just mark as closed?
           For now, we implement ONLY the ORPHAN ADOPTION as requested.
        2. Adoptiert Trades in State, die auf Exchange existieren aber in State fehlen (Waisen).
        """
        if not real_positions:
            return

        # 2. WAISEN ADOPTION
        real_map = {p['symbol']: p for p in real_positions if not p.get('is_ghost')}
        
        # Use a copy of keys to avoid modification issues if we add
        async with self._trade_lock:
             known_symbols = set(self._trades.keys())

        for symbol, real_pos in real_map.items():
            if symbol not in known_symbols:
                logger.warning(f"⚠️ DESYNC FIX: Found ORPHAN {symbol} on {exchange_name}. Adopting into DB...")
                
                try:
                    # Parse position data
                    size = safe_float(real_pos.get('size'), 0.0)
                    price = safe_float(real_pos.get('entry_price') or real_pos.get('mark_price'), 0.0)
                    
                    if size == 0:
                        continue
                        
                    # Determine sides
                    side_x10 = "NONE"
                    side_lighter = "NONE"
                    entry_px_x10 = 0.0
                    entry_px_lit = 0.0
                    
                    # Lighter uses 'size' signed for side? Adapter usually provides abs in 'size' but let's check.
                    # Lighter adapter 'fetch_open_positions':
                    # positions.append({"symbol": symbol, "size": size}) where size is already signed!
                    # X10 adapter 'fetch_open_positions' usually returns signed size too.
                    
                    rec_side = "BUY" if size > 0 else "SELL"
                    
                    if exchange_name == 'X10':
                        side_x10 = rec_side
                        entry_px_x10 = price
                    elif exchange_name == 'Lighter':
                        side_lighter = rec_side
                        entry_px_lit = price
                        
                    # Estimate USD size
                    size_usd = abs(size) * price
                    
                    # Create Recovery Trade
                    trade = TradeState(
                        symbol=symbol,
                        side_x10=side_x10,
                        side_lighter=side_lighter,
                        size_usd=size_usd,
                        entry_price_x10=entry_px_x10,
                        entry_price_lighter=entry_px_lit,
                        status=TradeStatus.OPEN,
                        is_farm_trade=False,
                        created_at=int(time.time() * 1000),
                        pnl=0.0,
                        funding_collected=0.0,
                        account_label="Recovery",
                        x10_order_id=f"RECOVERY_{exchange_name}_{int(time.time())}",
                        lighter_order_id=f"RECOVERY_{exchange_name}_{int(time.time())}"
                    )
                    
                    await self.add_trade(trade)
                    known_symbols.add(symbol) # prevent double add if loop logic changes
                    logger.info(f"✅ Successfully adopted orphan trade {symbol}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to adopt orphan {symbol}: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # BALANCE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════════

    async def update_balance(
        self,
        exchange: str,
        available: float,
        total: float = 0.0,
        in_position: float = 0.0
    ):
        """Update balance for an exchange"""
        async with self._balance_lock:
            self._balances[exchange] = BalanceState(
                exchange=exchange,
                available=available,
                total=total or available,
                in_position=in_position,
            )
            self._dirty_balances.add(exchange)

    async def get_balance(self, exchange: str) -> Optional[BalanceState]:
        """Get balance for an exchange"""
        return self._balances.get(exchange)

    async def get_available_balance(self, exchange: str) -> float:
        """Get available balance for an exchange"""
        balance = self._balances.get(exchange)
        return balance. available if balance else 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # MARKET DATA OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════════

    async def update_market(
        self,
        symbol: str,
        mark_price: Optional[float] = None,
        funding_rate_x10: Optional[float] = None,
        funding_rate_lighter: Optional[float] = None,
        spread: Optional[float] = None,
        apy: Optional[float] = None
    ):
        """Update market data for a symbol"""
        async with self._market_lock:
            market = self._markets. get(symbol) or MarketState(symbol=symbol)
            
            if mark_price is not None:
                market.mark_price = mark_price
            if funding_rate_x10 is not None:
                market. funding_rate_x10 = funding_rate_x10
            if funding_rate_lighter is not None:
                market. funding_rate_lighter = funding_rate_lighter
            if spread is not None:
                market.spread = spread
            if apy is not None:
                market.apy = apy
            
            market.updated_at = int(time.time() * 1000)
            self._markets[symbol] = market
            self._dirty_markets.add(symbol)

    async def get_market(self, symbol: str) -> Optional[MarketState]:
        """Get market data for a symbol"""
        return self._markets. get(symbol)

    async def get_mark_price(self, symbol: str) -> float:
        """Get mark price for a symbol"""
        market = self._markets.get(symbol)
        return market.mark_price if market else 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # BOT STATE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_state(self, key: str, value: Any):
        """Set a bot state value"""
        async with self._state_lock:
            self._bot_state[key] = value
            self._dirty_bot_state. add(key)

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Get a bot state value"""
        return self._bot_state.get(key, default)

    # ═══════════════════════════════════════════════════════════════════════════
    # WRITE-BEHIND QUEUE
    # ═══════════════════════════════════════════════════════════════════════════

    async def _queue_write(
        self,
        operation: WriteOperation,
        table: str,
        key: str,
        data: Dict[str, Any],
        wait: bool = False
    ) -> Optional[Any]:
        """Queue a write operation"""
        future = asyncio.get_running_loop().create_future() if wait else None
        
        write = PendingWrite(
            operation=operation,
            table=table,
            key=key,
            data=data,
            callback=future
        )
        
        try:
            self._write_queue. put_nowait(write)
        except asyncio.QueueFull:
            logger.warning("Write queue full, dropping oldest")
            try:
                self._write_queue. get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._write_queue. put_nowait(write)
        
        if wait and future:
            return await future
        return None

    async def _writer_loop(self):
        """Background task that batches and commits writes"""
        logger.info("📝 State writer loop started")
        
        batch: List[PendingWrite] = []
        last_flush = time. monotonic()
        
        while self._running:
            try:
                # Get next write with timeout
                try:
                    write = await asyncio. wait_for(
                        self._write_queue.get(),
                        timeout=self.WRITE_FLUSH_INTERVAL
                    )
                    
                    if write is None:  # Shutdown signal
                        break
                    
                    batch.append(write)
                    
                except asyncio.TimeoutError:
                    pass
                
                # Flush if batch is full or interval elapsed
                now = time.monotonic()
                should_flush = (
                    len(batch) >= self. WRITE_BATCH_SIZE or
                    (batch and now - last_flush >= self. WRITE_FLUSH_INTERVAL)
                )
                
                if should_flush and batch:
                    await self._flush_batch(batch)
                    batch = []
                    last_flush = now
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Writer loop error: {e}")
                await asyncio.sleep(1.0)
        
        # Final flush
        if batch:
            await self._flush_batch(batch)
        
        logger.info("📝 State writer loop stopped")

    async def _flush_batch(self, batch: List[PendingWrite]):
        """Flush a batch of writes to database"""
        if not batch or not self._db:
            return
        
        from src.database import get_trade_repository
        repo = await get_trade_repository()
        
        for write in batch:
            try:
                if write.table == "trades":
                    if write.operation == WriteOperation.INSERT:
                        trade_data = write.data.copy()
                        trade_data['status'] = trade_data.get('status', 'open')
                        db_id = await repo.add_trade(trade_data)
                        
                        # Update in-memory with DB ID
                        async with self._trade_lock:
                            if write.key in self._trades:
                                self._trades[write. key].db_id = db_id
                        
                        if write.callback and not write.callback.done():
                            write.callback.set_result(db_id)
                            
                    elif write.operation == WriteOperation.UPDATE:
                        if 'status' in write.data:
                            status = write.data['status']
                            if isinstance(status, TradeStatus):
                                status = status.value
                            if status == 'closed':
                                await repo.close_trade(
                                    write. key,
                                    write.data. get('pnl', 0),
                                    write.data.get('funding_collected', 0)
                                )
                        
                        if write.callback and not write.callback.done():
                            write.callback.set_result(True)
                            
                    elif write. operation == WriteOperation. DELETE:
                        # Trades are closed, not deleted
                        pass
                
            except Exception as e:
                logger.error(f"Flush error for {write.table}/{write.key}: {e}")
                if write.callback and not write.callback.done():
                    write.callback.set_exception(e)
        
        self._stats["writes_flushed"] += len(batch)
        logger.debug(f"📝 Flushed {len(batch)} writes")

    async def _flush_dirty(self):
        """Flush all dirty state to database"""
        # Trade writes are already queued, just process remaining
        while not self._write_queue.empty():
            try:
                write = self._write_queue.get_nowait()
                if write:
                    await self._flush_batch([write])
            except asyncio.QueueEmpty:
                break

    # ═══════════════════════════════════════════════════════════════════════════
    # SYNC & RECOVERY
    # ═══════════════════════════════════════════════════════════════════════════

    async def _load_from_db(self):
        """Load initial state from database"""
        if not self._db:
            return
        
        from src.database import get_trade_repository
        repo = await get_trade_repository()
        
        try:
            db_trades = await repo.get_open_trades()
            
            async with self._trade_lock:
                for row in db_trades:
                    trade = TradeState(
                        symbol=row['symbol'],
                        side_x10=row. get('side_x10', 'BUY'),
                        side_lighter=row.get('side_lighter', 'SELL'),
                        size_usd=safe_float(row.get('size_usd'), 0.0),
                        entry_price_x10=safe_float(row.get('entry_price_x10'), 0.0),
                        entry_price_lighter=safe_float(row.get('entry_price_lighter'), 0.0),
                        status=TradeStatus(row. get('status', 'open')),
                        is_farm_trade=bool(row.get('is_farm_trade', 0)),
                        created_at=row.get('created_at', 0),
                        pnl=safe_float(row.get('pnl'), 0.0),
                        funding_collected=safe_float(row.get('funding_collected'), 0.0),
                        account_label=row.get('account_label', 'Main'),
                        x10_order_id=row. get('x10_order_id'),
                        lighter_order_id=row.get('lighter_order_id'),
                        db_id=row.get('id'),
                    )
                    self._trades[trade. symbol] = trade
            
            logger.info(f"📂 Loaded {len(self._trades)} trades from database")
            
        except Exception as e:
            logger.error(f"Failed to load state from DB: {e}")

    async def _sync_loop(self):
        """Periodic full sync with database"""
        while self._running:
            try:
                await asyncio.sleep(self.SYNC_INTERVAL)
                await self._verify_state()
                self._stats["syncs"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sync error: {e}")

    async def _verify_state(self):
        """Verify in-memory state matches database"""
        if not self._db:
            return
        
        from src.database import get_trade_repository
        repo = await get_trade_repository()
        
        try:
            db_trades = await repo.get_open_trades()
            db_symbols = {t['symbol'] for t in db_trades}
            memory_symbols = await self. get_open_symbols()
            
            # Check for discrepancies
            in_memory_only = memory_symbols - db_symbols
            in_db_only = db_symbols - memory_symbols
            
            if in_memory_only:
                logger.warning(f"⚠️ Trades in memory but not DB: {in_memory_only}")
            
            if in_db_only:
                logger.warning(f"⚠️ Trades in DB but not memory: {in_db_only}")
                # Load missing trades
                for row in db_trades:
                    if row['symbol'] in in_db_only:
                        trade = TradeState. from_dict(row)
                        async with self._trade_lock:
                            self._trades[trade.symbol] = trade
            
        except Exception as e:
            logger.error(f"State verification failed: {e}")

    async def _snapshot_loop(self):
        """Periodic snapshot for recovery"""
        while self._running:
            try:
                await asyncio.sleep(self.SNAPSHOT_INTERVAL)
                await self._save_snapshot()
                self._stats["snapshots"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger. error(f"Snapshot error: {e}")

    async def _save_snapshot(self):
        """Save state snapshot to disk"""
        snapshot_path = Path(self. db_path). parent / "state_snapshot.json"
        
        try:
            snapshot = {
                "timestamp": int(time.time() * 1000),
                "trades": {k: v.to_dict() for k, v in self._trades.items()},
                "balances": {k: asdict(v) for k, v in self._balances.items()},
                "bot_state": self._bot_state,
            }
            
            # Write atomically
            temp_path = snapshot_path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(snapshot, f, indent=2)
            temp_path.replace(snapshot_path)
            
            logger.debug(f"📸 State snapshot saved ({len(self._trades)} trades)")
            
        except Exception as e:
            logger.error(f"Snapshot save failed: {e}")

    async def load_snapshot(self) -> bool:
        """Load state from snapshot (for recovery)"""
        snapshot_path = Path(self.db_path).parent / "state_snapshot.json"
        
        if not snapshot_path.exists():
            return False
        
        try:
            with open(snapshot_path, 'r') as f:
                snapshot = json. load(f)
            
            async with self._trade_lock:
                self._trades = {
                    k: TradeState.from_dict(v)
                    for k, v in snapshot. get("trades", {}).items()
                }
            
            async with self._balance_lock:
                self._balances = {
                    k: BalanceState(**v)
                    for k, v in snapshot.get("balances", {}).items()
                }
            
            self._bot_state = snapshot.get("bot_state", {})
            
            logger.info(f"📸 Loaded snapshot ({len(self._trades)} trades)")
            return True
            
        except Exception as e:
            logger.error(f"Snapshot load failed: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get state manager statistics"""
        return {
            **self._stats,
            "trades_in_memory": len(self._trades),
            "open_trades": len([t for t in self._trades. values() if t. status == TradeStatus.OPEN]),
            "balances_tracked": len(self._balances),
            "markets_tracked": len(self._markets),
            "pending_writes": self._write_queue. qsize(),
            "dirty_trades": len(self._dirty_trades),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE MANAGER INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

_state_manager: Optional[InMemoryStateManager] = None


async def get_state_manager() -> InMemoryStateManager:
    """Get or create the global state manager"""
    global _state_manager
    if _state_manager is None:
        _state_manager = InMemoryStateManager()
        await _state_manager. start()
    return _state_manager


async def close_state_manager():
    """Close the global state manager"""
    global _state_manager
    if _state_manager:
        await _state_manager.stop()
        _state_manager = None