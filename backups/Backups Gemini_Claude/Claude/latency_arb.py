# src/latency_arb.py
import time
import logging
from typing import Dict, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)

class LatencyArbDetector:
    """
    Cross-Exchange Latency Arbitrage Detection
    
    Strategy:
    - Track last update times for both exchanges
    - Detect when one exchange lags behind the other
    - Enter trade BEFORE lagging exchange updates
    - Exit immediately after update (30-60s hold)
    
    Expected: 0.1-0.3% per trade, risk-free
    """
    
    def __init__(self, lag_threshold_seconds: float = 5.0):
        self.lag_threshold = lag_threshold_seconds
        self.last_update_times: Dict[str, Dict[str, float]] = {}
        self.rate_history: Dict[str, deque] = {}
        self.opportunities_detected = 0
        self.opportunities_executed = 0
        
        logger.info(f"⚡ Latency Arb Detector initialized (threshold: {lag_threshold_seconds}s)")
    
    def _update_timestamp(self, symbol: str, exchange: str):
        now = time.time()
        if symbol not in self.last_update_times:
            self.last_update_times[symbol] = {}
        self.last_update_times[symbol][exchange] = now
    
    def _get_lag(self, symbol: str) -> Optional[Tuple[str, float]]:
        """
        Calculate lag between exchanges
        
        Returns:
            (lagging_exchange, lag_seconds) or None
        """
        if symbol not in self.last_update_times:
            return None
        
        updates = self.last_update_times[symbol]
        
        if 'X10' not in updates or 'Lighter' not in updates:
            return None
        
        x10_time = updates['X10']
        lighter_time = updates['Lighter']
        
        lag = abs(x10_time - lighter_time)
        
        if lag < self.lag_threshold:
            return None
        
        if x10_time < lighter_time:
            return ('X10', lag)
        else:
            return ('Lighter', lag)
    
    async def detect_lag_opportunity(
        self, 
        symbol: str, 
        x10_rate: float, 
        lighter_rate: float,
        x10_adapter,
        lighter_adapter
    ) -> Optional[Dict]:
        """
        Detect if lag-based arbitrage opportunity exists
        
        Args:
            symbol: Trading pair
            x10_rate: Current X10 funding rate
            lighter_rate: Current Lighter funding rate
            x10_adapter: X10 adapter instance
            lighter_adapter: Lighter adapter instance
        
        Returns:
            Opportunity dict or None
        """
        self._update_timestamp(symbol, 'X10')
        self._update_timestamp(symbol, 'Lighter')
        
        if symbol not in self.rate_history:
            self.rate_history[symbol] = deque(maxlen=100)
        
        now = time.time()
        self.rate_history[symbol].append((now, x10_rate, lighter_rate))
        
        if len(self.rate_history[symbol]) < 2:
            return None
        
        lag_info = self._get_lag(symbol)
        if not lag_info:
            return None
        
        lagging_exchange, lag_seconds = lag_info
        
        history = list(self.rate_history[symbol])
        recent_samples = history[-5:]
        
        if len(recent_samples) < 2:
            return None
        
        if lagging_exchange == 'X10':
            leading_rates = [s[2] for s in recent_samples]
            lagging_rates = [s[1] for s in recent_samples]
            leading_exchange = 'Lighter'
        else:
            leading_rates = [s[1] for s in recent_samples]
            lagging_rates = [s[2] for s in recent_samples]
            leading_exchange = 'X10'
        
        rate_change = leading_rates[-1] - leading_rates[0]
        rate_change_abs = abs(rate_change)
        
        if rate_change_abs < 0.0002:
            return None
        
        lagging_change = abs(lagging_rates[-1] - lagging_rates[0])
        
        if lagging_change > rate_change_abs * 0.3:
            return None
        
        self.opportunities_detected += 1
        
        current_net = lighter_rate - x10_rate
        predicted_net = current_net + (rate_change if lagging_exchange == 'X10' else -rate_change)
        
        if predicted_net > 0:
            leg1_exchange = 'Lighter'
            leg1_side = 'BUY'
        else:
            leg1_exchange = 'X10'
            leg1_side = 'BUY'
        
        expected_profit_pct = rate_change_abs * 0.5
        
        logger.info(
            f"⚡ LATENCY ARB: {symbol} | "
            f"{lagging_exchange} lagging {lag_seconds:.1f}s | "
            f"Leading change: {rate_change:+.6f} | "
            f"Expected: {expected_profit_pct*100:.3f}%"
        )
        
        opportunity = {
            'symbol': symbol,
            'type': 'latency_arb',
            'lagging_exchange': lagging_exchange,
            'lag_seconds': lag_seconds,
            'rate_change': rate_change,
            'expected_profit_pct': expected_profit_pct,
            'leg1_exchange': leg1_exchange,
            'leg1_side': leg1_side,
            'net_funding_hourly': predicted_net,
            'apy': abs(predicted_net) * 24 * 365,
            'hold_time_seconds': 60,
            'is_latency_arb': True
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
        _detector = LatencyArbDetector(lag_threshold_seconds=5.0)
    return _detector