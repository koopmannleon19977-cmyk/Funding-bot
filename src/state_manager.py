# src/state_manager.py - KOMPLETT ERSETZEN
import asyncio
import aiosqlite
import logging
from typing import Dict, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

class InMemoryStateManager:
    """
    In-Memory State mit Write-Behind Pattern + Fee Tracking
    
    Features:
    - Alle Reads aus RAM (0ms latency)
    - Async writes batched im Background
    - Fee Rate Tracking mit EMA
    - Exchange-spezifische Fee-Separation
    """
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.open_trades: Dict[str, dict] = {}
        self.write_queue: asyncio.Queue = asyncio.Queue()
        self.writer_task: Optional[asyncio.Task] = None
        self.total_reads = 0
        self.total_writes = 0
        self.cache_hits = 0
        
        # Fee Stats: (exchange, symbol) -> fee_rate
        self.fee_cache: Dict[Tuple[str, str], float] = {}
        self.FEE_EMA_ALPHA = 0.2  # 20% weight for new samples
        
        logger.info("InMemoryStateManager initialized")
    
    async def start(self):
        await self._init_db()
        await self._load_from_db()
        await self._load_fee_cache()
        self.writer_task = asyncio.create_task(self._background_writer())
        logger.info("Background writer started")
    
    async def stop(self):
        if self.writer_task:
            await self.write_queue.put(None)
            await self.writer_task
            logger.info("All writes flushed to DB")
    
    async def _init_db(self):
        """Ensure all tables exist with correct schema"""
        async with aiosqlite.connect(self.db_file) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    symbol TEXT PRIMARY KEY,
                    entry_time TIMESTAMP,
                    notional_usd REAL,
                    status TEXT DEFAULT 'OPEN',
                    leg1_exchange TEXT,
                    initial_spread_pct REAL,
                    initial_funding_rate_hourly REAL,
                    entry_price_x10 REAL,
                    entry_price_lighter REAL,
                    funding_flip_start_time TIMESTAMP,
                    is_farm_trade BOOLEAN DEFAULT FALSE,
                    account_label TEXT
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fee_stats (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    avg_fee_rate REAL NOT NULL,
                    sample_count INTEGER DEFAULT 1,
                    last_updated TIMESTAMP,
                    PRIMARY KEY (exchange, symbol)
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    hold_duration_hours REAL,
                    close_reason TEXT,
                    final_pnl_usd REAL,
                    funding_pnl_usd REAL,
                    spread_pnl_usd REAL,
                    fees_usd REAL,
                    account_label TEXT
                )
            """)
            
            await conn.commit()
    
    async def _load_from_db(self):
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                conn.row_factory = aiosqlite.Row
                
                async with conn.execute("SELECT * FROM trades WHERE status = 'OPEN'") as cursor:
                    rows = await cursor.fetchall()
                    
                    for row in rows:
                        trade = dict(row)
                        
                        # Convert datetime strings to datetime objects
                        for date_col in ['entry_time', 'funding_flip_start_time']:
                            val = trade.get(date_col)
                            if val and isinstance(val, str):
                                try:
                                    trade[date_col] = datetime.fromisoformat(val.replace('Z', '+00:00'))
                                except:
                                    try:
                                        trade[date_col] = datetime.strptime(val, '%Y-%m-%d %H:%M:%S.%f')
                                    except:
                                        trade[date_col] = None
                        
                        self.open_trades[trade['symbol']] = trade
            
            logger.info(f"Loaded {len(self.open_trades)} open trades into RAM")
            
        except Exception as e:
            logger.error(f"Failed to load state from DB: {e}")
    
    async def _load_fee_cache(self):
        """Load historical fee rates from DB"""
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute("SELECT exchange, symbol, avg_fee_rate FROM fee_stats") as cursor:
                    rows = await cursor.fetchall()
                    for exchange, symbol, rate in rows:
                        self.fee_cache[(exchange, symbol)] = float(rate)
            
            logger.info(f"Loaded {len(self.fee_cache)} fee stats into cache")
        except Exception as e:
            logger.error(f"Fee cache load error: {e}")
    
    async def _background_writer(self):
        batch = []
        
        while True:
            try:
                try:
                    item = await asyncio.wait_for(self.write_queue.get(), timeout=1.0)
                    
                    if item is None:
                        if batch:
                            await self._flush_batch(batch)
                        break
                    
                    batch.append(item)
                    
                except asyncio.TimeoutError:
                    pass
                
                if len(batch) >= 10 or (batch and self.write_queue.empty()):
                    await self._flush_batch(batch)
                    batch = []
                    
            except Exception as e:
                logger.error(f"Background writer error: {e}")
                await asyncio.sleep(1)
    
    async def _flush_batch(self, batch: List[dict]):
        if not batch:
            return
        
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                for item in batch:
                    op = item['op']
                    data = item['data']
                    
                    if op == 'INSERT':
                        await conn.execute("""
                            INSERT OR REPLACE INTO trades (
                                symbol, entry_time, notional_usd, status, leg1_exchange,
                                initial_spread_pct, initial_funding_rate_hourly,
                                entry_price_x10, entry_price_lighter, 
                                funding_flip_start_time, is_farm_trade, account_label
                            ) VALUES (
                                :symbol, :entry_time, :notional_usd, 'OPEN', :leg1_exchange,
                                :initial_spread_pct, :initial_funding_rate_hourly,
                                :entry_price_x10, :entry_price_lighter, 
                                :funding_flip_start_time, :is_farm_trade, :account_label
                            )
                        """, data)
                        
                    elif op == 'UPDATE':
                        updates = data['updates']
                        symbol = data['symbol']
                        
                        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
                        values = list(updates.values()) + [symbol]
                        
                        await conn.execute(
                            f"UPDATE trades SET {set_clause} WHERE symbol = ?",
                            values
                        )
                        
                    elif op == 'DELETE':
                        await conn.execute(
                            "UPDATE trades SET status = 'CLOSED' WHERE symbol = ?",
                            (data['symbol'],)
                        )
                    
                    elif op == 'FEE_UPDATE':
                        exchange = data['exchange']
                        symbol = data['symbol']
                        new_rate = data['fee_rate']
                        
                        # Check if exists
                        async with conn.execute(
                            "SELECT avg_fee_rate, sample_count FROM fee_stats WHERE exchange=? AND symbol=?",
                            (exchange, symbol)
                        ) as cursor:
                            row = await cursor.fetchone()
                        
                        if row:
                            old_rate, count = row
                            # EMA Update
                            updated_rate = (new_rate * self.FEE_EMA_ALPHA) + (old_rate * (1 - self.FEE_EMA_ALPHA))
                            
                            await conn.execute("""
                                UPDATE fee_stats 
                                SET avg_fee_rate=?, sample_count=?, last_updated=?
                                WHERE exchange=? AND symbol=?
                            """, (updated_rate, count + 1, datetime.utcnow(), exchange, symbol))
                        else:
                            # First sample
                            await conn.execute("""
                                INSERT INTO fee_stats (exchange, symbol, avg_fee_rate, sample_count, last_updated)
                                VALUES (?, ?, ?, 1, ?)
                            """, (exchange, symbol, new_rate, datetime.utcnow()))
                
                await conn.commit()
                
            self.total_writes += len(batch)
            logger.debug(f"Flushed {len(batch)} writes to DB")
            
        except Exception as e:
            logger.error(f"Batch write failed: {e}")
    
    def get_open_trades(self) -> List[dict]:
        self.total_reads += 1
        self.cache_hits += 1
        return list(self.open_trades.values())
    
    def get_trade(self, symbol: str) -> Optional[dict]:
        self.total_reads += 1
        if symbol in self.open_trades:
            self.cache_hits += 1
            return self.open_trades[symbol]
        return None
    
    def get_fee_estimate(self, exchange: str, symbol: str, default: float = 0.00025) -> float:
        """
        Get estimated fee RATE for exchange+symbol
        
        Args:
            exchange: "X10" or "Lighter"
            symbol: Trading pair (e.g. "BTC-USD")
            default: Fallback fee rate
        
        Returns:
            Fee rate (e.g. 0.00025 = 0.025%)
        """
        return self.fee_cache.get((exchange, symbol), default)
    
    async def update_fee_stats(self, exchange: str, symbol: str, fee_rate: float):
        """
        Update fee statistics with new sample
        
        Args:
            exchange: "X10" or "Lighter"
            symbol: Trading pair (e.g. "BTC-USD")
            fee_rate: Fee as decimal (e.g. 0.0005 = 0.05%)
        """
        if fee_rate <= 0 or fee_rate > 0.01:
            logger.warning(f"Suspicious fee rate {fee_rate} for {exchange}/{symbol}, ignoring")
            return
        
        # Update in-memory cache with EMA
        current = self.fee_cache.get((exchange, symbol), fee_rate)
        new_ema = (fee_rate * self.FEE_EMA_ALPHA) + (current * (1 - self.FEE_EMA_ALPHA))
        self.fee_cache[(exchange, symbol)] = new_ema
        
        # Queue DB write
        await self.write_queue.put({
            'op': 'FEE_UPDATE',
            'data': {
                'exchange': exchange,
                'symbol': symbol,
                'fee_rate': fee_rate
            }
        })
        
        logger.debug(f"Fee update {exchange}/{symbol}: {fee_rate:.6f} (EMA: {new_ema:.6f})")
    
    async def add_trade(self, trade_data: dict):
        symbol = trade_data['symbol']
        
        if 'is_farm_trade' not in trade_data:
            trade_data['is_farm_trade'] = False
        
        if 'funding_flip_start_time' not in trade_data:
            trade_data['funding_flip_start_time'] = None
        
        if 'account_label' not in trade_data:
            trade_data['account_label'] = 'Main'
        
        if isinstance(trade_data.get('entry_time'), datetime):
            entry_str = trade_data['entry_time'].strftime('%Y-%m-%d %H:%M:%S.%f')
        else:
            entry_str = trade_data.get('entry_time')
        
        self.open_trades[symbol] = trade_data
        
        db_data = trade_data.copy()
        db_data['entry_time'] = entry_str
        
        await self.write_queue.put({
            'op': 'INSERT',
            'data': db_data
        })
        
        logger.debug(f"Trade {symbol} added to state (queued for DB)")
    
    async def update_trade(self, symbol: str, updates: dict):
        if symbol not in self.open_trades:
            logger.warning(f"Cannot update {symbol} - not in state")
            return
        
        self.open_trades[symbol].update(updates)
        
        await self.write_queue.put({
            'op': 'UPDATE',
            'data': {
                'symbol': symbol,
                'updates': updates
            }
        })
        
        logger.debug(f"Trade {symbol} updated in state (queued for DB)")
    
    async def close_trade(self, symbol: str):
        if symbol not in self.open_trades:
            logger.warning(f"Cannot close {symbol} - not in state")
            return
        
        del self.open_trades[symbol]
        
        await self.write_queue.put({
            'op': 'DELETE',
            'data': {'symbol': symbol}
        })
        
        logger.info(f"Trade {symbol} closed in state (queued for DB)")
    
    def get_stats(self) -> dict:
        hit_rate = (self.cache_hits / self.total_reads * 100) if self.total_reads > 0 else 0
        
        return {
            'open_trades': len(self.open_trades),
            'total_reads': self.total_reads,
            'cache_hits': self.cache_hits,
            'hit_rate_pct': hit_rate,
            'total_writes': self.total_writes,
            'pending_writes': self.write_queue.qsize(),
            'cached_fee_stats': len(self.fee_cache)
        }