from datetime import datetime
from collections import deque
from decimal import Decimal
import logging
from typing import Optional

from src.config.settings import settings
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

class CircuitBreaker:
    """
    Protects the system from cascading failures and excessive losses.
    Tracks failures and drawdown to trigger a 'Kill Switch'.
    """
    def __init__(self):
        self.max_failures = settings.risk.cb_max_consecutive_failures
        self.failure_window = deque() # Stores timestamps of failures
        self.is_tripped = False
        self.trip_reason: Optional[str] = None
        
        self.consecutive_failures = 0

    def record_failure(self, reason: str):
        """Register a trade failure."""
        now = datetime.utcnow()
        self.consecutive_failures += 1
        self.failure_window.append(now)
        
        logger.warning(f"CircuitBreaker: Recorded failure #{self.consecutive_failures} - {reason}")
        
        if self.consecutive_failures >= self.max_failures:
            self.trip(f"Max consecutive failures reached ({self.consecutive_failures})")

    def record_success(self):
        """Reset consecutive failures on success."""
        if self.consecutive_failures > 0:
            logger.info("CircuitBreaker: Consecutive failures reset due to success.")
        self.consecutive_failures = 0

    def trip(self, reason: str):
        """Manually trip the circuit breaker."""
        if not self.is_tripped:
            logger.critical(f"🚨 CIRCUIT BREAKER TRIPPED: {reason}")
            self.is_tripped = True
            self.trip_reason = reason
            
            # Optional: Send Alert
            
    def can_trade(self) -> bool:
        """Returns True if trading is allowed."""
        return not self.is_tripped

    def reset(self):
        """Manually reset the breaker."""
        logger.info("CircuitBreaker: Manually Reset.")
        self.is_tripped = False
        self.consecutive_failures = 0
        self.trip_reason = None

# Global Singleton
circuit_breaker = CircuitBreaker()
