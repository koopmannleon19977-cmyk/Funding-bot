# src/rate_limiter.py
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

class TokenBucketLimiter:
    """
    Token Bucket Rate Limiter
    
    Args:
        max_tokens: Maximum tokens in bucket
        refill_per_second: Tokens added per second
        name: Identifier for logging
    """
    
    def __init__(self, max_tokens: int, refill_per_second: float, name: str = "limiter"):
        self.max_tokens = max_tokens
        self.refill_per_second = refill_per_second
        self.name = name
        self.tokens = float(max_tokens)
        self.last_refill = time.time()
        self.lock = asyncio.Lock()
        
    async def acquire(self, tokens: float = 1.0) -> bool:
        """
        Acquire tokens (blocking until available)
        
        Returns:
            True when tokens acquired
        """
        async with self.lock:
            while True:
                now = time.time()
                elapsed = now - self.last_refill
                self.tokens = min(
                    self.max_tokens,
                    self.tokens + (elapsed * self.refill_per_second)
                )
                self.last_refill = now
                
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                
                deficit = tokens - self.tokens
                wait_time = deficit / self.refill_per_second
                
                self.lock.release()
                await asyncio.sleep(wait_time)
                await self.lock.acquire()

class AdaptiveRateLimiter:
    """
    Adaptive Rate Limiter (adjusts to 429 errors)
    
    Args:
        initial_rate: Starting requests per second
        min_rate: Minimum rate (safety floor)
        max_rate: Maximum rate (ceiling)
        name: Identifier for logging
    """
    
    def __init__(
        self,
        initial_rate: float = 10.0,
        min_rate: float = 1.0,
        max_rate: float = 20.0,
        name: str = "adaptive"
    ):
        self.current_rate = initial_rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.name = name
        
        self.bucket = TokenBucketLimiter(
            max_tokens=int(initial_rate * 2),
            refill_per_second=initial_rate,
            name=name
        )
        
        self.consecutive_429s = 0
        self.consecutive_successes = 0
        self.last_adjustment = time.time()
        
    async def acquire(self) -> bool:
        return await self.bucket.acquire()
    
    def on_success(self):
        self.consecutive_429s = 0
        self.consecutive_successes += 1
        
        if self.consecutive_successes >= 50:
            now = time.time()
            if now - self.last_adjustment > 30:
                old_rate = self.current_rate
                self.current_rate = min(self.max_rate, self.current_rate * 1.2)
                
                if self.current_rate != old_rate:
                    logger.info(
                        f"📈 {self.name}: Rate increased "
                        f"{old_rate:.1f} → {self.current_rate:.1f} req/s"
                    )
                    
                    self.bucket.refill_per_second = self.current_rate
                    self.bucket.max_tokens = int(self.current_rate * 2)
                    
                    self.last_adjustment = now
                    self.consecutive_successes = 0
    
    def on_429(self):
        self.consecutive_successes = 0
        self.consecutive_429s += 1
        
        old_rate = self.current_rate
        
        if self.consecutive_429s == 1:
            self.current_rate = max(self.min_rate, self.current_rate * 0.5)
        elif self.consecutive_429s == 2:
            self.current_rate = max(self.min_rate, self.current_rate * 0.3)
        else:
            self.current_rate = self.min_rate
        
        logger.warning(
            f"⚠️ {self.name}: 429 detected! "
            f"Rate reduced {old_rate:.1f} → {self.current_rate:.1f} req/s "
            f"(consecutive: {self.consecutive_429s})"
        )
        
        self.bucket.refill_per_second = self.current_rate
        self.bucket.max_tokens = int(self.current_rate * 2)
        self.last_adjustment = time.time()