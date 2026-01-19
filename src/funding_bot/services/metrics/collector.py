"""
Centralized metrics collector for performance monitoring.

Tracks latency, throughput, and rate limit usage for all bot operations.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .storage import MetricPoint, MetricsStorage

logger = logging.getLogger(__name__)


@dataclass
class RateLimitTracker:
    """Track rate limit usage for an exchange."""

    requests_made: int = 0
    window_start: float = field(default_factory=time.monotonic)
    window_duration: float = 60.0  # 1 minute rolling window


@dataclass
class ExchangeLimits:
    """Rate limits for an exchange."""

    exchange: str
    rest_requests_per_minute: int
    ws_messages_per_minute: int
    max_inflight_messages: int

    # Premium account limits (Lighter)
    is_premium: bool = False


# Exchange rate limits
EXCHANGE_LIMITS: dict[str, ExchangeLimits] = {
    "lighter": ExchangeLimits(
        exchange="lighter",
        rest_requests_per_minute=24000,  # Premium!
        ws_messages_per_minute=200,  # Same for all accounts
        max_inflight_messages=50,  # Same for all accounts
        is_premium=True,
    ),
    "x10": ExchangeLimits(
        exchange="x10",
        rest_requests_per_minute=1000,  # Per IP
        ws_messages_per_minute=100,  # Assumed
        max_inflight_messages=50,  # Assumed
        is_premium=False,
    ),
}


class MetricsCollector:
    """
    Centralized performance metrics collection.

    Tracks:
    - Operation latency (P50, P95, P99)
    - Rate limit usage per exchange
    - Request counts per endpoint
    - WebSocket message rates
    - Database query durations
    """

    def __init__(self, storage: MetricsStorage | None = None):
        """
        Initialize metrics collector.

        Args:
            storage: Metrics storage backend (creates default if None)
        """
        self._storage = storage or MetricsStorage(
            in_memory_size=1000,
            persist_to_disk=False,
        )

        # Rate limit tracking
        self._rate_limit_tracking: dict[str, RateLimitTracker] = defaultdict(lambda: RateLimitTracker())

        # Request counters
        self._request_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        # Active operation tracking (for concurrent operations)
        self._active_operations: dict[str, int] = defaultdict(int)
        self._active_ops_lock = asyncio.Lock()

        # Rate limit proximity warnings (to avoid spam)
        self._warning_times: dict[str, float] = {}

    async def record_latency(
        self,
        operation: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Record operation latency.

        Args:
            operation: Operation name (e.g., "lighter.place_order")
            duration_ms: Duration in milliseconds
            metadata: Optional metadata (symbol, side, etc.)
        """
        point = MetricPoint(
            operation=operation,
            value=duration_ms,
            unit="ms",
            metadata=metadata or {},
        )
        await self._storage.record(point)

        # Log slow operations
        if duration_ms > 1000:  # >1 second
            logger.warning(
                f"Slow operation: {operation} took {duration_ms:.2f}ms",
                extra=metadata or {},
            )

    async def record_count(
        self,
        operation: str,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Record a count metric.

        Args:
            operation: Operation name
            count: Count value
            metadata: Optional metadata
        """
        point = MetricPoint(
            operation=operation,
            value=float(count),
            unit="count",
            metadata=metadata or {},
        )
        await self._storage.record(point)

    async def record_api_request(
        self,
        exchange: str,
        endpoint: str,
        duration_ms: float,
    ) -> None:
        """
        Record an API request to an exchange.

        Args:
            exchange: Exchange name ("lighter" or "x10")
            endpoint: API endpoint called
            duration_ms: Request duration in milliseconds
        """
        operation = f"{exchange}.{endpoint}"
        await self.record_latency(operation, duration_ms)

        # Track request count for rate limiting
        self._request_counts[exchange][endpoint] += 1
        await self._update_rate_limit_tracking(exchange)

    async def record_websocket_message(
        self,
        exchange: str,
        message_type: str,  # "sent" or "received"
        duration_ms: float | None = None,
    ) -> None:
        """
        Record a WebSocket message.

        Args:
            exchange: Exchange name
            message_type: Type of message ("sent" or "received")
            duration_ms: Optional processing duration
        """
        operation = f"{exchange}.ws_{message_type}"

        if duration_ms is not None:
            await self.record_latency(operation, duration_ms)

        await self.record_count(f"{exchange}.ws_messages", 1)

    async def record_db_query(
        self,
        query_type: str,
        duration_ms: float,
        row_count: int | None = None,
    ) -> None:
        """
        Record a database query.

        Args:
            query_type: Type of query ("select", "insert", "update", "delete")
            duration_ms: Query duration in milliseconds
            row_count: Optional number of rows affected
        """
        operation = f"db.{query_type}"
        metadata = {"rows": row_count} if row_count is not None else {}
        await self.record_latency(operation, duration_ms, metadata)

    async def get_percentiles(
        self,
        operation: str,
        percentiles: list[int] | None = None,
    ) -> dict[int, float]:
        """
        Get latency percentiles for an operation.

        Args:
            operation: Operation name
            percentiles: List of percentiles to calculate

        Returns:
            Dict mapping percentile -> value in milliseconds
        """
        if percentiles is None:
            percentiles = [50, 95, 99]
        return await self._storage.get_percentiles(operation, percentiles)

    async def get_rate_limit_proximity(
        self,
        exchange: str,
        limit_type: str = "rest",  # "rest" or "websocket"
    ) -> float:
        """
        Get rate limit usage percentage for an exchange.

        Args:
            exchange: Exchange name
            limit_type: Type of limit ("rest" or "websocket")

        Returns:
            Percentage of limit used (0-100+)
        """
        limits = EXCHANGE_LIMITS.get(exchange)
        if not limits:
            return 0.0

        tracker = self._rate_limit_tracking[f"{exchange}.{limit_type}"]

        # Calculate usage in rolling window
        now = time.monotonic()
        window_elapsed = now - tracker.window_start

        # Reset window if duration exceeded
        if window_elapsed >= tracker.window_duration:
            tracker.requests_made = 0
            tracker.window_start = now
            window_elapsed = 0.0

        # Get limit for this type
        if limit_type == "rest":
            limit = limits.rest_requests_per_minute
        elif limit_type == "websocket":
            limit = limits.ws_messages_per_minute
        else:
            return 0.0

        # Calculate percentage (annualized to per-minute)
        rate_per_minute = tracker.requests_made * (60.0 / window_elapsed) if window_elapsed > 0 else 0.0

        percentage = (rate_per_minute / limit) * 100.0

        # Warn if approaching limit
        if percentage > 80.0:
            warning_key = f"{exchange}.{limit_type}"
            last_warning = self._warning_times.get(warning_key, 0.0)

            # Only warn once per minute
            if now - last_warning > 60.0:
                logger.warning(
                    f"Rate limit proximity: {exchange} {limit_type} at {percentage:.1f}% "
                    f"({rate_per_minute:.0f}/{limit} requests/minute)"
                )
                self._warning_times[warning_key] = now

        return percentage

    async def get_metrics_summary(self) -> dict[str, Any]:
        """
        Get a summary of all metrics.

        Returns:
            Dict with metric summaries
        """
        return {
            "rate_limits": {
                "lighter": {
                    "rest": await self.get_rate_limit_proximity("lighter", "rest"),
                    "websocket": await self.get_rate_limit_proximity("lighter", "websocket"),
                },
                "x10": {
                    "rest": await self.get_rate_limit_proximity("x10", "rest"),
                    "websocket": await self.get_rate_limit_proximity("x10", "websocket"),
                },
            },
            "request_counts": dict(self._request_counts),
        }

    async def _update_rate_limit_tracking(self, exchange: str) -> None:
        """Update rate limit tracking for an exchange."""
        # Track REST requests
        rest_tracker = self._rate_limit_tracking[f"{exchange}.rest"]
        rest_tracker.requests_made += 1

    async def increment_inflight_messages(self, exchange: str) -> None:
        """Increment inflight message count for an exchange."""
        await self.record_count(f"{exchange}.inflight_messages", 1)

    async def decrement_inflight_messages(self, exchange: str) -> None:
        """Decrement inflight message count for an exchange."""
        await self.record_count(f"{exchange}.inflight_messages", -1)

    async def clear_all_metrics(self) -> None:
        """Clear all stored metrics."""
        await self._storage.clear_memory()
        self._rate_limit_tracking.clear()
        self._request_counts.clear()


# Global metrics collector instance
_collector: MetricsCollector | None = None
_collector_lock = asyncio.Lock()


async def get_metrics_collector() -> MetricsCollector:
    """
    Get the global metrics collector instance.

    Returns:
        MetricsCollector singleton instance
    """
    global _collector

    if _collector is None:
        async with _collector_lock:
            if _collector is None:
                _collector = MetricsCollector()

    return _collector
