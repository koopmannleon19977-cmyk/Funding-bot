import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Type, TypeVar


TEvent = TypeVar("TEvent")
Handler = Callable[[TEvent], Awaitable[None]]


@dataclass(frozen=True)
class TradeOpened:
    trade_id: str
    symbol: str
    timestamp: datetime


@dataclass(frozen=True)
class TradeClosed:
    trade_id: str
    symbol: str
    timestamp: datetime


@dataclass(frozen=True)
class MaintenanceViolation:
    trade_id: str
    symbol: str
    reason: str
    timestamp: datetime


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[Type, List[Handler]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    def subscribe(self, event_type: Type[TEvent], handler: Handler[TEvent]) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: TEvent) -> None:
        await self._queue.put(event)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            event = await self._queue.get()
            await self._dispatch(event)

    async def _dispatch(self, event: TEvent) -> None:
        handlers = self._handlers.get(type(event), [])
        await asyncio.gather(*(h(event) for h in handlers))

