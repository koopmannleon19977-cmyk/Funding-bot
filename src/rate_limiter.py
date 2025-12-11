# src/rate_limiter.py - PUNKT 3: TOKEN BUCKET RATE LIMITER (FIXED EXPORTS)

import asyncio
import time
import logging
from typing import Optional, Dict, Callable, Any, List
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
import config

logger = logging.getLogger(__name__)


class Exchange(Enum):
    X10 = "X10"
    LIGHTER = "LIGHTER"


@dataclass
class RateLimiterConfig:
    """Configuration for rate limiter"""
    tokens_per_second: float = 10.0
    max_tokens: float = 100.0
    min_request_interval: float = 0.05
    penalty_429_seconds: float = 30.0
    penalty_multiplier: float = 2.0
    max_penalty: float = 300.0


class TokenBucketRateLimiter:
    """
    Token Bucket Rate Limiter with 429 penalty handling.
    
    Features:
    - Proactive rate limiting (prevents 429s)
    - Automatic penalty on 429 response
    - Exponential backoff on repeated 429s
    - Per-exchange isolation
    - Request deduplication (prevents API storms)
    - Graceful shutdown support (cancels all waiting tasks)
    """
    
    def __init__(self, config: Optional[RateLimiterConfig] = None, name: str = "default"):
        self.config = config or RateLimiterConfig()
        self.name = name
        
        self._tokens = self.config.max_tokens
        self._last_refill = time.monotonic()
        self._last_request = 0.0
        self._penalty_until = 0.0
        self._consecutive_429s = 0
        self._current_penalty = self.config.penalty_429_seconds
        
        self._lock = asyncio.Lock()
        
        # ═══════════════════════════════════════════════════════════════
        # GRACEFUL SHUTDOWN SUPPORT
        # ═══════════════════════════════════════════════════════════════
        self._shutdown = False
        self._waiters: List[asyncio.Task] = []
        self._waiters_lock = asyncio.Lock()
        
        # ═══════════════════════════════════════════════════════════════
        # REQUEST DEDUPLICATION (Prevents API storms during shutdown)
        # ═══════════════════════════════════════════════════════════════
        self._recent_requests: Dict[str, float] = {}
        self._dedup_window = 0.5  # 500ms - skip duplicate requests within this window
        self._dedup_cleanup_interval = 60.0  # Cleanup old entries every 60s
        self._last_dedup_cleanup = time.monotonic()
        
        # Stats
        self._requests_total = 0
        self._requests_throttled = 0
        self._penalties_applied = 0
        self._requests_deduplicated = 0
    
    def _refill_tokens(self):
        """Refill tokens based on elapsed time"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        
        tokens_to_add = elapsed * self.config.tokens_per_second
        self._tokens = min(self.config.max_tokens, self._tokens + tokens_to_add)
        self._last_refill = now
    
    def shutdown(self):
        """
        Signal shutdown - cancel all waiting tasks.
        
        This should be called early in the shutdown sequence,
        BEFORE trying to close positions, to prevent rate-limited
        operations from hanging.
        """
        if self._shutdown:
            return  # Already shut down
        
        self._shutdown = True
        cancelled_count = 0
        
        # Cancel all waiting tasks
        for task in self._waiters:
            if task and not task.done():
                task.cancel()
                cancelled_count += 1
        
        self._waiters.clear()
        logger.info(f"🛑 [{self.name}] Rate limiter shutdown - cancelled {cancelled_count} waiters")
    
    @property
    def is_shutdown(self) -> bool:
        """Check if rate limiter is in shutdown mode."""
        return self._shutdown
    
    async def _register_waiter(self) -> asyncio.Task:
        """Register current task as a waiter."""
        task = asyncio.current_task()
        if task:
            async with self._waiters_lock:
                self._waiters.append(task)
        return task
    
    async def _unregister_waiter(self, task: asyncio.Task):
        """Unregister task from waiters."""
        if task:
            async with self._waiters_lock:
                if task in self._waiters:
                    self._waiters.remove(task)
    
    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire tokens, waiting if necessary.
        Returns wait time in seconds, or -1.0 if shutdown/cancelled.
        
        Handles asyncio.CancelledError gracefully during shutdown.
        Callers should check for negative return value to detect shutdown.
        """
        # ═══════════════════════════════════════════════════════════════
        # SHUTDOWN CHECK: Return immediately if shutting down
        # ═══════════════════════════════════════════════════════════════
        if self._shutdown:
            logger.debug(f"[{self.name}] Rate limiter acquire() skipped - shutdown active")
            return -1.0
        
        # SAFETY: Ensure tokens is a float (catches caller bugs)
        if not isinstance(tokens, (int, float)):
            logger.warning(f"[{self.name}] acquire() called with non-numeric tokens: {tokens} (type={type(tokens)}). Using 1.0")
            tokens = 1.0
        tokens = float(tokens)
        
        # Register this task as a waiter so it can be cancelled during shutdown
        current_task = await self._register_waiter()
        
        try:
            async with self._lock:
                # Double-check shutdown after acquiring lock
                if self._shutdown:
                    return -1.0
                
                self._requests_total += 1
                
                # Check penalty
                now = time.monotonic()
                if now < self._penalty_until:
                    wait_time = self._penalty_until - now
                    self._requests_throttled += 1
                    logger.debug(f"[{self.name}] Penalty active, waiting {wait_time:.1f}s")
                    try:
                        await asyncio.sleep(wait_time)
                    except asyncio.CancelledError:
                        logger.debug(f"[{self.name}] Rate limiter sleep cancelled during penalty wait")
                        raise
                    
                    # Check shutdown after sleep
                    if self._shutdown:
                        return -1.0
                    now = time.monotonic()
                
                # Refill tokens
                self._refill_tokens()
                
                # Check minimum interval
                time_since_last = now - self._last_request
                if time_since_last < self.config.min_request_interval:
                    wait_time = self.config.min_request_interval - time_since_last
                    try:
                        await asyncio.sleep(wait_time)
                    except asyncio.CancelledError:
                        logger.debug(f"[{self.name}] Rate limiter sleep cancelled during interval wait")
                        raise
                    
                    # Check shutdown after sleep
                    if self._shutdown:
                        return -1.0
                
                # Wait for tokens if needed
                if self._tokens < tokens:
                    tokens_needed = tokens - self._tokens
                    wait_time = tokens_needed / self.config.tokens_per_second
                    self._requests_throttled += 1
                    logger.debug(f"[{self.name}] Waiting {wait_time:.2f}s for tokens")
                    try:
                        await asyncio.sleep(wait_time)
                    except asyncio.CancelledError:
                        logger.debug(f"[{self.name}] Rate limiter sleep cancelled during token wait")
                        raise
                    
                    # Check shutdown after sleep
                    if self._shutdown:
                        return -1.0
                    self._refill_tokens()
                
                # Consume tokens
                self._tokens -= tokens
                self._last_request = time.monotonic()
                
                return 0.0
        except asyncio.CancelledError:
            # Return -1.0 to signal cancellation to caller (don't raise!)
            # This prevents "exception was never retrieved" errors in asyncio.gather()
            logger.debug(f"[{self.name}] Rate limiter acquire cancelled during shutdown")
            return -1.0  # Caller should check for negative value
        finally:
            # Always unregister waiter
            await self._unregister_waiter(current_task)
    
    def penalize_429(self):
        """Apply penalty for 429 response"""
        self._consecutive_429s += 1
        self._penalties_applied += 1
        
        # Exponential backoff
        self._current_penalty = min(
            self.config.penalty_429_seconds * (self.config.penalty_multiplier ** (self._consecutive_429s - 1)),
            self.config.max_penalty
        )
        
        self._penalty_until = time.monotonic() + self._current_penalty
        
        logger.warning(
            f"[{self.name}] 429 detected! Penalty: {self._current_penalty:.0f}s "
            f"(consecutive: {self._consecutive_429s})"
        )
    
    def on_success(self):
        """Reset consecutive 429 counter on success"""
        if self._consecutive_429s > 0:
            self._consecutive_429s = 0
            self._current_penalty = self.config.penalty_429_seconds
    
    def _cleanup_old_requests(self):
        """Remove old entries from dedup cache to prevent memory growth"""
        now = time.monotonic()
        if now - self._last_dedup_cleanup > self._dedup_cleanup_interval:
            cutoff = now - self._dedup_window
            self._recent_requests = {k: v for k, v in self._recent_requests.items() if v > cutoff}
            self._last_dedup_cleanup = now
    
    def is_duplicate(self, request_key: str) -> bool:
        """
        Check if a request should be skipped due to deduplication.
        
        Args:
            request_key: Unique key for request (e.g., "GET:/user/orders:BTC-USD")
        
        Returns:
            True if request is duplicate and should be skipped
        """
        now = time.monotonic()
        self._cleanup_old_requests()
        
        last_request_time = self._recent_requests.get(request_key, 0)
        if now - last_request_time < self._dedup_window:
            self._requests_deduplicated += 1
            logger.debug(f"[{self.name}] 🔄 Deduplicated: {request_key}")
            return True
        
        # Record this request
        self._recent_requests[request_key] = now
        return False
    
    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        return {
            "name": self.name,
            "tokens_available": self._tokens,
            "requests_total": self._requests_total,
            "requests_throttled": self._requests_throttled,
            "requests_deduplicated": self._requests_deduplicated,
            "penalties_applied": self._penalties_applied,
            "consecutive_429s": self._consecutive_429s,
            "penalty_active": time.monotonic() < self._penalty_until,
            "shutdown": self._shutdown,
            "active_waiters": len(self._waiters)
        }




