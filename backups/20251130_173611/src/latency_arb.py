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
    - Uses REAL WebSocket update timestamps from adapters.
    - Detects if one exchange's data is stale (> lag_threshold).
    - If the 'fast' exchange moves significantly, we front-run the 'slow' one.
    """
    
    def __init__(self, lag_threshold_seconds: float = 1.0):  # THRESHOLD GESENKT AUF 1.0s
        self.lag_threshold = lag_threshold_seconds
        self.last_update_times: Dict[str, Dict[str, float]] = {}
        self.rate_history: Dict[str, deque] = {}
        self.opportunities_detected = 0
        self.opportunities_executed = 0
        
        logger.info(f"⚡ Latency Arb Detector initialized (threshold: {lag_threshold_seconds}s)")
    
    def _update_timestamps_from_adapters(self, symbol: str, x10_adapter, lighter_adapter):
        """
        Extracts the latest update timestamp from adapter caches.
        Falls back to current time if cache is missing.
        """
        now = time.time()
        
        # Get X10 timestamp (ws cache time)
        t_x10 = now
        if hasattr(x10_adapter, '_funding_cache_time'):
            t_x10 = x10_adapter._funding_cache_time.get(symbol, now)
            
        # Get Lighter timestamp (ws cache time)
        t_lit = now
        if hasattr(lighter_adapter, '_funding_cache_time'):
            t_lit = lighter_adapter._funding_cache_time.get(symbol, now)

        if symbol not in self.last_update_times:
            self.last_update_times[symbol] = {}
            
        self.last_update_times[symbol]['X10'] = t_x10
        self.last_update_times[symbol]['Lighter'] = t_lit
    
    def _get_lag(self, symbol: str) -> Optional[Tuple[str, float]]:
        """
        Calculate lag based on stored timestamps.
        Returns: (lagging_exchange_name, lag_in_seconds)
        """
        if symbol not in self.last_update_times:
            return None
        
        updates = self.last_update_times[symbol]
        x10_time = updates.get('X10', 0)
        lighter_time = updates.get('Lighter', 0)
        
        diff = x10_time - lighter_time
        lag = abs(diff)
        
        # DEBUG: Logge JEDEN Lag über 0.5s, damit wir Aktivität sehen
        if lag > 0.5:
            lagger = 'X10' if x10_time < lighter_time else 'Lighter'
            # Nur alle 10s loggen um Spam zu vermeiden
            if int(time.time()) % 10 == 0:
                logger.debug(f"🔍 Lag check {symbol}: {lag:.2f}s ({lagger} slow)")

        if lag < self.lag_threshold:
            return None
        
        # If X10 time < Lighter time, X10 is OLDER (Lagging)
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
        Detect if lag-based arbitrage opportunity exists.
        """
        # 1. Sync timestamps
        self._update_timestamps_from_adapters(symbol, x10_adapter, lighter_adapter)
        
        # 2. Update Rate History
        if symbol not in self.rate_history:
            self.rate_history[symbol] = deque(maxlen=100)
        
        now = time.time()
        self.rate_history[symbol].append((now, x10_rate, lighter_rate))
        
        if len(self.rate_history[symbol]) < 5:
            return None
        
        # 3. Check for Lag
        lag_info = self._get_lag(symbol)
        if not lag_info:
            return None
        
        lagging_exchange, lag_seconds = lag_info
        
        # 4. Analyze Trend of the LEADING exchange
        history = list(self.rate_history[symbol])
        recent_samples = history[-5:] # Check last few updates
        
        if lagging_exchange == 'X10':
            # Lighter is leading
            leading_rates = [s[2] for s in recent_samples]
            leading_exchange = 'Lighter'
        else:
            # X10 is leading
            leading_rates = [s[1] for s in recent_samples]
            leading_exchange = 'X10'
        
        # Calculate momentum of leader
        rate_change = leading_rates[-1] - leading_rates[0]
        rate_change_abs = abs(rate_change)
        
        # Filter: Minimum movement required to justify trade
        # REDUZIERT: 0.05% -> 0.01% um empfindlicher zu sein
        if rate_change_abs < 0.0001:
            return None
            
        self.opportunities_detected += 1
        
        # 5. Predict Direction
        # Logic: If Leader funding goes UP, Laggard funding will likely go UP too.
        # We want to position ahead of the laggard update.
        
        # Current visible spread
        current_net = lighter_rate - x10_rate
        
        # Predicted spread (assuming laggard catches up)
        # This is a simplification; usually we just trade the convergence
        predicted_net = current_net + (rate_change if lagging_exchange == 'X10' else -rate_change)
        
        # Construct Opportunity
        # We map this to a standard opportunity dict so the executor handles it normally
        
        # Direction determination:
        # If funding increases, we want to be Long the receiver / Short the payer?
        # Actually simpler: We treat it as a spread arbitrage with a 'boosted' rate
        
        logger.info(
            f"⚡ LATENCY ARB: {symbol} | "
            f"{lagging_exchange} lagging {lag_seconds:.1f}s | "
            f"Leader ({leading_exchange}) moved {rate_change:+.6f}"
        )
        
        opportunity = {
            'symbol': symbol,
            'type': 'latency_arb',
            'apy': abs(predicted_net) * 24 * 365 * 2, # Artificial boost to ensure execution priority
            'net_funding_hourly': predicted_net,
            
            # Standard execution fields
            'leg1_exchange': 'Lighter' if predicted_net > 0 else 'X10',
            'leg1_side': 'SELL' if predicted_net > 0 else 'BUY', # Standard arb direction
            
            # Meta info
            'is_farm_trade': False,
            'is_latency_arb': True,
            'lag_seconds': lag_seconds,
            'confidence': 0.95 # High confidence
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
        _detector = LatencyArbDetector(lag_threshold_seconds=1.0)  # Standard 1s
    return _detector