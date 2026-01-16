"""
In-Memory Event Bus Implementation.

Simple pub/sub implementation for domain events.
All handlers are async and exceptions are logged (not propagated).

🚀 PERFORMANCE: Fixed task memory accumulation with proper cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TypeVar

from funding_bot.domain.events import DomainEvent
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort

logger = get_logger(__name__)

T = TypeVar("T", bound=DomainEvent)


class InMemoryEventBus(EventBusPort):
    """
    In-memory async event bus with automatic task cleanup.

    Features:
    - Type-safe subscriptions
    - Async handlers
    - Exception isolation (one failing handler doesn't affect others)
    - Graceful shutdown
    - 🚀 PERFORMANCE: Automatic task cleanup to prevent memory leaks
    """

    def __init__(self):
        self._handlers: dict[type[DomainEvent], set[Callable[[DomainEvent], Awaitable[None]]]] = defaultdict(set)
        self._running = False
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self._processor_task: asyncio.Task | None = None

        # 🚀 PERFORMANCE: Track active handler tasks for cleanup
        self._active_tasks: set[asyncio.Task] = set()
        self._task_cleanup_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the event bus processor."""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(
            self._process_events(),
            name="event_bus_processor"
        )
        logger.debug("Event bus started")

    async def stop(self) -> None:
        """Stop the event bus and drain remaining events."""
        if not self._running:
            return

        self._running = False

        # Process remaining events
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                await self._dispatch(event)
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.exception(f"Error processing event during shutdown: {e}")

        # Cancel processor
        if self._processor_task:
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
            self._processor_task = None

        # 🚀 PERFORMANCE: Wait for active handler tasks to complete
        await self._cleanup_active_tasks(timeout=5.0)

        logger.debug("Event bus stopped")

    def subscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """Subscribe to events of a specific type."""
        self._handlers[event_type].add(handler)  # type: ignore
        logger.debug(f"Subscribed to {event_type.__name__}")

    def unsubscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """Unsubscribe a handler from an event type."""
        self._handlers[event_type].discard(handler)  # type: ignore

    async def publish(self, event: DomainEvent) -> None:
        """
        Publish an event to all subscribers.

        Events are queued and processed asynchronously.
        """
        if not self._running:
            # If not started, dispatch directly
            await self._dispatch(event)
            return

        await self._queue.put(event)

    def subscriber_count(self, event_type: type[DomainEvent]) -> int:
        """Get number of subscribers for an event type."""
        return len(self._handlers.get(event_type, set()))

    async def _process_events(self) -> None:
        """Background task to process events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )
                await self._dispatch(event)

                # 🚀 PERFORMANCE: Periodic cleanup of completed tasks
                if len(self._active_tasks) > 100:  # Only cleanup if many tasks
                    await self._cleanup_active_tasks(timeout=0.1)

            except TimeoutError:
                # Periodic cleanup on timeout too
                if len(self._active_tasks) > 0:
                    await self._cleanup_active_tasks(timeout=0.1)
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Event processor error: {e}")

    async def _dispatch(self, event: DomainEvent) -> None:
        """
        Dispatch event to all registered handlers.

        🚀 PERFORMANCE: Uses TaskGroup (Python 3.11+) or manual cleanup
        to ensure proper task cleanup and prevent memory leaks.
        """
        event_type = type(event)
        handlers = self._handlers.get(event_type, set())

        if not handlers:
            logger.debug(f"No handlers for {event_type.__name__}")
            return

        # Use TaskGroup (Python 3.11+)
        await self._dispatch_with_taskgroup(handlers, event)

    async def _dispatch_with_taskgroup(
        self,
        handlers: set[Callable[[DomainEvent], Awaitable[None]]],
        event: DomainEvent,
    ) -> None:
        """Dispatch handlers using TaskGroup (automatic cleanup)."""
        try:
            async with asyncio.TaskGroup() as tg:
                for handler in handlers:
                    tg.create_task(self._safe_call(handler, event))
        except* (Exception, asyncio.CancelledError):
            # TaskGroup will handle cleanup
            pass

    async def _dispatch_with_manual_cleanup(
        self,
        handlers: set[Callable[[DomainEvent], Awaitable[None]]],
        event: DomainEvent,
    ) -> None:
        """Dispatch handlers with manual task cleanup for older Python versions."""
        # Create tasks
        tasks = [
            asyncio.create_task(self._safe_call(handler, event))
            for handler in handlers
        ]

        # Track tasks for cleanup
        async with self._task_cleanup_lock:
            self._active_tasks.update(tasks)

        try:
            # Wait for all tasks to complete
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Clean up completed tasks
            async with self._task_cleanup_lock:
                for task in tasks:
                    self._active_tasks.discard(task)

    async def _cleanup_active_tasks(self, timeout: float = 5.0) -> None:
        """
        Clean up completed handler tasks.

        🚀 PERFORMANCE: Prevents memory accumulation from completed tasks.
        """
        async with self._task_cleanup_lock:
            if not self._active_tasks:
                return

            # Separate completed from active tasks
            completed_tasks = {t for t in self._active_tasks if t.done()}
            active_tasks = self._active_tasks - completed_tasks

            # Wait for pending tasks to complete (with timeout)
            if active_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*active_tasks, return_exceptions=True),
                        timeout=timeout
                    )
                except TimeoutError:
                    # Cancel any still-running tasks
                    for task in active_tasks:
                        if not task.done():
                            task.cancel()
                    # Wait for cancellation to complete
                    await asyncio.gather(*active_tasks, return_exceptions=True)

            # Update active tasks set
            self._active_tasks = {t for t in self._active_tasks if not t.done()}

            if completed_tasks:
                logger.debug(f"Cleaned up {len(completed_tasks)} completed event handler tasks")

    async def _safe_call(
        self,
        handler: Callable[[DomainEvent], Awaitable[None]],
        event: DomainEvent,
    ) -> None:
        """Call handler with exception isolation."""
        try:
            await handler(event)
        except Exception as e:
            handler_name = getattr(handler, "__name__", repr(handler))
            logger.exception(
                f"Handler {handler_name} failed for {type(event).__name__}: {e}"
            )

