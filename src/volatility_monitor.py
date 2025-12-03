# src/volatility_monitor.py
import time
import logging
import statistics
from collections import deque
from typing import Dict, Tuple, Optional
import config

logger = logging.getLogger(__name__)

class VolatilityMonitor:
    """
    Advanced Volatility Monitor (Hybrid: Internal History + Config Fallback).
    
    Features:
    - Maintains sliding window of price history (24h)
    - Calculates volatility using High-Low Range
    - Per-Symbol Regime Detection
    - Granular Multipliers for Sizing & Hold Time
    """

    def __init__(self, window_size: int = 288):  # ~24h at 5min updates
        self.price_history: Dict[str, deque] = {}
        self.current_regimes: Dict[str, str] = {}
        self.window_seconds = 86400  # 24h
        
        # Thresholds (24h volatility as %)
        self.THRESHOLDS = {
            'LOW': getattr(config, 'VOL_LOW_THRESHOLD', 3.0),
            'NORMAL': getattr(config, 'VOL_NORMAL_THRESHOLD', 10.0),
            'HIGH': getattr(config, 'VOL_HIGH_THRESHOLD', 20.0)
        }
        
        # Hard Config Fallback
        self.hard_cap_vol = getattr(config, 'MAX_VOLATILITY_PCT_24H', 50.0)
        # Panic threshold for immediate/forced closes (percent)
        self.panic_threshold = getattr(config, 'VOLATILITY_PANIC_THRESHOLD', 5.0)
        
        self.last_regime_log = {}
        
        logger.info(
            f" Volatility Monitor initialized: "
            f"Low<{self.THRESHOLDS['LOW']}%, Normal<{self.THRESHOLDS['NORMAL']}%, "
            f"High<{self.THRESHOLDS['HIGH']}%, Hard Cap<{self.hard_cap_vol}%"
        )

    def update_price(self, symbol: str, price: float):
        """Append price to history and update regime"""
        if symbol not in self.price_history:
            self.price_history[symbol] = deque()
            
        now = time.time()
        history = self.price_history[symbol]
        history.append((now, price))
        
        # Prune old entries (> 24h)
        while history and (now - history[0][0] > self.window_seconds):
            history.popleft()
            
        # Update Regime
        self._evaluate_regime(symbol)

    def _calculate_volatility(self, symbol: str) -> Optional[float]:
        """
        Calculate 24h volatility using High-Low Range
        
        Returns:
            Volatility as percentage (e.g. 5.0 = 5%)
        """
        history = self.price_history.get(symbol)
        if not history or len(history) < 2:
            return None
            
        prices = [p for _, p in history]
        low = min(prices)
        high = max(prices)
        
        if low == 0:
            return None
            
        # High-Low Range method (better for crypto than mid-point)
        vol_pct = ((high - low) / low) * 100.0
        return vol_pct

    def _evaluate_regime(self, symbol: str):
        """Evaluate and update regime for symbol"""
        vol_pct = self._calculate_volatility(symbol)
        
        if vol_pct is None:
            return
            
        # Hard Cap Check (Priority)
        if vol_pct > self.hard_cap_vol:
            new_regime = "EXTREME"
        elif vol_pct < self.THRESHOLDS['LOW']:
            new_regime = "LOW"
        elif vol_pct < self.THRESHOLDS['NORMAL']:
            new_regime = "NORMAL"
        elif vol_pct < self.THRESHOLDS['HIGH']:
            new_regime = "HIGH"
        else:
            new_regime = "EXTREME"
            
        # State Change Log (throttled to 1/min per symbol)
        old_regime = self.current_regimes.get(symbol, "UNKNOWN")
        now = time.time()
        last_log = self.last_regime_log.get(symbol, 0)
        
        if new_regime != old_regime and (now - last_log > 60):
            logger.info(
                f" VOL REGIME: {symbol} {old_regime} -> {new_regime} "
                f"(24h Range: {vol_pct:.2f}%)"
            )
            self.last_regime_log[symbol] = now
            
        self.current_regimes[symbol] = new_regime

    def check_trade_safety(self, symbol: str) -> Dict:
        """
        Returns granular trade adjustments based on regime.
        
        Returns:
            {
                'allow_entry': bool,
                'size_multiplier': float,
                'hold_time_multiplier': float,
                'close_existing': bool,
                'regime': str,
                'reason': str
            }
        """
        regime = self.current_regimes.get(symbol, "NORMAL")
        
        if regime == "LOW":
            return {
                'allow_entry': True,
                'size_multiplier': 1.2,  # Aggressive in calm markets
                'hold_time_multiplier': 1.5,
                'close_existing': False,
                'regime': regime,
                'reason': "Low volatility - increased sizing"
            }
            
        elif regime == "NORMAL":
            return {
                'allow_entry': True,
                'size_multiplier': 1.0,
                'hold_time_multiplier': 1.0,
                'close_existing': False,
                'regime': regime,
                'reason': "Normal conditions"
            }
            
        elif regime == "HIGH":
            return {
                'allow_entry': True,
                'size_multiplier': 0.5,
                'hold_time_multiplier': 0.25,  # 4h max hold instead of 7d
                'close_existing': False,
                'regime': regime,
                'reason': "High volatility - reduced size/time"
            }
            
        else:  # EXTREME
            return {
                'allow_entry': False,
                'size_multiplier': 0.0,
                'hold_time_multiplier': 0.0,
                'close_existing': True,
                'regime': regime,
                'reason': "EXTREME volatility - trading halted"
            }

    def should_close_due_to_volatility(self, symbol: str) -> bool:
        """Quick check if position should be closed"""
        safety = self.check_trade_safety(symbol)
        return safety.get('close_existing', False)
    # `should_close_due_to` moved to the end of the class for clarity.
    def can_enter_trade(self, symbol: str) -> bool:
        """Quick check if new trades allowed"""
        safety = self.check_trade_safety(symbol)
        return safety.get('allow_entry', True)
        
    def get_size_adjustment(self, symbol: str) -> float:
        """Get size multiplier"""
        safety = self.check_trade_safety(symbol)
        return safety.get('size_multiplier', 1.0)
        
    def get_hold_time_adjustment(self, symbol: str) -> float:
        """Get hold time multiplier"""
        safety = self.check_trade_safety(symbol)
        return safety.get('hold_time_multiplier', 1.0)

    def get_volatility_24h(self, symbol: str) -> float:
        """Get 24h volatility as percentage. Returns 0.0 if not available."""
        vol = self._calculate_volatility(symbol)
        return vol if vol is not None else 0.0

    def get_stats(self) -> Dict:
        """Monitor stats"""
        regime_counts = {}
        for regime in self.current_regimes.values():
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
            
        return {
            'tracked_symbols': len(self.price_history),
            'regime_counts': regime_counts,
            'regimes': self.current_regimes.copy()
        }

    def should_close_due_to(self, volatility: float) -> bool:
        """Check if volatility exceeds critical threshold to panic close.

        Uses `VOLATILITY_PANIC_THRESHOLD` from `config` (fallback to
        `self.panic_threshold`). Logs a warning when panic-close is
        triggered.
        """
        limit = getattr(config, 'VOLATILITY_PANIC_THRESHOLD', self.panic_threshold)
        is_panic = volatility > limit
        if is_panic:
            logger.warning(f"⚠️ Volatility panic: {volatility:.2f}% > {limit}%")
        return is_panic

_monitor = None

def get_volatility_monitor() -> VolatilityMonitor:
    global _monitor
    if _monitor is None:
        _monitor = VolatilityMonitor()
    return _monitor