# ═══════════════════════════════════════════════════════════════
# EXCHANGE-SPECIFIC LIMITERS (SINGLETON INSTANCES)
# ═══════════════════════════════════════════════════════════════

# X10: 1000 requests/minute = ~16.7/second
X10_RATE_LIMITER = TokenBucketRateLimiter(
    config=RateLimiterConfig(
        tokens_per_second=15.0,
        max_tokens=50.0,
        min_request_interval=0.06,
        penalty_429_seconds=10.0
    ),
    name="X10"
)

# Lighter Rate Limits
# Standard: 60 req/min = 1 req/s
# Premium: 24000 req/min = 400 req/s (We use 50 safe limit)

lighter_tier = getattr(config, 'LIGHTER_ACCOUNT_TIER', 'STANDARD').upper()
logger.info(f"🛡️ Lighter Rate Limiter Tier: {lighter_tier}")

if lighter_tier == 'PREMIUM':
    lighter_config = RateLimiterConfig(
        tokens_per_second=50.0,
        max_tokens=100.0,
        min_request_interval=0.02,
        penalty_429_seconds=10.0
    )
else:
    # STANDARD (Default)
    lighter_config = RateLimiterConfig(
        tokens_per_second=1.0,   # 1 request per second
        max_tokens=5.0,          # Burst up to 5
        min_request_interval=1.0, # Strict 1s interval
        penalty_429_seconds=30.0 # Stricter penalty for Standard
    )

