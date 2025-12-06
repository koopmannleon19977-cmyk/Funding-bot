"""
Funding History Collector for Prediction V2
Collects and stores funding rate history for ML predictions. 
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque

from src.database import get_funding_repository, FundingRepository

logger = logging.getLogger(__name__)


@dataclass
class FundingSnapshot:
    timestamp: int
    symbol: str
    rate_x10: float
    rate_lighter: float
    spread: float
    mark_price: float


class FundingHistoryCollector:
    """
    Collects funding rate history for prediction model. 
    Stores last N snapshots per symbol. 
    Persists data to SQLite database.
    """
    
    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self._history: Dict[str, deque] = {}
        self._collection_interval = 60
        self._running = False
        self._repo: Optional[FundingRepository] = None
        
    async def initialize(self):
        """Initialize repository and load history"""
        self._repo = await get_funding_repository()
        # TODO: Load recent history from DB if needed
        logger.info("✅ Funding History Collector initialized with DB connection")

    async def add_snapshot(self, symbol: str, rate_x10: float, rate_lighter: float, 
                     mark_price: float = 0.0):
        """Add a funding rate snapshot and save to DB."""
        timestamp = int(time.time() * 1000)
        
        # 1. Update In-Memory History
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.max_history)
        
        snapshot = FundingSnapshot(
            timestamp=timestamp,
            symbol=symbol,
            rate_x10=rate_x10,
            rate_lighter=rate_lighter,
            spread=rate_lighter - rate_x10,
            mark_price=mark_price
        )
        self._history[symbol].append(snapshot)
        
        # 2. Persist to DB
        if self._repo:
            try:
                # Save specialized history for ML
                await self._repo.add_rate_history(
                    symbol=symbol,
                    rate_lighter=rate_lighter,
                    rate_x10=rate_x10,
                    timestamp=timestamp
                )
                # Also save standard funding record (optional, but good for auditing)
                # We save the average rate or specific exchange rates based on need
                # For now, let's just save the ML history as it is more detailed
            except Exception as e:
                logger.error(f"Failed to persist funding history for {symbol}: {e}")
    
    def get_history(self, symbol: str, n: int = 100) -> List[FundingSnapshot]:
        """Get last N snapshots for symbol."""
        if symbol not in self._history:
            return []
        
        history = list(self._history[symbol])
        return history[-n:]
    
    def get_average_spread(self, symbol: str, periods: int = 24) -> Optional[float]:
        """Get average spread over last N periods."""
        history = self.get_history(symbol, periods)
        
        if len(history) < 3:
            return None
        
        spreads = [s.spread for s in history]
        return sum(spreads) / len(spreads)
    
    def get_spread_trend(self, symbol: str, periods: int = 24) -> Optional[str]:
        """Determine spread trend: WIDENING, NARROWING, or STABLE."""
        history = self.get_history(symbol, periods)
        
        if len(history) < 6:
            return None
        
        mid = len(history) // 2
        first_half = [s. spread for s in history[:mid]]
        second_half = [s.spread for s in history[mid:]]
        
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        
        diff = avg_second - avg_first
        threshold = abs(avg_first) * 0.1 if avg_first != 0 else 0.0001
        
        if diff > threshold:
            return "WIDENING"
        elif diff < -threshold:
            return "NARROWING"
        else:
            return "STABLE"
    
    async def collection_loop(self, x10_adapter, lighter_adapter, symbols: List[str]):
        """Background loop to collect funding snapshots."""
        self._running = True
        if not self._repo:
            await self.initialize()
            
        logger.info(f"📊 Funding History Collector started for {len(symbols)} symbols")
        
        while self._running:
            try:
                collected = 0
                for symbol in symbols:
                    rate_x10 = x10_adapter.funding_rates.get(symbol)
                    rate_lighter = lighter_adapter.funding_rates. get(symbol)
                    
                    if rate_x10 is not None and rate_lighter is not None:
                        mark_price = x10_adapter.price_cache.get(symbol, 0)
                        await self.add_snapshot(symbol, rate_x10, rate_lighter, mark_price)
                        collected += 1
                
                logger.debug(f"📊 Collected {collected} funding snapshots")
                await asyncio.sleep(self._collection_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Funding collection error: {e}")
                await asyncio.sleep(10)
        
        logger.info("📊 Funding History Collector stopped")
    
    def stop(self):
        self._running = False
    
    def get_stats(self) -> Dict:
        """Get collector statistics."""
        return {
            'symbols_tracked': len(self._history),
            'total_snapshots': sum(len(h) for h in self._history.values()),
            'symbols_with_history': [
                s for s, h in self._history.items() if len(h) >= 10
            ],
            'db_connected': self._repo is not None
        }


# Singleton instance
_collector = None

def get_funding_collector() -> FundingHistoryCollector:
    global _collector
    if _collector is None:
        _collector = FundingHistoryCollector()
    return _collector