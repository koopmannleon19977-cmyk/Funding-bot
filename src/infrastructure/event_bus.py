from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from src.core.events import Event
from src.core.interfaces import EventBusInterface

TEvent = TypeVar("TEvent", bound="Event")


Handler = Callable[[Event], Awaitable[Any]]


class EventBus(EventBusInterface):
    """In-memory async event bus (pub/sub)."""

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._loop_task: asyncio.Task | None = None

    def subscribe(self, event_type: type[TEvent], handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task

    async def _run(self) -> None:
        while self._running:
            event = await self._queue.get()
            await self._dispatch(event)

    async def _dispatch(self, event: Event) -> None:
        handlers = self._handlers.get(type(event), [])
        await asyncio.gather(*(handler(event) for handler in handlers))
