# src/btc_correlation.py
import time
import logging
from collections import deque
from typing import Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

class BTCCorrelationMonitor:
    """
    Monitors BTC Price action to detect market-wide risk regimes.
    
    Logic:
    - Tracks BTC price history (short-term & medium-term).
    - Calculates 'Crash Probability' based on velocity & acceleration.
    - Provides a 'Safety Factor' (0.0 to 1.0) for Long positions.
    
    If BTC crashes, Funding Rates often turn negative.
    We want to reduce Long exposure in these moments.
    """
    
    def __init__(self, window_size: int = 300): # 300 updates history
        self.price_history = deque(maxlen=window_size)
        self.last_update = 0
        self.current_price = 0.0
        
        # Configurable Thresholds
        self.CRASH_VELOCITY = -0.005      # -0.5% in short time
        self.DUMP_ACCELERATION = -0.001   # Accelerating dump
        self.RECOVERY_THRESHOLD = 0.002   # +0.2% for recovery
        
    def update_price(self, price: float):
        """Feed new BTC price (from Lighter or X10)"""
        if price <= 0: return
        
        now = time.time()
        self.current_price = price
        self.price_history.append((now, price))
        self.last_update = now

    def get_market_regime(self) -> Tuple[str, float]:
        """
        Returns (RegimeName, SafetyFactor)
        
        Regimes:
        - BULLISH: BTC pumps -> Funding likely positive -> Factor 1.0
        - STABLE:  BTC ranging -> Normal operations -> Factor 1.0
        - DUMP:    BTC dumping -> Funding drops/negative -> Factor 0.0 - 0.5
        - CRASH:   Panic selling -> STOP TRADING -> Factor 0.0
        """
        if len(self.price_history) < 10:
            return "UNCERTAIN", 1.0
            
        # Analyze last 15 minutes (approx)
        history_slice = list(self.price_history)
        
        # Calculate changes
        start_p = history_slice[0][1]
        end_p = history_slice[-1][1]
        
        total_change = (end_p - start_p) / start_p
        
        # Calculate Velocity (change per minute)
        duration = history_slice[-1][0] - history_slice[0][0]
        if duration < 1: duration = 1
        velocity_per_min = total_change / (duration / 60.0)
        
        # Short term (last 1 min)
        short_slice = [x for x in history_slice if x[0] > time.time() - 60]
        if short_slice and len(short_slice) > 2:
            s_start = short_slice[0][1]
            s_end = short_slice[-1][1]
            short_change = (s_end - s_start) / s_start
        else:
            short_change = 0.0

        # === REGIME DETECTION ===
        
        # 1. CRASH DETECTION
        if short_change < -0.01: # -1% in 1 min
            return "CRASH", 0.0
            
        # 2. DUMP DETECTION
        if velocity_per_min < -0.001: # -0.1% per min steady drop
            return "DUMP", 0.5
            
        # 3. BULLISH
        if velocity_per_min > 0.001:
            return "BULLISH", 1.2 # Boost longs slightly
            
        # 4. STABLE
        return "STABLE", 1.0

    def get_safety_multiplier(self) -> float:
        """Get simple float multiplier for position sizing [0.0 - 1.2]"""
        regime, factor = self.get_market_regime()
        return factor

# Singleton
_btc_monitor = None

def get_btc_monitor() -> BTCCorrelationMonitor:
    global _btc_monitor
    if _btc_monitor is None:
        _btc_monitor = BTCCorrelationMonitor()
    return _btc_monitor