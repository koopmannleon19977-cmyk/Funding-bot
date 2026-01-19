"""
Metrics storage backend for performance tracking.

Stores metrics in-memory with optional SQLite persistence for historical analysis.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """A single metric data point."""

    operation: str
    value: float
    unit: str  # "ms", "count", "percent", etc.
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class InMemoryRingBuffer:
    """Thread-safe in-memory ring buffer for recent metrics."""

    def __init__(self, max_size: int = 1000):
        self._buffer: deque[MetricPoint] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def add(self, point: MetricPoint) -> None:
        """Add a metric point to the buffer."""
        async with self._lock:
            self._buffer.append(point)

    async def get_recent(self, operation: str, limit: int = 100) -> list[MetricPoint]:
        """Get recent metrics for an operation."""
        async with self._lock:
            return [p for p in self._buffer if p.operation == operation][-limit:]

    async def get_all_recent(self, limit: int = 100) -> list[MetricPoint]:
        """Get all recent metrics."""
        async with self._lock:
            return list(self._buffer)[-limit:]

    async def clear(self) -> None:
        """Clear all metrics from the buffer."""
        async with self._lock:
            self._buffer.clear()


class MetricsStorage:
    """
    Metrics storage with in-memory ring buffer and optional SQLite persistence.

    In-memory buffer stores last N metrics for real-time monitoring.
    SQLite persists historical data for offline analysis.
    """

    def __init__(
        self,
        in_memory_size: int = 1000,
        persist_to_disk: bool = False,
        retention_days: int = 7,
    ):
        """
        Initialize metrics storage.

        Args:
            in_memory_size: Max metrics to keep in memory (ring buffer)
            persist_to_disk: Whether to persist metrics to SQLite
            retention_days: Days to keep persisted metrics
        """
        self._ring_buffer = InMemoryRingBuffer(max_size=in_memory_size)
        self._persist_to_disk = persist_to_disk
        self._retention_days = retention_days

        # SQLite storage (lazy initialization)
        self._db_path: str | None = None
        self._db_lock = asyncio.Lock()

    async def record(self, point: MetricPoint) -> None:
        """
        Record a metric point.

        Args:
            point: Metric data point to record
        """
        # Add to in-memory buffer
        await self._ring_buffer.add(point)

        # Optionally persist to disk
        if self._persist_to_disk:
            await self._persist_to_db(point)

    async def get_recent_metrics(
        self,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[MetricPoint]:
        """
        Get recent metrics from in-memory buffer.

        Args:
            operation: Filter by operation name (None = all operations)
            limit: Max number of metrics to return

        Returns:
            List of recent metric points
        """
        if operation:
            return await self._ring_buffer.get_recent(operation, limit)
        return await self._ring_buffer.get_all_recent(limit)

    async def get_percentiles(
        self,
        operation: str,
        percentiles: list[int],
        limit: int = 1000,
    ) -> dict[int, float]:
        """
        Calculate percentiles for an operation's metrics.

        Args:
            operation: Operation name to calculate percentiles for
            percentiles: List of percentiles to calculate (e.g., [50, 95, 99])
            limit: Max number of recent metrics to consider

        Returns:
            Dict mapping percentile -> value
        """
        metrics = await self._ring_buffer.get_recent(operation, limit)

        if not metrics:
            return {p: 0.0 for p in percentiles}

        # Extract values and sort
        values = sorted([m.value for m in metrics])

        # Calculate percentiles
        result = {}
        for p in percentiles:
            idx = int(len(values) * p / 100) - 1
            idx = max(0, min(idx, len(values) - 1))
            result[p] = values[idx]

        return result

    async def get_rate_limit_proximity(
        self,
        exchange: str,
        limit_type: str,  # "rest" or "websocket"
    ) -> float:
        """
        Get current rate limit usage percentage for an exchange.

        Args:
            exchange: Exchange name ("lighter" or "x10")
            limit_type: Type of limit ("rest" or "websocket")

        Returns:
            Percentage of limit used (0-100+)
        """
        # This will be implemented by the MetricsCollector
        # which tracks request counts per exchange
        return 0.0

    async def clear_memory(self) -> None:
        """Clear all metrics from in-memory buffer."""
        await self._ring_buffer.clear()

    async def _persist_to_db(self, point: MetricPoint) -> None:
        """Persist metric to SQLite database."""
        # TODO: Implement SQLite persistence
        # For now, we only use in-memory storage
        pass

    async def cleanup_old_metrics(self) -> None:
        """Remove metrics older than retention period."""
        # TODO: Implement SQLite cleanup
        pass
