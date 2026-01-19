"""
Event Bus Port: Abstract interface for pub/sub event system.

Used for loose coupling between services.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TypeVar

from funding_bot.domain.events import DomainEvent

T = TypeVar("T", bound=DomainEvent)
EventHandler = Callable[[DomainEvent], Awaitable[None]]


class EventBusPort(ABC):
    """
    Abstract interface for event bus.

    Supports publish/subscribe pattern for domain events.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the event bus (if needed)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the event bus and cleanup."""
        ...

    @abstractmethod
    def subscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """
        Subscribe to events of a specific type.

        Args:
            event_type: The event class to subscribe to.
            handler: Async function to call when event is published.
        """
        ...

    @abstractmethod
    def unsubscribe(
        self,
        event_type: type[T],
        handler: Callable[[T], Awaitable[None]],
    ) -> None:
        """Unsubscribe a handler from an event type."""
        ...

    @abstractmethod
    async def publish(self, event: DomainEvent) -> None:
        """
        Publish an event to all subscribers.

        Args:
            event: The domain event to publish.
        """
        ...

    @abstractmethod
    def subscriber_count(self, event_type: type[DomainEvent]) -> int:
        """Get number of subscribers for an event type."""
        ...
