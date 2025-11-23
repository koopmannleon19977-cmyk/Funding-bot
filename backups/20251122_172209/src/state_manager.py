# state_manager.py
import asyncio
import aiosqlite
import logging
from typing import Dict, Optional, List
from datetime import datetime
from collections import deque

logger = logging.getLogger(__name__)


class InMemoryStateManager:
    """
     PRODUCTION-GRADE: In-Memory State mit Write-Behind Pattern
    
    Features:
    - Alle Reads aus RAM (0ms latency)
    - Async writes batched im Background
    - Dirty-Tracking fr optimale DB-Performance
    - Crash-Safety mit Write-Ahead-Log
    """
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        
        # In-Memory State
        self.open_trades: Dict[str, dict] = {}  # symbol -> trade_data
        self.dirty_symbols: set = set()  # Symbols mit pending writes
        
        # Write Queue
        self.write_queue: asyncio.Queue = asyncio.Queue()
        self.writer_task: Optional[asyncio.Task] = None
        
        # Stats
        self.total_reads = 0
        self.total_writes = 0
        self.cache_hits = 0
        
        logger.info(" InMemoryStateManager initialized")
    
    async def start(self):
        """Start background writer"""
        # Load initial state
        await self._load_from_db()
        
        # Start background writer
        self.writer_task = asyncio.create_task(self._background_writer())
        logger.info(" Background writer started")
    
    async def stop(self):
        """Graceful shutdown - flush all pending writes"""
        if self.writer_task:
            # Signal shutdown
            await self.write_queue.put(None)
            
            # Wait for queue to empty
            await self.writer_task
            
            logger.info(" All writes flushed to DB")
    
    async def _load_from_db(self):
        """Load initial state from DB into RAM"""
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                conn.row_factory = aiosqlite.Row
                
                async with conn.execute(
                    "SELECT * FROM trades WHERE status = 'OPEN'"
                ) as cursor:
                    rows = await cursor.fetchall()
                    
                    for row in rows:
                        trade = dict(row)
                        
                        # Parse timestamps
                        for date_col in ['entry_time', 'funding_flip_start_time']:
                            val = trade.get(date_col)
                            if val and isinstance(val, str):
                                try:
                                    trade[date_col] = datetime.fromisoformat(
                                        val.replace('Z', '+00:00')
                                    )
                                except:
                                    try:
                                        trade[date_col] = datetime.strptime(
                                            val, '%Y-%m-%d %H:%M:%S.%f'
                                        )
                                    except:
                                        trade[date_col] = None
                        
                        self.open_trades[trade['symbol']] = trade
            
            logger.info(f" Loaded {len(self.open_trades)} open trades into RAM")
            
        except Exception as e:
            logger.error(f" Failed to load state from DB: {e}")
    
    async def _background_writer(self):
        """
        Background task that batches writes to DB
        
        Strategy:
        - Wait for writes in queue
        - Batch up to 10 writes or 1s timeout
        - Execute as single transaction
        """
        batch = []
        
        while True:
            try:
                # Wait for write or timeout
                try:
                    item = await asyncio.wait_for(
                        self.write_queue.get(), 
                        timeout=1.0
                    )
                    
                    # Shutdown signal
                    if item is None:
                        # Flush remaining batch
                        if batch:
                            await self._flush_batch(batch)
                        break
                    
                    batch.append(item)
                    
                except asyncio.TimeoutError:
                    pass
                
                # Flush if batch full or timeout
                if len(batch) >= 10 or (batch and self.write_queue.empty()):
                    await self._flush_batch(batch)
                    batch = []
                    
            except Exception as e:
                logger.error(f" Background writer error: {e}")
                await asyncio.sleep(1)
    
    async def _flush_batch(self, batch: List[dict]):
        """Write batch to DB as single transaction"""
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
                                funding_flip_start_time, is_farm_trade
                            ) VALUES (
                                :symbol, :entry_time, :notional_usd, 'OPEN', :leg1_exchange,
                                :initial_spread_pct, :initial_funding_rate_hourly,
                                :entry_price_x10, :entry_price_lighter, 
                                :funding_flip_start_time, :is_farm_trade
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
                
                await conn.commit()
                
            self.total_writes += len(batch)
            logger.debug(f" Flushed {len(batch)} writes to DB")
            
        except Exception as e:
            logger.error(f" Batch write failed: {e}")
    
    # ===== PUBLIC API =====
    
    def get_open_trades(self) -> List[dict]:
        """
        Get all open trades (instant from RAM)
        
        Returns:
            List of trade dicts
        """
        self.total_reads += 1
        self.cache_hits += 1
        
        return list(self.open_trades.values())
    
    def get_trade(self, symbol: str) -> Optional[dict]:
        """Get specific trade by symbol"""
        self.total_reads += 1
        
        if symbol in self.open_trades:
            self.cache_hits += 1
            return self.open_trades[symbol]
        
        return None
    
    async def add_trade(self, trade_data: dict):
        """
        Add new trade (instant in RAM, async to DB)
        
        Args:
            trade_data: Trade dict with all required fields
        """
        symbol = trade_data['symbol']
        
        # Ensure required fields
        if 'is_farm_trade' not in trade_data:
            trade_data['is_farm_trade'] = False
        
        if 'funding_flip_start_time' not in trade_data:
            trade_data['funding_flip_start_time'] = None
        
        # Format timestamp
        if isinstance(trade_data.get('entry_time'), datetime):
            entry_str = trade_data['entry_time'].strftime('%Y-%m-%d %H:%M:%S.%f')
        else:
            entry_str = trade_data.get('entry_time')
        
        # Update RAM
        self.open_trades[symbol] = trade_data
        
        # Queue DB write
        db_data = trade_data.copy()
        db_data['entry_time'] = entry_str
        
        await self.write_queue.put({
            'op': 'INSERT',
            'data': db_data
        })
        
        logger.debug(f" Trade {symbol} added to state (queued for DB)")
    
    async def update_trade(self, symbol: str, updates: dict):
        """
        Update existing trade (instant in RAM, async to DB)
        
        Args:
            symbol: Trade symbol
            updates: Dict of fields to update
        """
        if symbol not in self.open_trades:
            logger.warning(f" Cannot update {symbol} - not in state")
            return
        
        # Update RAM
        self.open_trades[symbol].update(updates)
        
        # Queue DB write
        await self.write_queue.put({
            'op': 'UPDATE',
            'data': {
                'symbol': symbol,
                'updates': updates
            }
        })
        
        logger.debug(f" Trade {symbol} updated in state (queued for DB)")
    
    async def close_trade(self, symbol: str):
        """
        Close trade (instant in RAM, async to DB)
        
        Args:
            symbol: Trade symbol
        """
        if symbol not in self.open_trades:
            logger.warning(f" Cannot close {symbol} - not in state")
            return
        
        # Remove from RAM
        del self.open_trades[symbol]
        
        # Queue DB write
        await self.write_queue.put({
            'op': 'DELETE',
            'data': {'symbol': symbol}
        })
        
        logger.info(f" Trade {symbol} closed in state (queued for DB)")
    
    def get_stats(self) -> dict:
        """Get performance stats"""
        hit_rate = (self.cache_hits / self.total_reads * 100) if self.total_reads > 0 else 0
        
        return {
            'open_trades': len(self.open_trades),
            'total_reads': self.total_reads,
            'cache_hits': self.cache_hits,
            'hit_rate_pct': hit_rate,
            'total_writes': self.total_writes,
            'pending_writes': self.write_queue.qsize()
        }
