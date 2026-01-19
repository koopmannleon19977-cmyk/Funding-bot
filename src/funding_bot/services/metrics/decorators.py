"""
Performance tracking decorators.

Provides decorators for tracking operation latency and metrics.
"""

import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from .collector import get_metrics_collector

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def track_latency(
    operation: str | None = None,
    metadata_extractor: Callable[..., dict[str, Any]] | None = None,
) -> Callable[[F], F]:
    """
    Decorator to track operation latency.

    Args:
        operation: Operation name (defaults to function name)
        metadata_extractor: Optional function to extract metadata from args/kwargs

    Example:
        ```python
        @track_latency("lighter.place_order")
        async def place_order(self, request: OrderRequest) -> Order:
            ...

        @track_latency(
            metadata_extractor=lambda self, symbol, **kwargs: {"symbol": symbol}
        )
        async def refresh_symbol(self, symbol: str) -> None:
            ...
        ```

    Returns:
        Decorated function with latency tracking
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            op_name = operation or f"{func.__module__}.{func.__name__}"
            start_time = time.perf_counter()

            try:
                result = await func(*args, **kwargs)

                # Record latency
                duration_ms = (time.perf_counter() - start_time) * 1000

                # Extract metadata if provided
                metadata = None
                if metadata_extractor:
                    try:
                        metadata = metadata_extractor(*args, **kwargs)
                    except Exception as e:
                        logger.debug(f"Failed to extract metadata: {e}")

                # Get collector and record
                try:
                    collector = await get_metrics_collector()
                    await collector.record_latency(op_name, duration_ms, metadata)
                except Exception as e:
                    # Don't let metrics recording break the function
                    logger.debug(f"Failed to record latency: {e}")

                return result

            except Exception as e:
                # Record failed attempt
                duration_ms = (time.perf_counter() - start_time) * 1000
                try:
                    collector = await get_metrics_collector()
                    await collector.record_latency(
                        f"{op_name}.failed",
                        duration_ms,
                        {"error": str(e)},
                    )
                except Exception:
                    pass

                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            op_name = operation or f"{func.__module__}.{func.__name__}"
            start_time = time.perf_counter()

            try:
                result = func(*args, **kwargs)

                # Record latency
                duration_ms = (time.perf_counter() - start_time) * 1000

                # Extract metadata if provided
                metadata = None
                if metadata_extractor:
                    try:
                        metadata = metadata_extractor(*args, **kwargs)
                    except Exception as e:
                        logger.debug(f"Failed to extract metadata: {e}")

                # Get collector and record (sync version)
                try:
                    # For sync functions, we need to handle the async collector
                    # Create a new event loop if none exists
                    import asyncio

                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # Running in async context, schedule the coroutine
                            asyncio.create_task(_record_latency_async(op_name, duration_ms, metadata))
                        else:
                            # No loop or not running, run in new loop
                            asyncio.run(_record_latency_async(op_name, duration_ms, metadata))
                    except RuntimeError:
                        # No event loop, create one
                        asyncio.run(_record_latency_async(op_name, duration_ms, metadata))
                except Exception as e:
                    # Don't let metrics recording break the function
                    logger.debug(f"Failed to record latency: {e}")

                return result

            except Exception as e:
                # Record failed attempt
                duration_ms = (time.perf_counter() - start_time) * 1000
                try:
                    import asyncio

                    asyncio.run(_record_latency_async(f"{op_name}.failed", duration_ms, {"error": str(e)}))
                except Exception:
                    pass

                raise

        # Return appropriate wrapper based on whether function is async
        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        else:
            return sync_wrapper  # type: ignore

    return decorator


async def _record_latency_async(
    operation: str,
    duration_ms: float,
    metadata: dict[str, Any] | None,
) -> None:
    """Helper to record latency asynchronously."""
    try:
        collector = await get_metrics_collector()
        await collector.record_latency(operation, duration_ms, metadata)
    except Exception as e:
        logger.debug(f"Failed to record latency: {e}")


def track_operation(
    operation_type: str,  # "api_request", "ws_message", "db_query", etc.
    exchange: str | None = None,
) -> Callable[[F], F]:
    """
    Decorator to track specific operation types.

    Args:
        operation_type: Type of operation being tracked
        exchange: Optional exchange name (for API/WS operations)

    Example:
        ```python
        @track_operation("api_request", exchange="lighter")
        async def get_account(self) -> Account:
            ...

        @track_operation("db_query")
        async def get_trade(self, trade_id: str) -> Trade:
            ...
        ```

    Returns:
        Decorated function with operation tracking
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.perf_counter()

            try:
                result = await func(*args, **kwargs)

                # Record based on operation type
                duration_ms = (time.perf_counter() - start_time) * 1000

                try:
                    collector = await get_metrics_collector()

                    if operation_type == "api_request" and exchange:
                        # Extract endpoint from function name
                        endpoint = func.__name__
                        await collector.record_api_request(exchange, endpoint, duration_ms)

                    elif operation_type == "db_query":
                        # Extract query type from function name
                        query_type = func.__name__.split("_")[0]  # get_, insert_, etc.
                        await collector.record_db_query(query_type, duration_ms)

                    else:
                        # Generic latency tracking
                        op_name = (
                            f"{exchange}.{operation_type}.{func.__name__}"
                            if exchange
                            else f"{operation_type}.{func.__name__}"
                        )
                        await collector.record_latency(op_name, duration_ms)

                except Exception as e:
                    logger.debug(f"Failed to record operation: {e}")

                return result

            except Exception:
                duration_ms = (time.perf_counter() - start_time) * 1000
                # Record failure
                try:
                    collector = await get_metrics_collector()
                    await collector.record_latency(
                        f"{operation_type}.{func.__name__}.failed",
                        duration_ms,
                    )
                except Exception:
                    pass
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.perf_counter()

            try:
                result = func(*args, **kwargs)

                # Record based on operation type (sync version)
                duration_ms = (time.perf_counter() - start_time) * 1000

                try:
                    # For sync functions, schedule async recording
                    import asyncio

                    asyncio.create_task(_record_operation_sync(operation_type, exchange, func, duration_ms))
                except Exception as e:
                    logger.debug(f"Failed to record operation: {e}")

                return result

            except Exception:
                duration_ms = (time.perf_counter() - start_time) * 1000
                try:
                    import asyncio

                    asyncio.create_task(
                        _record_operation_sync(operation_type, exchange, func, duration_ms, failed=True)
                    )
                except Exception:
                    pass
                raise

        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        else:
            return sync_wrapper  # type: ignore

    return decorator


