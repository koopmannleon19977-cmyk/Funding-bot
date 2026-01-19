"""
Rate Limit Awareness Service

Provides intelligent rate limit handling with proactive backoff
to avoid 429/503 errors and optimize API usage.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque

from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


class RateLimitAwareExecutor:
    """
    Intelligent rate limit handling with proactive backoff.

    Features:
    - Tracks API usage per minute
    - Proactive backoff when approaching limits
    - Exponential backoff on rate limit errors
    - Per-exchange rate limit tracking
    """

    def __init__(
        self,
        exchange_name: str,
        requests_per_minute: int = 24000,  # Lighter Premium default
        warning_threshold: float = 0.8,  # 80% = Backoff starten
        critical_threshold: float = 0.95,  # 95% = Stopp
    ):
        """
        Initialize rate limiter.

        Args:
            exchange_name: Name of exchange (for logging)
            requests_per_minute: Rate limit (default: Lighter Premium)
            warning_threshold: Usage % to trigger backoff
            critical_threshold: Usage % to stop requests
        """
        self.exchange_name = exchange_name
        self.requests_per_minute = requests_per_minute
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold

        # Track request timestamps (rolling 60-second window)
        self.request_history = deque(maxlen=requests_per_minute)

        # Backoff state
        self.backoff_until = 0
        self.consecutive_errors = 0
        self.max_consecutive_errors = 3

        # Statistics
        self.total_requests = 0
        self.total_backoffs = 0
        self.total_errors = 0

    def _clean_old_requests(self) -> None:
        """Remove requests older than 60 seconds."""
        now = time.time()
        cutoff = now - 60
        while self.request_history and self.request_history[0] < cutoff:
            self.request_history.popleft()

    def _get_usage_pct(self) -> float:
        """Calculate current rate limit usage as percentage."""
        self._clean_old_requests()
        return len(self.request_history) / self.requests_per_minute

    def _calculate_backoff(self, usage_pct: float) -> float:
        """Calculate backoff time based on usage percentage."""
        if usage_pct < 0.8:
            return 0.0
        elif usage_pct < 0.9:
            return 2.0  # 2 seconds at 80-90%
        elif usage_pct < 0.95:
            return 5.0  # 5 seconds at 90-95%
        else:
            return 10.0  # 10 seconds at >95%

    async def execute(
        self,
        request_func,
        label: str = "request",
    ):
        """
        Execute request with rate limit awareness.

        Args:
            request_func: Async function to execute
            label: Description for logging

        Returns:
            Response from request_func

        Raises:
            Exception: If request fails after retries
        """
        # 1. Wait if in backoff
        now = time.time()
        if now < self.backoff_until:
            wait_time = self.backoff_until - now
            logger.info(f"[RateLimit:{self.exchange_name}] In backoff, waiting {wait_time:.1f}s (label={label})")
            import asyncio

            await asyncio.sleep(wait_time)

        # 2. Check rate limit usage
        usage_pct = self._get_usage_pct()

        if usage_pct >= self.critical_threshold:
            # Critical: Wait until requests expire
            if self.request_history:
                wait_time = 60 - (now - self.request_history[0])
                logger.warning(
                    f"[RateLimit:{self.exchange_name}] CRITICAL: {usage_pct:.1%} usage, "
                    f"waiting {wait_time:.1f}s (label={label})"
                )
                import asyncio

                await asyncio.sleep(wait_time)

        elif usage_pct >= self.warning_threshold:
            # Warning: Proactive backoff
            backoff = self._calculate_backoff(usage_pct)
            if backoff > 0:
                logger.info(
                    f"[RateLimit:{self.exchange_name}] {usage_pct:.1%} usage, "
                    f"proactive backoff {backoff:.1f}s (label={label})"
                )
                self.total_backoffs += 1
                import asyncio

                await asyncio.sleep(backoff)

        # 3. Execute request
        self.total_requests += 1
        request_time = time.time()

        try:
            response = await request_func()
            self.request_history.append(request_time)
            self.consecutive_errors = 0  # Reset on success
            return response

        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = (
                "429" in error_str  # Too Many Requests
                or "503" in error_str  # Service Unavailable
                or "rate limit" in error_str
                or "too many requests" in error_str
            )

            if is_rate_limit:
                self.total_errors += 1
                self.consecutive_errors += 1

                # Parse Retry-After header if available
                retry_after = 5  # Default 5 seconds
                if hasattr(e, "response") and hasattr(e.response, "headers"):
                    retry_after_header = e.response.headers.get("Retry-After")
                    if retry_after_header:
                        with contextlib.suppress(ValueError):
                            retry_after = int(retry_after_header)

                self.backoff_until = time.time() + retry_after
                logger.warning(
                    f"[RateLimit:{self.exchange_name}] Hit rate limit, "
                    f"backing off {retry_after}s (consecutive={self.consecutive_errors}, "
                    f"label={label})"
                )

                import asyncio

                await asyncio.sleep(retry_after)

                # Retry once
                try:
                    response = await request_func()
                    self.request_history.append(time.time())
                    self.consecutive_errors = 0
                    return response
                except Exception as retry_e:
                    logger.error(f"[RateLimit:{self.exchange_name}] Retry failed: {retry_e}")
                    raise

            # Not a rate limit error or retry exhausted
            raise

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        return {
            "exchange": self.exchange_name,
            "requests_per_minute": self.requests_per_minute,
            "current_usage": f"{self._get_usage_pct():.1%}",
            "total_requests": self.total_requests,
            "total_backoffs": self.total_backoffs,
            "total_errors": self.total_errors,
            "consecutive_errors": self.consecutive_errors,
        }

    def reset(self) -> None:
        """Reset rate limiter state (for testing)."""
        self.request_history.clear()
        self.backoff_until = 0
        self.consecutive_errors = 0
        self.total_requests = 0
        self.total_backoffs = 0
        self.total_errors = 0


class RateLimiterManager:
    """
    Manages multiple rate limiters for different exchanges/endpoints.
    """

    def __init__(self):
        self.limiters = {}

    def get_limiter(self, exchange: str, endpoint: str = "default", **kwargs) -> RateLimitAwareExecutor:
        """
        Get or create rate limiter for exchange/endpoint.

        Args:
            exchange: Exchange name (LIGHTER, X10)
            endpoint: Endpoint identifier (default, funding, trading, etc.)
            **kwargs: Passed to RateLimitAwareExecutor

        Returns:
            RateLimitAwareExecutor instance
        """
        key = f"{exchange}:{endpoint}"

        if key not in self.limiters:
            # Set default limits based on exchange
            if exchange == "LIGHTER":
                requests_per_minute = kwargs.get(
                    "requests_per_minute",
                    24000,  # Premium Tier
                )
            elif exchange == "X10":
                requests_per_minute = kwargs.get(
                    "requests_per_minute",
                    10000,  # X10 has no published limit, use conservative
                )
            else:
                requests_per_minute = kwargs.get("requests_per_minute", 1000)

            self.limiters[key] = RateLimitAwareExecutor(
                exchange_name=key,
                requests_per_minute=requests_per_minute,
                **{k: v for k, v in kwargs.items() if k != "requests_per_minute"},
            )

        return self.limiters[key]

    def get_all_stats(self) -> list[dict]:
        """Get statistics for all limiters."""
        return [limiter.get_stats() for limiter in self.limiters.values()]

    def reset_all(self) -> None:
        """Reset all limiters (for testing)."""
        for limiter in self.limiters.values():
            limiter.reset()


# Global instance
_rate_limiter_manager = RateLimiterManager()


def get_rate_limiter_manager() -> RateLimiterManager:
    """Get global rate limiter manager instance."""
    return _rate_limiter_manager
