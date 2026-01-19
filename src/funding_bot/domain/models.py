"""
Canonical Domain Models.

All financial calculations use Decimal for precision.
These models are the single source of truth - SDK types are mapped to these.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

# =============================================================================
# ENUMS
# =============================================================================


class Exchange(str, Enum):
    """Supported exchanges."""

    LIGHTER = "LIGHTER"
    X10 = "X10"


class Side(str, Enum):
    """Order/position side."""

    BUY = "BUY"
    SELL = "SELL"

    def inverse(self) -> Side:
        """Return the opposite side."""
        return Side.SELL if self == Side.BUY else Side.BUY

    @classmethod
    def from_string(cls, value: str) -> Side:
        """Parse side from various string formats."""
        normalized = value.upper().strip()
        if normalized in ("BUY", "LONG", "B"):
            return cls.BUY
        if normalized in ("SELL", "SHORT", "S"):
            return cls.SELL
        raise ValueError(f"Unknown side: {value}")


class OrderType(str, Enum):
    """Order types."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(str, Enum):
    """Time in force options."""

    GTC = "GTC"  # Good till cancelled
    IOC = "IOC"  # Immediate or cancel
    POST_ONLY = "POST_ONLY"  # Maker only
    FOK = "FOK"  # Fill or kill


class OrderStatus(str, Enum):
    """Order status."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    def is_terminal(self) -> bool:
        """Check if this is a final state."""
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    def is_active(self) -> bool:
        """Check if order is still active."""
        return self in (
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )


class TradeStatus(str, Enum):
    """Business trade lifecycle status."""

    PENDING = "PENDING"  # Opportunity detected, not yet executing
    OPENING = "OPENING"  # Legs being executed
    OPEN = "OPEN"  # Both legs filled, position active
    CLOSING = "CLOSING"  # Exit in progress
    CLOSED = "CLOSED"  # Both legs closed
    REJECTED = "REJECTED"  # Preflight rejected (no execution)
    FAILED = "FAILED"  # Execution failed
    ROLLBACK = "ROLLBACK"  # Partial fill being rolled back


class SurgeTradeStatus(str, Enum):
    """Lifecycle status for Surge Pro single-leg trades."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ExecutionState(str, Enum):
    """Granular execution state machine."""

    PENDING = "PENDING"
    LEG1_SUBMITTED = "LEG1_SUBMITTED"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SUBMITTED = "LEG2_SUBMITTED"
    COMPLETE = "COMPLETE"
    PARTIAL_FILL = "PARTIAL_FILL"
    ROLLBACK_QUEUED = "ROLLBACK_QUEUED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_DONE = "ROLLBACK_DONE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class ExecutionAttemptStatus(str, Enum):
    """Execution attempt lifecycle for KPI/decision tracking."""

    STARTED = "STARTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    OPENED = "OPENED"


# =============================================================================
# VALUE OBJECTS & MODELS
# =============================================================================


