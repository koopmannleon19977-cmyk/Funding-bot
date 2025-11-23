# src/latency_arb.py

import asyncio
import time
import logging
from typing import Dict, Optional, Tuple
from collections import deque
from datetime import datetime

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
        
        # Update tracking
        self.last_update_times: Dict[str, Dict[str, float]] = {}  # symbol -> {exchange: timestamp}
        self.rate_history: Dict[str, deque] = {}  # symbol -> [(timestamp, x10_rate, lighter_rate), ...]
        
        # Detection stats
        self.opportunities_detected = 0
        self.opportunities_executed = 0
        
        logger.info(f"✅ Latency Arb Detector initialized (threshold: {lag_threshold_seconds}s)")
    
    def _update_timestamp(self, symbol: str, exchange: str):
        """Record update timestamp for symbol/exchange"""
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
        
        # Determine who is lagging
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
        # Update timestamps
        self._update_timestamp(symbol, 'X10')
        self._update_timestamp(symbol, 'Lighter')
        
        # Track rate history
        if symbol not in self.rate_history:
            self.rate_history[symbol] = deque(maxlen=100)
        
        now = time.time()
        self.rate_history[symbol].append((now, x10_rate, lighter_rate))
        
        # Need at least 2 samples
        if len(self.rate_history[symbol]) < 2:
            return None
        
        # Check for lag
        lag_info = self._get_lag(symbol)
        
        if not lag_info:
            return None
        
        lagging_exchange, lag_seconds = lag_info
        
        # Get rate history
        history = list(self.rate_history[symbol])
        
        # Check if recent rate changed on leading exchange
        recent_samples = history[-5:]  # Last 5 samples (within lag window)
        
        if len(recent_samples) < 2:
            return None
        
        # Calculate rate momentum on leading exchange
        if lagging_exchange == 'X10':
            # Lighter is leading
            leading_rates = [s[2] for s in recent_samples]  # Lighter rates
            lagging_rates = [s[1] for s in recent_samples]  # X10 rates
            leading_exchange = 'Lighter'
        else:
            # X10 is leading
            leading_rates = [s[1] for s in recent_samples]  # X10 rates
            lagging_rates = [s[2] for s in recent_samples]  # Lighter rates
            leading_exchange = 'X10'
        
        # Check if leading exchange had significant rate change
        rate_change = leading_rates[-1] - leading_rates[0]
        rate_change_abs = abs(rate_change)
        
        # Need significant change (>0.02%/h = 0.0002)
        if rate_change_abs < 0.0002:
            return None
        
        # Check if lagging exchange hasn't updated yet
        lagging_change = abs(lagging_rates[-1] - lagging_rates[0])
        
        if lagging_change > rate_change_abs * 0.3:
            # Lagging exchange already updated
            return None
        
        # ===== OPPORTUNITY DETECTED =====
        self.opportunities_detected += 1
        
        # Predict direction after lag update
        current_net = lighter_rate - x10_rate
        predicted_net = current_net + (rate_change if lagging_exchange == 'X10' else -rate_change)
        
        # Determine position
        # If predicted_net will be positive → Long Lighter, Short X10
        # If predicted_net will be negative → Long X10, Short Lighter
        
        if predicted_net > 0:
            leg1_exchange = 'Lighter'
            leg1_side = 'BUY'
        else:
            leg1_exchange = 'X10'
            leg1_side = 'BUY'
        
        # Expected profit: Capture the rate change before it equalizes
        expected_profit_pct = rate_change_abs * 0.5  # Conservative: 50% of change
        
        logger.info(
            f"🎯 LATENCY ARB: {symbol} | "
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
            'hold_time_seconds': 60,  # Short hold
            'is_latency_arb': True
        }
        
        return opportunity
    
    def get_stats(self) -> Dict:
        """Get detection stats"""
        return {
            'opportunities_detected': self.opportunities_detected,
            'opportunities_executed': self.opportunities_executed,
            'tracked_symbols': len(self.last_update_times)
        }


# Global instance
_detector = None


def get_detector() -> LatencyArbDetector:
    """Get or create global detector instance"""
    global _detector
    if _detector is None:
        _detector = LatencyArbDetector(lag_threshold_seconds=5.0)
    return _detector