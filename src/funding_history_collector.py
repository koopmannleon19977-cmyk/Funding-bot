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
    """
    
    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self._history: Dict[str, deque] = {}
        self._collection_interval = 60
        self._running = False
        
    def add_snapshot(self, symbol: str, rate_x10: float, rate_lighter: float, 
                     mark_price: float = 0.0):
        """Add a funding rate snapshot."""
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.max_history)
        
        snapshot = FundingSnapshot(
            timestamp=int(time.time() * 1000),
            symbol=symbol,
            rate_x10=rate_x10,
            rate_lighter=rate_lighter,
            spread=rate_lighter - rate_x10,
            mark_price=mark_price
        )
        
        self._history[symbol].append(snapshot)
    
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
        logger.info(f"📊 Funding History Collector started for {len(symbols)} symbols")
        
        while self._running:
            try:
                collected = 0
                for symbol in symbols:
                    rate_x10 = x10_adapter.funding_rates.get(symbol)
                    rate_lighter = lighter_adapter.funding_rates. get(symbol)
                    
                    if rate_x10 is not None and rate_lighter is not None:
                        mark_price = x10_adapter.price_cache.get(symbol, 0)
                        self.add_snapshot(symbol, rate_x10, rate_lighter, mark_price)
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
            ]
        }


# Singleton instance
_collector = None

def get_funding_collector() -> FundingHistoryCollector:
    global _collector
    if _collector is None:
        _collector = FundingHistoryCollector()
    return _collector