from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Type, TypeVar

TEvent = TypeVar("TEvent", bound="Event")


class Event:
    """Base event type."""


Handler = Callable[[Event], Awaitable[Any]]


class EventBus:
    """In-memory async event bus (pub/sub)."""

    def __init__(self) -> None:
        self._handlers: Dict[Type[Event], List[Handler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._loop_task: asyncio.Task | None = None

    def subscribe(self, event_type: Type[TEvent], handler: Handler) -> None:
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


@dataclass(frozen=True)
class TradeOpened(Event):
    trade_id: str
    symbol: str
    timestamp: datetime


@dataclass(frozen=True)
class TradeClosed(Event):
    trade_id: str
    symbol: str
    timestamp: datetime


@dataclass(frozen=True)
class MaintenanceViolation(Event):
    trade_id: str
    symbol: str
    reason: str
    timestamp: datetime
