# src/open_interest_tracker.py - PUNKT 8: OPEN INTEREST TRACKING

import asyncio
import time
import logging
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, field
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class OITrend(Enum):
    """Open Interest trend direction"""
    RISING = "RISING"
    FALLING = "FALLING"
    STABLE = "STABLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class OISnapshot:
    """Single OI data point"""
    timestamp: float
    oi_x10: float
    oi_lighter: float
    
    @property
    def total(self) -> float:
        return self. oi_x10 + self. oi_lighter
    
    @property
    def imbalance(self) -> float:
        """Imbalance between exchanges (-1 to +1)"""
        total = self.total
        if total < 1e-8:
            return 0.0
        return (self.oi_x10 - self.oi_lighter) / total


@dataclass
class OIMetrics:
    """Calculated OI metrics for a symbol"""
    symbol: str
    current_oi: float
    velocity_1m: float  # Change per minute
    velocity_5m: float  # 5-min average velocity
    velocity_15m: float  # 15-min average velocity
    trend: OITrend
    imbalance: float  # Cross-exchange imbalance
    zscore: float  # Standard deviation from mean
    last_update: float
    
    def is_healthy(self, min_oi: float = 50000) -> bool:
        """Check if OI indicates healthy market conditions"""
        return (
            self.current_oi >= min_oi and
            self.trend != OITrend.UNKNOWN and
            abs(self.imbalance) < 0.5  # Not too imbalanced
        )