@dataclass(frozen=True, slots=True)
class FundingRate:
    """Funding rate snapshot.

    Rates are stored as a normalized HOURLY rate (decimal).

    IMPORTANT: Both exchanges return HOURLY rates directly from their APIs.
    - Lighter: API returns hourly rate (formula: premium/8 + interest per hour)
    - X10 (Extended): API returns hourly rate (formula: avg_premium/8 + clamp per hour)
    - The /8 in the formulas converts 8h premium intervals to hourly rates
    - ExchangeSettings.funding_rate_interval_hours controls normalization (default: 1 = hourly, no division)
    - Both exchanges apply funding hourly (every hour on the hour)

    APY calculation: hourly_rate * 24 * 365 * 100%
    """

    symbol: str
    exchange: Exchange
    rate: Decimal  # Normalized HOURLY rate
    next_funding_time: datetime | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def rate_hourly(self) -> Decimal:
        """Return hourly rate (already normalized)."""
        return self.rate

    @property
    def rate_annual(self) -> Decimal:
        """Annualized rate (APY).

        Formula: hourly_rate * 24 hours * 365 days
        """
        return self.rate * Decimal("24") * Decimal("365")


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Market/instrument metadata."""

    symbol: str
    exchange: Exchange
    base_asset: str
    quote_asset: str = "USD"

    # Precision
    price_precision: int = 2
    qty_precision: int = 4
    tick_size: Decimal = Decimal("0.01")
    step_size: Decimal = Decimal("0.0001")

    # Limits
    min_qty: Decimal = Decimal("0")
    max_qty: Decimal = Decimal("1000000")
    min_notional: Decimal = Decimal("1")

    # Fees (fallback values)
    maker_fee: Decimal = Decimal("0")
    taker_fee: Decimal = Decimal("0")

    # Max Leverage Limits (Documented/IMR based)
    lighter_max_leverage: Decimal = Decimal("3")  # Conservative default
    lighter_min_initial_margin_fraction: int | None = None  # Raw IMR from API (e.g. 1e17 for 10%)
    x10_max_leverage: Decimal = Decimal("20")  # Optimistic default for X10 (updated from audit)


@dataclass(frozen=True, slots=True)
class Balance:
    """Account balance."""

    exchange: Exchange
    available: Decimal
    total: Decimal
    currency: str = "USD"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class Position:
    """
    Exchange position.

    This is always a live snapshot from the exchange.
    """

    symbol: str
    exchange: Exchange
    side: Side
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def notional_usd(self) -> Decimal:
        """Position notional value in USD."""
        return abs(self.qty * self.mark_price)

    @property
    def is_long(self) -> bool:
        return self.side == Side.BUY

    @property
    def is_short(self) -> bool:
        return self.side == Side.SELL


@dataclass(slots=True)
class OrderRequest:
    """Request to place an order."""

    symbol: str
    exchange: Exchange
    side: Side
    qty: Decimal
    order_type: OrderType
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.client_order_id is None:
            self.client_order_id = str(uuid.uuid4())[:8]


@dataclass(slots=True)
class Order:
    """
    Order with fill information.

    Created after an order is submitted.
    """

    order_id: str
    symbol: str
    exchange: Exchange
    side: Side
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False

    # Fill info
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_currency: str = "USD"

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

    # Client reference
    client_order_id: str | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_active(self) -> bool:
        return self.status.is_active()

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def fill_pct(self) -> Decimal:
        if self.qty == 0:
            return Decimal("0")
        return (self.filled_qty / self.qty) * Decimal("100")


@dataclass(slots=True)
class TradeLeg:
    """One leg of a delta-neutral trade."""

    exchange: Exchange
    side: Side
    order_id: str | None = None
    qty: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    exit_price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")

    @property
    def pnl(self) -> Decimal:
        """
        Calculate realized PnL for this leg (including fees).

        PnL Calculation:
        - BUY leg: (exit_price - entry_price) * filled_qty - fees
        - SELL leg: (entry_price - exit_price) * filled_qty - fees

        IMPORTANT: Fees are included in this calculation!
        - This property returns PnL AFTER fees (net PnL)
        - For gross PnL (before fees), add self.fees back to the result

        Note: This is leg-level PnL only. Trade-level PnL requires
        summing both legs and adding funding_collected.

        Returns:
            Decimal: Net realized PnL for this leg (price change - fees)
        """
        if self.filled_qty == 0 or self.exit_price == 0:
            return Decimal("0")

        if self.side == Side.BUY:
            return (self.exit_price - self.entry_price) * self.filled_qty - self.fees
        else:
            return (self.entry_price - self.exit_price) * self.filled_qty - self.fees


@dataclass(slots=True)
class Trade:
    """
    A delta-neutral funding arbitrage trade.

    Consists of two opposing positions (one LONG, one SHORT) on different exchanges.
    """

    trade_id: str
    symbol: str

    # Legs
    leg1: TradeLeg  # Typically Lighter (maker)
    leg2: TradeLeg  # Typically X10 (hedge)

    # Trade parameters
    target_qty: Decimal
    target_notional_usd: Decimal

    # State
    status: TradeStatus = TradeStatus.PENDING
    execution_state: ExecutionState = ExecutionState.PENDING

    # Funding
    funding_collected: Decimal = Decimal("0")
    last_funding_update: datetime | None = None

    # PnL
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")  # Track highest PnL for trailing stop

    # Metadata
    entry_apy: Decimal = Decimal("0")
    entry_spread: Decimal = Decimal("0")
    current_apy: Decimal = Decimal("0")  # latest observed APY (for UI/monitoring)
    current_lighter_rate: Decimal = Decimal("0")
    current_x10_rate: Decimal = Decimal("0")
    last_eval_at: datetime | None = None
    close_reason: str | None = None
    error: str | None = None  # Error message when trade fails

    # Close execution tracking (for P1.1 analysis)
    close_attempts: int = 0  # Number of maker attempts before close success/taker
    close_duration_seconds: Decimal = Decimal("0")  # Time from close start to complete

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    # Event log
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        symbol: str,
        leg1_exchange: Exchange,
        leg1_side: Side,
        leg2_exchange: Exchange,
        target_qty: Decimal,
        target_notional_usd: Decimal,
        entry_apy: Decimal = Decimal("0"),
        entry_spread: Decimal = Decimal("0"),
    ) -> Trade:
        """Factory method to create a new trade."""
        return cls(
            trade_id=str(uuid.uuid4()),
            symbol=symbol,
            leg1=TradeLeg(exchange=leg1_exchange, side=leg1_side),
            leg2=TradeLeg(exchange=leg2_exchange, side=leg1_side.inverse()),
            target_qty=target_qty,
            target_notional_usd=target_notional_usd,
            entry_apy=entry_apy,
            entry_spread=entry_spread,
        )

    @property
    def is_open(self) -> bool:
        return self.status == TradeStatus.OPEN

    @property
    def is_closed(self) -> bool:
        return self.status == TradeStatus.CLOSED

    @property
    def total_fees(self) -> Decimal:
        return self.leg1.fees + self.leg2.fees

    @property
    def total_pnl(self) -> Decimal:
        """
        Total PnL including funding and fees.
        Note: realized_pnl already subtracts fees via TradeLeg.pnl,
        so we do NOT subtract total_fees again here.
        """
        return self.realized_pnl + self.funding_collected

    @property
    def hold_duration_seconds(self) -> float:
        """Time since trade opened."""
        if not self.opened_at:
            return 0.0
        end = self.closed_at or datetime.now(UTC)
        return (end - self.opened_at).total_seconds()

    def mark_opened(self) -> None:
        """Mark trade as fully opened."""
        self.status = TradeStatus.OPEN
        self.execution_state = ExecutionState.COMPLETE
        self.opened_at = datetime.now(UTC)
        self.last_funding_update = self.opened_at
        self._log_event("OPENED")

    def mark_closed(self, reason: str, pnl: Decimal = Decimal("0")) -> None:
        """Mark trade as closed."""
        self.status = TradeStatus.CLOSED
        self.closed_at = datetime.now(UTC)
        self.close_reason = reason
        self.realized_pnl = pnl
        self._log_event("CLOSED", {"reason": reason, "pnl": str(pnl)})

    def update_funding(self, amount: Decimal) -> None:
        """Update collected funding."""
        self.funding_collected += amount
        self.last_funding_update = datetime.now(UTC)
        self._log_event("FUNDING", {"amount": str(amount), "total": str(self.funding_collected)})

    def _log_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Append event to trade log."""
        self.events.append(
            {
                "type": event_type,
                "timestamp": datetime.now(UTC).isoformat(),
                "data": data or {},
            }
        )


