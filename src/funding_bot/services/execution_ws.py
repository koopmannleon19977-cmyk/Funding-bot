"""
WebSocket order subscription and update handling for ExecutionEngine.

Extracted for maintainability - handles WS-driven order fill waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from funding_bot.domain.models import Exchange, Order
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.services.execution import ExecutionEngine

logger = get_logger("funding_bot.services.execution")


async def cleanup_stale_watchers(engine: ExecutionEngine) -> None:
    """
    Clean up stale order watchers and cached order updates.

    MEMORY MANAGEMENT: Periodically remove stale entries to prevent unbounded growth.
    This is a safety net in case the finally block in _wait_for_fill_ws() doesn't execute.

    Should be called periodically (e.g., every hour) from the supervisor.

    Args:
        engine: The ExecutionEngine instance
    """
    async with engine._ws_orders_lock:
        # Clean up empty watcher sets
        stale_keys = [key for key, watchers in engine._order_watchers.items() if not watchers]
        for key in stale_keys:
            engine._order_watchers.pop(key, None)

        # Clean up old order updates (keep last 100 per exchange to limit memory)
        max_cached_per_exchange = 100
        for exchange in (engine.lighter.exchange, engine.x10.exchange):
            # Get all keys for this exchange
            exchange_keys = [key for key in engine._last_order_update if key[0] == exchange]
            # Sort by some criterion (here: by key string) and keep only the newest
            if len(exchange_keys) > max_cached_per_exchange:
                # Remove oldest entries (first in the sorted list)
                keys_to_remove = exchange_keys[:-max_cached_per_exchange]
                for key in keys_to_remove:
                    engine._last_order_update.pop(key, None)

    logger.debug(
        f"Cleanup complete. Watchers: {len(engine._order_watchers)}, Cached orders: {len(engine._last_order_update)}"
    )


async def ensure_ws_order_subscriptions(engine: ExecutionEngine) -> None:
    """
    Register WS order callbacks once (best-effort).

    Args:
        engine: The ExecutionEngine instance
    """
    if engine._ws_orders_ready:
        return

    async with engine._ws_orders_lock:
        if engine._ws_orders_ready:
            return

        async def _subscribe_one(ex) -> None:
            with contextlib.suppress(Exception):
                # Use engine's method so it routes through the class
                await ex.subscribe_orders(engine._on_ws_order_update)

        await asyncio.gather(
            _subscribe_one(engine.lighter),
            _subscribe_one(engine.x10),
            return_exceptions=True,
        )

        engine._ws_orders_ready = True


async def on_ws_order_update(engine: ExecutionEngine, order: Order) -> None:
    """
    Dispatch WS order updates to any in-flight waiters.

    Args:
        engine: The ExecutionEngine instance
        order: The order update from WebSocket
    """
    keys: set[tuple[Exchange, str]] = {(order.exchange, str(order.order_id))}
    # Lighter fills can arrive on WS before we resolve the system order_id (we may be waiting
    # on a `pending_{client}_{market}` placeholder). To correlate fast, also index by client id.
    if order.client_order_id:
        keys.add((order.exchange, f"client:{order.client_order_id}"))

    async with engine._ws_orders_lock:
        for k in keys:
            engine._last_order_update[k] = order
        watchers: set[asyncio.Queue[Order]] = set()
        for k in keys:
            watchers |= engine._order_watchers.get(k, set())
        watchers_list = list(watchers)

    for q in watchers_list:
        with contextlib.suppress(Exception):
            if q.full():
                _ = q.get_nowait()
            q.put_nowait(order)