async def _record_operation_sync(
    operation_type: str,
    exchange: str | None,
    func: Callable,
    duration_ms: float,
    failed: bool = False,
) -> None:
    """Helper to record operation asynchronously from sync context."""
    try:
        collector = await get_metrics_collector()

        if failed:
            await collector.record_latency(
                f"{operation_type}.{func.__name__}.failed",
                duration_ms,
            )
        elif operation_type == "api_request" and exchange:
            endpoint = func.__name__
            await collector.record_api_request(exchange, endpoint, duration_ms)
        elif operation_type == "db_query":
            query_type = func.__name__.split("_")[0]
            await collector.record_db_query(query_type, duration_ms)
        else:
            op_name = (
                f"{exchange}.{operation_type}.{func.__name__}" if exchange else f"{operation_type}.{func.__name__}"
            )
            await collector.record_latency(op_name, duration_ms)

    except Exception as e:
        logger.debug(f"Failed to record operation: {e}")


class track_operation_context:
    """
    Context manager for tracking operations.

    Useful for tracking code blocks that aren't easily decorated.

    Example:
        ```python
        async with track_operation_context("market_data.refresh", symbol="BTC-USD") as tracker:
            result = await complex_operation()
            tracker.set_metadata({"rows": len(result)})
        ```
    """

    def __init__(
        self,
        operation: str,
        metadata: dict[str, Any] | None = None,
    ):
        self._operation = operation
        self._metadata = metadata or {}
        self._start_time: float | None = None

    async def __aenter__(self):
        self._start_time = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_time is None:
            return

        duration_ms = (time.perf_counter() - self._start_time) * 1000

        try:
            collector = await get_metrics_collector()

            if exc_type is not None:
                self._metadata["error"] = str(exc_val)
                await collector.record_latency(
                    f"{self._operation}.failed",
                    duration_ms,
                    self._metadata,
                )
            else:
                await collector.record_latency(
                    self._operation,
                    duration_ms,
                    self._metadata,
                )
        except Exception as e:
            logger.debug(f"Failed to record operation: {e}")

    def set_metadata(self, key: str, value: Any) -> None:
        """Set a metadata key-value pair."""
        self._metadata[key] = value

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Update metadata with multiple key-value pairs."""
        self._metadata.update(metadata)
