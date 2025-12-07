import asyncio
import logging
import time
from typing import List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class CircuitBreakerConfig:
    max_consecutive_failures: int = 5
    max_drawdown_pct: float = 0.10  # 10% max drawdown
    drawdown_window_seconds: int = 3600  # 1 hour
    enable_kill_switch: bool = True

class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.failure_count = 0
        self._equity_history: List[Tuple[float, float]] = []  # (timestamp, equity)
        self._is_triggered = False
        self._trigger_reason = None

    @property
    def is_triggered(self) -> bool:
        return self._is_triggered

    def record_trade_result(self, success: bool, symbol: str = "UNKNOWN"):
        """
        Register a trade outcome.
        """
        if success:
            if self.failure_count > 0:
                logger.info(f"Circuit Breaker: Failure count reset (was {self.failure_count}) after success on {symbol}")
            self.failure_count = 0
        else:
            self.failure_count += 1
            logger.warning(f"Circuit Breaker: Trade failed for {symbol}. Consecutive failures: {self.failure_count}/{self.config.max_consecutive_failures}")
            
            if self.failure_count >= self.config.max_consecutive_failures:
                self._trigger(f"Too many consecutive trade failures ({self.failure_count})")

    def update_equity(self, current_equity: float):
        """
        Track equity to detect rapid drawdowns.
        Should be called periodically (e.g. every minute).
        """
        now = time.time()
        self._equity_history.append((now, current_equity))
        
        # Prune old history
        cutoff = now - self.config.drawdown_window_seconds
        self._equity_history = [x for x in self._equity_history if x[0] > cutoff]
        
        if not self._equity_history:
            return

        # Calculate Max Drawdown in window
        # Max Peak in the current window vs Current Equity
        # Note: This is a "Trailing Drawdown" within the window.
        
        # Find local maximum in the window
        max_equity = max(x[1] for x in self._equity_history)
        
        if max_equity <= 0:
            return

        drawdown = (max_equity - current_equity) / max_equity
        
        if drawdown > self.config.max_drawdown_pct:
            logger.error(f"Drawdown detected: {drawdown*100:.2f}% (Limit: {self.config.max_drawdown_pct*100:.2f}%)")
            self._trigger(f"Critical Drawdown detected: {drawdown*100:.1f}% > {self.config.max_drawdown_pct*100:.1f}%")

    def _trigger(self, reason: str):
        if self._is_triggered:
            return # Already triggered
            
        logger.critical(f"🛑 CIRCUIT BREAKER TRIGGERED: {reason}")
        self._is_triggered = True
        self._trigger_reason = reason
        
        if self.config.enable_kill_switch:
            logger.critical("🛑 KILL SWITCH ENABLED: Initiating Emergency Shutdown Sequence...")
            # We don't exit here directly, we just set the flag. 
            # The main loop checks this flag and handles the Panic Close + Exit.