LIGHTER_RATE_LIMITER = TokenBucketRateLimiter(
    config=lighter_config,
    name="LIGHTER"
)


def get_rate_limiter(exchange: Exchange) -> TokenBucketRateLimiter:
    """Get rate limiter for exchange"""
    if exchange == Exchange.X10:
        return X10_RATE_LIMITER
    elif exchange == Exchange.LIGHTER:
        return LIGHTER_RATE_LIMITER
    else:
        raise ValueError(f"Unknown exchange: {exchange}")


# ═══════════════════════════════════════════════════════════════
# DECORATOR FOR RATE-LIMITED FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def rate_limited(exchange: Exchange, tokens: float = 1.0):
    """
    Decorator to rate-limit async functions.
    
    Usage:
        @rate_limited(Exchange.LIGHTER)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            limiter = get_rate_limiter(exchange)
            await limiter.acquire(tokens)
            try:
                result = await func(*args, **kwargs)
                limiter.on_success()
                return result
            except Exception as e:
                if "429" in str(e).lower() or "rate" in str(e).lower():
                    limiter.penalize_429()
                raise
        return wrapper
    return decorator


def with_rate_limit(limiter: TokenBucketRateLimiter, tokens: float = 1.0):
    """
    Decorator using specific limiter instance.
    
    Usage:
        @with_rate_limit(LIGHTER_RATE_LIMITER)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            await limiter.acquire(tokens)
            try:
                result = await func(*args, **kwargs)
                limiter.on_success()
                return result
            except Exception as e:
                if "429" in str(e).lower() or "rate" in str(e).lower():
                    limiter.penalize_429()
                raise
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_all_stats() -> Dict[str, Dict]:
    """Get stats for all rate limiters"""
    return {
        "X10": X10_RATE_LIMITER.get_stats(),
        "LIGHTER": LIGHTER_RATE_LIMITER.get_stats()
    }


async def reset_all_limiters():
    """Reset all rate limiters (for testing)"""
    for limiter in [X10_RATE_LIMITER, LIGHTER_RATE_LIMITER]:
        limiter._tokens = limiter.config.max_tokens
        limiter._penalty_until = 0.0
        limiter._consecutive_429s = 0
        limiter._current_penalty = limiter.config.penalty_429_seconds
        limiter._shutdown = False
        limiter._waiters.clear()


def shutdown_all_rate_limiters():
    """
    Shutdown all rate limiters - cancel all waiting tasks.
    
    This should be called early in the bot shutdown sequence,
    BEFORE trying to close positions, to prevent rate-limited
    operations from blocking or hanging during shutdown.
    
    Returns:
        Dict with shutdown stats for each limiter
    """
    stats = {}
    for name, limiter in [("X10", X10_RATE_LIMITER), ("LIGHTER", LIGHTER_RATE_LIMITER)]:
        waiter_count = len(limiter._waiters)
        limiter.shutdown()
        stats[name] = {
            "waiters_cancelled": waiter_count,
            "shutdown": limiter._shutdown
        }
    
    logger.info(f"🛑 All rate limiters shutdown: {stats}")
    return stats


def are_rate_limiters_shutdown() -> bool:
    """Check if all rate limiters are in shutdown mode."""
    return X10_RATE_LIMITER.is_shutdown and LIGHTER_RATE_LIMITER.is_shutdown