@dataclass(frozen=True, slots=True)
class Opportunity:
    """
    Detected funding arbitrage opportunity.

    Immutable - represents a point-in-time snapshot.
    """

    symbol: str
    timestamp: datetime

    # Funding
    lighter_rate: Decimal
    x10_rate: Decimal
    net_funding_hourly: Decimal
    apy: Decimal

    # Market
    spread_pct: Decimal
    mid_price: Decimal
    lighter_best_bid: Decimal
    lighter_best_ask: Decimal
    x10_best_bid: Decimal
    x10_best_ask: Decimal

    # Sizing
    suggested_qty: Decimal
    suggested_notional: Decimal

    # Analysis
    expected_value_usd: Decimal
    breakeven_hours: Decimal
    liquidity_score: Decimal = Decimal("1")
    confidence: Decimal = Decimal("1")

    # Direction
    long_exchange: Exchange = Exchange.LIGHTER
    short_exchange: Exchange = Exchange.X10

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity has positive expected value."""
        return self.expected_value_usd > Decimal("0")


@dataclass(slots=True)
class ExecutionAttempt:
    """
    Execution attempt / decision log for KPI tracking.

    This is intentionally separate from `Trade` so that preflight rejects and
    aborted attempts can be persisted without polluting the trades table/KPIs.
    """

    attempt_id: str
    symbol: str
    mode: str  # "PAPER" or "LIVE"
    status: ExecutionAttemptStatus = ExecutionAttemptStatus.STARTED
    stage: str = "START"
    trade_id: str | None = None
    reason: str | None = None

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        mode: str,
        trade_id: str | None = None,
        stage: str = "START",
        data: dict[str, Any] | None = None,
    ) -> ExecutionAttempt:
        return cls(
            attempt_id=str(uuid.uuid4()),
            symbol=symbol,
            mode=mode,
            trade_id=trade_id,
            stage=stage,
            data=data or {},
        )


@dataclass(slots=True)
class SurgeTrade:
    """Single-leg trade used by the Surge Pro strategy."""

    trade_id: str
    symbol: str
    exchange: Exchange
    side: Side
    qty: Decimal
    entry_price: Decimal = Decimal("0")
    exit_price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    status: SurgeTradeStatus = SurgeTradeStatus.PENDING
    entry_order_id: str | None = None
    entry_client_order_id: str | None = None
    exit_client_order_id: str | None = None
    entry_reason: str | None = None
    exit_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def pnl(self) -> Decimal:
        """Realized PnL including fees (0 if not closed)."""
        if self.qty == 0 or self.exit_price == 0:
            return Decimal("0")
        if self.side == Side.BUY:
            return (self.exit_price - self.entry_price) * self.qty - self.fees
        return (self.entry_price - self.exit_price) * self.qty - self.fees
