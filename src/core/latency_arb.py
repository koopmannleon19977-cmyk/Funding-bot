# src/latency_arb.py
import time
import logging
from typing import Dict, Optional, Tuple, Any
from collections import deque
from decimal import Decimal

logger = logging.getLogger(__name__)

class LatencyArbDetector:
    """
    Cross-Exchange Latency Arbitrage Detection using Decimal for funding rates.
    """
    
    def __init__(self, lag_threshold_seconds: float = 5.0):
        # Load config values if available
        try:
            import config
            self.lag_threshold = float(getattr(config, 'LATENCY_ARB_MIN_LAG_SECONDS', lag_threshold_seconds))
            self.max_lag_threshold = float(getattr(config, 'LATENCY_ARB_MAX_LAG_SECONDS', 30.0))
            self.min_rate_change = Decimal(str(getattr(config, 'LATENCY_ARB_MIN_RATE_DIFF', 0.0002)))
        except (ImportError, Exception):
            self.lag_threshold = lag_threshold_seconds
            self.max_lag_threshold = 30.0
            self.min_rate_change = Decimal('0.0002')
        
        self.last_update_times: Dict[str, Dict[str, float]] = {}
        self.rate_history: Dict[str, deque] = {}
        self.opportunities_detected = 0
        self.opportunities_executed = 0
        
        self.last_opportunity_time: Dict[str, float] = {}
        self.opportunity_cooldown = 60.0
        
        logger.info(
            f"⚡ Latency Arb Detector initialized "
            f"(min_lag={self.lag_threshold}s, max_lag={self.max_lag_threshold}s, min_rate_diff={float(self.min_rate_change)*100:.3f}%)"
        )
    
    def _update_timestamps_from_adapters(self, symbol: str, x10_adapter: Any, lighter_adapter: Any):
        now = time.time()
        
        # Access internal attributes if they exist (adapters might need to expose these via interface)
        t_x10 = now
        if hasattr(x10_adapter, '_funding_cache_time'):
            t_x10 = x10_adapter._funding_cache_time.get(symbol, now)
            
        t_lit = now
        if hasattr(lighter_adapter, '_funding_cache_time'):
            t_lit = lighter_adapter._funding_cache_time.get(symbol, now)

        if symbol not in self.last_update_times:
            self.last_update_times[symbol] = {}
            
        self.last_update_times[symbol]['X10'] = t_x10
        self.last_update_times[symbol]['Lighter'] = t_lit
    
    def _get_lag(self, symbol: str) -> Optional[Tuple[str, float]]:
        if symbol not in self.last_update_times:
            return None
        
        updates = self.last_update_times[symbol]
        x10_time = updates.get('X10', 0)
        lighter_time = updates.get('Lighter', 0)
        
        diff = x10_time - lighter_time
        lag = abs(diff)
        
        if lag < self.lag_threshold:
            return None
        
        if x10_time < lighter_time:
            return ('X10', lag)
        else:
            return ('Lighter', lag)
    
    async def detect_lag_opportunity(
        self, 
        symbol: str, 
        x10_rate: Decimal, 
        lighter_rate: Decimal,
        x10_adapter: Any,
        lighter_adapter: Any
    ) -> Optional[Dict]:
        """
        Enhanced lag detection with Decimal funding rates.
        """
        now = time.time()
        last_opp = self.last_opportunity_time.get(symbol, 0)
        if now - last_opp < self.opportunity_cooldown:
            return None
        
        # 1. Sync timestamps
        self._update_timestamps_from_adapters(symbol, x10_adapter, lighter_adapter)
        
        # 2. Update Rate History
        if symbol not in self.rate_history:
            self.rate_history[symbol] = deque(maxlen=100)
        
        self.rate_history[symbol].append((now, x10_rate, lighter_rate))
        
        if len(self.rate_history[symbol]) < 5:
            return None
        
        # 3. Check for Lag
        lag_info = self._get_lag(symbol)
        if not lag_info:
            return None
        
        lagging_exchange, lag_seconds = lag_info
        
        if lag_seconds > self.max_lag_threshold:
            return None
        
        # 4. Analyze Trend of the LEADING exchange
        history = list(self.rate_history[symbol])
        recent_samples = history[-5:]
        
        if lagging_exchange == 'X10':
            leading_rates = [s[2] for s in recent_samples]
            leading_exchange = 'Lighter'
        else:
            leading_rates = [s[1] for s in recent_samples]
            leading_exchange = 'X10'
        
        # Calculate momentum of leader (Decimal)
        rate_change = leading_rates[-1] - leading_rates[0]
        rate_change_abs = abs(rate_change)
        
        if rate_change_abs < self.min_rate_change:
            return None
            
        self.opportunities_detected += 1
        self.last_opportunity_time[symbol] = now
        
        # 5. Predict Direction
        current_net = lighter_rate - x10_rate
        predicted_net = current_net + (rate_change if lagging_exchange == 'X10' else -rate_change)
        
        logger.info(
            f"⚡ LATENCY ARB: {symbol} | "
            f"{lagging_exchange} lagging {lag_seconds:.1f}s | "
            f"Leader ({leading_exchange}) moved {float(rate_change):+.6f}"
        )
        
        opportunity = {
            'symbol': symbol,
            'type': 'latency_arb',
            'apy': float(abs(predicted_net) * Decimal('24') * Decimal('365') * Decimal('2')),
            'net_funding_hourly': float(predicted_net),
            'leg1_exchange': 'Lighter' if predicted_net > 0 else 'X10',
            'leg1_side': 'SELL' if predicted_net > 0 else 'BUY',
            'is_farm_trade': False,
            'is_latency_arb': True,
            'lag_seconds': lag_seconds,
            'confidence': 0.95
        }
        
        return opportunity
    
    def get_stats(self) -> Dict:
        return {
            'opportunities_detected': self.opportunities_detected,
            'opportunities_executed': self.opportunities_executed,
            'tracked_symbols': len(self.last_update_times)
        }

_detector = None

def get_detector() -> LatencyArbDetector:
    global _detector
    if _detector is None:
        try:
            import config
            lag_threshold = getattr(config, 'LATENCY_ARB_MIN_LAG_SECONDS', 5.0)
        except ImportError:
            lag_threshold = 5.0
        _detector = LatencyArbDetector(lag_threshold_seconds=lag_threshold)
    return _detector

def is_latency_arb_enabled() -> bool:
    """Check if latency arbitrage is enabled in config."""
    try:
        import config
        return getattr(config, 'LATENCY_ARB_ENABLED', True)
    except Exception:
        return True  # Default: enabled