class OpenInterestTracker:
    """
    Tracks Open Interest across exchanges with velocity calculation. 
    
    Features:
    - Rolling window history per symbol
    - Velocity calculation (OI change rate)
    - Trend detection (rising/falling/stable)
    - Cross-exchange imbalance tracking
    - Z-score for anomaly detection
    """
    
    HISTORY_WINDOW = 60  # Keep 60 snapshots
    VELOCITY_THRESHOLD = 0.001  # 0.1% change = trend
    
    def __init__(self, x10_adapter=None, lighter_adapter=None):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        
        # Per-symbol history: deque of OISnapshot
        self._history: Dict[str, deque] = {}
        
        # Cached metrics
        self._metrics: Dict[str, OIMetrics] = {}
        
        # Background task
        self._update_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Symbols to track
        self._tracked_symbols: set = set()
        
        # Lock for thread-safe updates
        self._lock = asyncio.Lock()
    
    def set_adapters(self, x10_adapter, lighter_adapter):
        """Set exchange adapters (for lazy initialization)"""
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
    
    def track_symbol(self, symbol: str):
        """Add symbol to tracking list"""
        self._tracked_symbols. add(symbol)
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self. HISTORY_WINDOW)
    
    def track_symbols(self, symbols: List[str]):
        """Add multiple symbols to tracking"""
        for s in symbols:
            self.track_symbol(s)
    
    async def start(self, update_interval: float = 30.0):
        """Start background OI tracking"""
        if self._running:
            return
        
        self._running = True
        self._update_task = asyncio.create_task(
            self._update_loop(update_interval),
            name="oi_tracker"
        )
        logger.info(f"✅ OpenInterestTracker started (interval={update_interval}s)")
    
    async def stop(self):
        """Stop background tracking"""
        self._running = False
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ OpenInterestTracker stopped")
    
    async def _update_loop(self, interval: float):
        """Background loop to update OI data"""
        while self._running:
            try:
                await self._update_all_symbols()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"OI Tracker update error: {e}")
                await asyncio.sleep(5.0)
    
    async def _update_all_symbols(self):
        """Update OI for all tracked symbols"""
        if not self. x10 or not self.lighter:
            return
        
        symbols = list(self._tracked_symbols)
        if not symbols:
            return
        
        # Batch fetch OI (limit concurrency)
        semaphore = asyncio.Semaphore(5)
        
        async def fetch_one(symbol: str):
            async with semaphore:
                try:
                    await self.update_symbol(symbol)
                except Exception as e:
                    logger.debug(f"OI fetch failed for {symbol}: {e}")
        
        await asyncio.gather(*[fetch_one(s) for s in symbols], return_exceptions=True)
    
    async def update_symbol(self, symbol: str):
        """Fetch and store OI for a single symbol"""
        if not self.x10 or not self.lighter:
            return
        
        now = time.time()
        
        # Fetch OI from both exchanges
        oi_x10 = 0.0
        oi_lighter = 0.0
        
        try:
            oi_x10 = await self.x10.fetch_open_interest(symbol)
        except Exception:
            pass
        
        try:
            oi_lighter = await self.lighter.fetch_open_interest(symbol)
        except Exception:
            pass
        
        # Store snapshot
        snapshot = OISnapshot(
            timestamp=now,
            oi_x10=oi_x10,
            oi_lighter=oi_lighter
        )
        
        async with self._lock:
            if symbol not in self._history:
                self._history[symbol] = deque(maxlen=self. HISTORY_WINDOW)
            
            self._history[symbol]. append(snapshot)
            
            # Recalculate metrics
            self._metrics[symbol] = self._calculate_metrics(symbol)
    
    def update_from_websocket(self, symbol: str, exchange: str, oi: float):
        """
        Update OI from WebSocket stream (non-blocking). 
        Called from websocket_manager. 
        """
        now = time.time()
        
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.HISTORY_WINDOW)
        
        # Get last snapshot or create new
        history = self._history[symbol]
        
        if history:
            last = history[-1]
            # If recent enough, update in place
            if now - last.timestamp < 5.0:
                if exchange. lower() == 'x10':
                    new_snapshot = OISnapshot(now, oi, last.oi_lighter)
                else:
                    new_snapshot = OISnapshot(now, last.oi_x10, oi)
                history[-1] = new_snapshot
            else:
                # Create new snapshot
                if exchange.lower() == 'x10':
                    history.append(OISnapshot(now, oi, last. oi_lighter))
                else:
                    history.append(OISnapshot(now, last.oi_x10, oi))
        else:
            # First snapshot
            if exchange.lower() == 'x10':
                history.append(OISnapshot(now, oi, 0.0))
            else:
                history.append(OISnapshot(now, 0.0, oi))
        
        # Recalculate metrics (sync, called from WS handler)
        self._metrics[symbol] = self._calculate_metrics(symbol)
    
    def _calculate_metrics(self, symbol: str) -> OIMetrics:
        """Calculate OI metrics from history"""
        history = self._history. get(symbol)
        
        if not history or len(history) < 2:
            return OIMetrics(
                symbol=symbol,
                current_oi=history[-1].total if history else 0.0,
                velocity_1m=0.0,
                velocity_5m=0.0,
                velocity_15m=0.0,
                trend=OITrend.UNKNOWN,
                imbalance=history[-1].imbalance if history else 0.0,
                zscore=0.0,
                last_update=time.time()
            )
        
        now = time.time()
        current = history[-1]
        
        # Calculate velocities
        velocity_1m = self._calc_velocity(history, 60)
        velocity_5m = self._calc_velocity(history, 300)
        velocity_15m = self._calc_velocity(history, 900)
        
        # Determine trend
        trend = self._determine_trend(velocity_5m, current.total)
        
        # Calculate z-score
        zscore = self._calc_zscore(history)
        
        return OIMetrics(
            symbol=symbol,
            current_oi=current. total,
            velocity_1m=velocity_1m,
            velocity_5m=velocity_5m,
            velocity_15m=velocity_15m,
            trend=trend,
            imbalance=current.imbalance,
            zscore=zscore,
            last_update=now
        )
    
    def _calc_velocity(self, history: deque, lookback_seconds: float) -> float:
        """Calculate OI change velocity over lookback period"""
        if len(history) < 2:
            return 0.0
        
        now = time.time()
        current = history[-1]
        
        # Find oldest point within lookback
        for snapshot in history:
            if now - snapshot.timestamp <= lookback_seconds:
                dt = current.timestamp - snapshot. timestamp
                if dt > 0:
                    return (current.total - snapshot.total) / dt * 60  # Per minute
                break
        
        return 0.0
    
    def _determine_trend(self, velocity: float, current_oi: float) -> OITrend:
        """Determine OI trend based on velocity"""
        if current_oi < 1e-8:
            return OITrend. UNKNOWN
        
        pct_change = velocity / current_oi if current_oi > 0 else 0
        
        if pct_change > self.VELOCITY_THRESHOLD:
            return OITrend.RISING
        elif pct_change < -self. VELOCITY_THRESHOLD:
            return OITrend. FALLING
        else:
            return OITrend. STABLE
    
    def _calc_zscore(self, history: deque) -> float:
        """Calculate z-score of current OI vs history"""
        if len(history) < 5:
            return 0.0
        
        values = [s.total for s in history]
        mean = sum(values) / len(values)
        
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = variance ** 0.5
        
        if std < 1e-8:
            return 0.0
        
        current = values[-1]
        return (current - mean) / std
    
    def get_metrics(self, symbol: str) -> Optional[OIMetrics]:
        """Get cached OI metrics for symbol"""
        return self._metrics.get(symbol)
    
    def get_all_metrics(self) -> Dict[str, OIMetrics]:
        """Get all cached metrics"""
        return dict(self._metrics)
    
    def is_liquid(self, symbol: str, min_oi: float = 50000) -> bool:
        """Check if symbol has sufficient OI for trading"""
        metrics = self._metrics.get(symbol)
        if not metrics:
            return False
        return metrics.current_oi >= min_oi
    
    def get_velocity(self, symbol: str) -> float:
        """Get 5-min velocity for a symbol"""
        metrics = self._metrics. get(symbol)
        return metrics.velocity_5m if metrics else 0.0
    
    def get_trend(self, symbol: str) -> OITrend:
        """Get current OI trend for symbol"""
        metrics = self._metrics.get(symbol)
        return metrics.trend if metrics else OITrend.UNKNOWN
    
    def get_imbalance(self, symbol: str) -> float:
        """Get cross-exchange OI imbalance"""
        metrics = self._metrics.get(symbol)
        return metrics.imbalance if metrics else 0.0
    
    def get_oi_data_for_prediction(self, symbol: str) -> Dict:
        """
        Get OI data formatted for FundingPredictor input.
        Returns dict with velocity and trend info.
        """
        metrics = self._metrics.get(symbol)
        
        if not metrics:
            return {
                'oi': 0.0,
                'oi_velocity': 0.0,
                'oi_trend': 'UNKNOWN',
                'oi_zscore': 0.0,
                'oi_imbalance': 0.0
            }
        
        return {
            'oi': metrics.current_oi,
            'oi_velocity': metrics.velocity_5m,
            'oi_trend': metrics.trend.value,
            'oi_zscore': metrics.zscore,
            'oi_imbalance': metrics.imbalance
        }
    
    def get_stats(self) -> Dict:
        """Get tracker statistics"""
        return {
            'tracked_symbols': len(self._tracked_symbols),
            'symbols_with_data': len(self._metrics),
            'running': self._running,
            'total_snapshots': sum(len(h) for h in self._history.values())
        }


# ═══════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ═══════════════════════════════════════════════════════════════
_oi_tracker: Optional[OpenInterestTracker] = None


def get_oi_tracker() -> OpenInterestTracker:
    """Get or create singleton OI tracker"""
    global _oi_tracker
    if _oi_tracker is None:
        _oi_tracker = OpenInterestTracker()
    return _oi_tracker


async def init_oi_tracker(x10_adapter, lighter_adapter, symbols: List[str] = None):
    """Initialize and start OI tracker with adapters"""
    tracker = get_oi_tracker()
    tracker.set_adapters(x10_adapter, lighter_adapter)
    
    if symbols:
        tracker.track_symbols(symbols)
    
    await tracker.start()
    return tracker