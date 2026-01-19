"""
Domain Events.

Events are immutable records of things that happened in the domain.
They are used for:
- Audit logging
- Inter-service communication
- State reconstruction
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Side,
    TradeStatus,
)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base class for all domain events."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def event_type(self) -> str:
        return self.__class__.__name__


@dataclass(frozen=True, slots=True)
class TradeOpened(DomainEvent):
    """Emitted when a trade is fully opened (both legs filled)."""

    trade_id: str = ""
    symbol: str = ""
    leg1_exchange: Exchange = Exchange.LIGHTER
    leg2_exchange: Exchange = Exchange.X10
    qty: Decimal = Decimal("0")
    notional_usd: Decimal = Decimal("0")
    entry_apy: Decimal = Decimal("0")
    leg1_price: Decimal = Decimal("0")
    leg2_price: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class TradeClosed(DomainEvent):
    """Emitted when a trade is closed."""

    trade_id: str = ""
    symbol: str = ""
    reason: str = ""
    realized_pnl: Decimal = Decimal("0")
    funding_collected: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    hold_duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class TradeStateChanged(DomainEvent):
    """Emitted when trade status changes."""

    trade_id: str = ""
    symbol: str = ""
    old_status: TradeStatus = TradeStatus.PENDING
    new_status: TradeStatus = TradeStatus.PENDING
    execution_state: ExecutionState = ExecutionState.PENDING
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LegFilled(DomainEvent):
    """Emitted when a trade leg is filled."""

    trade_id: str = ""
    symbol: str = ""
    exchange: Exchange = Exchange.LIGHTER
    side: Side = Side.BUY
    order_id: str = ""
    qty: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class RollbackInitiated(DomainEvent):
    """Emitted when a rollback is started due to hedge failure."""

    trade_id: str = ""
    symbol: str = ""
    reason: str = ""
    leg_to_close: Exchange = Exchange.LIGHTER
    qty: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class RollbackCompleted(DomainEvent):
    """Emitted when rollback finishes."""

    trade_id: str = ""
    symbol: str = ""
    success: bool = False
    loss_usd: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class FundingCollected(DomainEvent):
    """Emitted when funding is collected for a trade."""

    trade_id: str = ""
    symbol: str = ""
    exchange: Exchange = Exchange.LIGHTER
    amount: Decimal = Decimal("0")
    cumulative: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PositionReconciled(DomainEvent):
    """Emitted when positions are reconciled."""

    symbol: str = ""
    exchange: Exchange = Exchange.LIGHTER
    action: str = ""  # "closed_zombie", "adopted_ghost", "ignored"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CircuitBreakerTripped(DomainEvent):
    """Emitted when circuit breaker activates."""

    reason: str = ""
    consecutive_failures: int = 0
    cooldown_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BrokenHedgeDetected(DomainEvent):
    """Emitted when a broken hedge is confirmed (one leg missing).

    This is a CRITICAL event that should trigger global trading pause.
    """

    trade_id: str = ""
    symbol: str = ""
    missing_exchange: Exchange = Exchange.LIGHTER
    remaining_qty: Decimal = Decimal("0")
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlertEvent(DomainEvent):
    """Generic alert for notifications."""

    level: str = "INFO"  # INFO, WARNING, ERROR, CRITICAL
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
