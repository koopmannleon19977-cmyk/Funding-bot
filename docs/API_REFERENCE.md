# API Reference

This document provides detailed API documentation for the Funding Bot's public interfaces, domain models, and services.

---

## Table of Contents

1. [Domain Models](#domain-models)
2. [Ports (Interfaces)](#ports-interfaces)
3. [Services](#services)
4. [Configuration](#configuration)
5. [Events](#events)

---

## Domain Models

Located in `src/funding_bot/domain/models.py`

### Enums

#### Exchange

```python
class Exchange(str, Enum):
    """Supported exchanges."""
    LIGHTER = "LIGHTER"
    X10 = "X10"
```

#### Side

```python
class Side(str, Enum):
    """Order/position side."""
    BUY = "BUY"
    SELL = "SELL"

    def inverse(self) -> Side:
        """Return the opposite side."""

    @classmethod
    def from_string(cls, value: str) -> Side:
        """Parse side from various string formats (BUY, LONG, B, SELL, SHORT, S)."""
```

#### OrderType

```python
class OrderType(str, Enum):
    """Order types."""
    LIMIT = "LIMIT"
    MARKET = "MARKET"
```

#### TimeInForce

```python
class TimeInForce(str, Enum):
    """Time in force options."""
    GTC = "GTC"       # Good till cancelled
    IOC = "IOC"       # Immediate or cancel
    POST_ONLY = "POST_ONLY"  # Maker only
    FOK = "FOK"       # Fill or kill
```

#### OrderStatus

```python
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
        """Check if this is a final state (FILLED, CANCELLED, REJECTED, EXPIRED)."""

    def is_active(self) -> bool:
        """Check if order is still active (PENDING, OPEN, PARTIALLY_FILLED)."""
```

#### TradeStatus

```python
class TradeStatus(str, Enum):
    """Business trade lifecycle status."""
    PENDING = "PENDING"    # Opportunity detected, not yet executing
    OPENING = "OPENING"    # Legs being executed
    OPEN = "OPEN"          # Both legs filled, position active
    CLOSING = "CLOSING"    # Exit in progress
    CLOSED = "CLOSED"      # Both legs closed
    REJECTED = "REJECTED"  # Preflight rejected (no execution)
    FAILED = "FAILED"      # Execution failed
    ROLLBACK = "ROLLBACK"  # Partial fill being rolled back
```

#### ExecutionState

```python
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
```

---

### Value Objects

#### FundingRate

Represents a funding rate snapshot with normalized hourly rate.

```python
@dataclass(frozen=True, slots=True)
class FundingRate:
    """
    Funding rate snapshot.

    Rates are stored as normalized HOURLY rates (decimal).
    Both Lighter and X10 return hourly rates directly from their APIs.

    APY calculation: hourly_rate * 24 * 365
    """

    symbol: str
    exchange: Exchange
    rate: Decimal                    # Normalized HOURLY rate
    next_funding_time: datetime | None = None
    timestamp: datetime

    @property
    def rate_hourly(self) -> Decimal:
        """Return hourly rate (already normalized)."""

    @property
    def rate_annual(self) -> Decimal:
        """Annualized rate (APY): hourly_rate * 24 * 365"""
```

**Example:**
```python
rate = FundingRate(
    symbol="ETH",
    exchange=Exchange.LIGHTER,
    rate=Decimal("0.0001"),  # 0.01% hourly
    next_funding_time=datetime.now(UTC) + timedelta(hours=1),
)

print(rate.rate_hourly)  # Decimal("0.0001")
print(rate.rate_annual)  # Decimal("0.876") = 87.6% APY
```

---

#### MarketInfo

Market/instrument metadata including precision and limits.

```python
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

    # Max Leverage Limits
    lighter_max_leverage: Decimal = Decimal("3")
    x10_max_leverage: Decimal = Decimal("20")
```

---

#### Balance

Account balance snapshot.

```python
@dataclass(frozen=True, slots=True)
class Balance:
    """Account balance."""

    exchange: Exchange
    available: Decimal    # Available for trading
    total: Decimal        # Total balance including margin
    currency: str = "USD"
    timestamp: datetime
```

---

#### Position

Live exchange position snapshot.

```python
@dataclass(slots=True)
class Position:
    """Exchange position (live snapshot)."""

    symbol: str
    exchange: Exchange
    side: Side
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    timestamp: datetime

    @property
    def notional_usd(self) -> Decimal:
        """Position notional value in USD: abs(qty * mark_price)"""

    @property
    def is_long(self) -> bool:
        """True if side == BUY"""

    @property
    def is_short(self) -> bool:
        """True if side == SELL"""
```

---

#### OrderRequest

Request to place an order.

```python
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
    client_order_id: str | None = None  # Auto-generated if not provided
```

**Example:**
```python
request = OrderRequest(
    symbol="ETH",
    exchange=Exchange.LIGHTER,
    side=Side.BUY,
    qty=Decimal("0.1"),
    order_type=OrderType.LIMIT,
    price=Decimal("3000.00"),
    time_in_force=TimeInForce.POST_ONLY,
)
```

---

#### Order

Order with fill information.

```python
@dataclass(slots=True)
class Order:
    """Order with fill information."""

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
    created_at: datetime
    updated_at: datetime | None = None
    client_order_id: str | None = None

    @property
    def is_filled(self) -> bool:
        """True if status == FILLED"""

    @property
    def is_active(self) -> bool:
        """True if status is PENDING, OPEN, or PARTIALLY_FILLED"""

    @property
    def remaining_qty(self) -> Decimal:
        """qty - filled_qty"""

    @property
    def fill_pct(self) -> Decimal:
        """Fill percentage: (filled_qty / qty) * 100"""
```

---

#### TradeLeg

One leg of a delta-neutral trade.

```python
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

        BUY leg: (exit_price - entry_price) * filled_qty - fees
        SELL leg: (entry_price - exit_price) * filled_qty - fees

        Returns net PnL (after fees).
        """
```

---

#### Trade

A delta-neutral funding arbitrage trade.

```python
@dataclass(slots=True)
class Trade:
    """Delta-neutral funding arbitrage trade."""

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
    high_water_mark: Decimal = Decimal("0")

    # Metadata
    entry_apy: Decimal = Decimal("0")
    entry_spread: Decimal = Decimal("0")
    current_apy: Decimal = Decimal("0")
    close_reason: str | None = None
    error: str | None = None

    # Timestamps
    created_at: datetime
    opened_at: datetime | None = None
    closed_at: datetime | None = None

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

    @property
    def is_open(self) -> bool:
        """True if status == OPEN"""

    @property
    def is_closed(self) -> bool:
        """True if status == CLOSED"""

    @property
    def total_fees(self) -> Decimal:
        """Sum of fees from both legs."""

    @property
    def total_pnl(self) -> Decimal:
        """Total PnL: realized_pnl + funding_collected"""

    @property
    def hold_duration_seconds(self) -> float:
        """Time since trade opened."""

    def mark_opened(self) -> None:
        """Mark trade as fully opened."""

    def mark_closed(self, reason: str, pnl: Decimal = Decimal("0")) -> None:
        """Mark trade as closed."""

    def update_funding(self, amount: Decimal) -> None:
        """Update collected funding."""
```

---

#### Opportunity

Detected funding arbitrage opportunity (immutable).

```python
@dataclass(frozen=True, slots=True)
class Opportunity:
    """Detected funding arbitrage opportunity."""

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
```

---

## Ports (Interfaces)

Located in `src/funding_bot/ports/`

### ExchangePort

Abstract interface for exchange adapters.

```python
class ExchangePort(ABC):
    """
    Abstract interface for exchange operations.

    All methods are async and use domain types.
    Implementations must map SDK-specific types to these domain types.
    """

    @property
    @abstractmethod
    def exchange(self) -> Exchange:
        """Return the exchange identifier."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is healthy."""

    # =========================================================================
    # Lifecycle
    # =========================================================================

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the adapter (load markets, establish connections)."""

    @abstractmethod
    async def close(self) -> None:
        """Close all connections and cleanup resources."""

    # =========================================================================
    # Market Data
    # =========================================================================

    @abstractmethod
    async def load_markets(self) -> dict[str, MarketInfo]:
        """Load all available markets. Returns dict mapping symbol -> MarketInfo."""

    @abstractmethod
    async def get_market_info(self, symbol: str) -> MarketInfo | None:
        """Get market metadata for a symbol."""

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """Get current mark price for a symbol."""

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate for a symbol."""

    @abstractmethod
    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
        """
        Get best bid/ask (Level 1 orderbook).

        Returns:
            {
                "best_bid": Decimal,
                "best_ask": Decimal,
                "bid_qty": Decimal,
                "ask_qty": Decimal,
                "timestamp": datetime,
            }
        """

    # =========================================================================
    # Account
    # =========================================================================

    @abstractmethod
    async def get_available_balance(self) -> Balance:
        """Get available margin/balance."""

    @abstractmethod
    async def get_fee_schedule(self, symbol: str | None = None) -> dict[str, Decimal]:
        """
        Get fee schedule for the account.

        Returns: {"maker_fee": Decimal, "taker_fee": Decimal}
        """

    # =========================================================================
    # Positions
    # =========================================================================

    @abstractmethod
    async def list_positions(self) -> list[Position]:
        """Get all open positions (qty != 0)."""

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a specific symbol. Returns None if no position."""

    @abstractmethod
    async def get_realized_funding(
        self, symbol: str, start_time: datetime | None = None
    ) -> Decimal:
        """
        Get cumulative realized funding payment for a symbol since start_time.

        Returns: Total funding (positive = received, negative = paid).
        """

    # =========================================================================
    # Orders
    # =========================================================================

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """
        Place an order.

        Args:
            request: OrderRequest with all order parameters.

        Returns:
            Order object with order_id and initial status.

        Raises:
            OrderRejectedError: If order is rejected.
            InsufficientBalanceError: If balance insufficient.
        """

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get order status by ID. Returns None if not found."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns:
            True if cancel succeeded or order already cancelled.
            False if order not found or already filled.
        """

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel all open orders, optionally filtered by symbol. Returns count."""

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        price: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> bool:
        """Modify an existing order. Default returns False (not supported)."""

    # =========================================================================
    # WebSocket (optional)
    # =========================================================================

    async def subscribe_positions(self, callback: PositionCallback) -> None:
        """Subscribe to position updates. Default: no-op (polling fallback)."""

    async def subscribe_orders(self, callback: OrderCallback) -> None:
        """Subscribe to order updates. Default: no-op (polling fallback)."""

    async def subscribe_funding(self, callback: FundingCallback) -> None:
        """Subscribe to funding rate updates. Default: no-op (polling fallback)."""

    async def subscribe_orderbook_l1(
        self,
        symbols: list[str] | None = None,
        *,
        depth: int | None = None,
    ) -> None:
        """Subscribe to orderbook updates. Default: no-op."""
```

### Callback Types

```python
PositionCallback = Callable[[Position], Awaitable[None]]
OrderCallback = Callable[[Order], Awaitable[None]]
FundingCallback = Callable[[FundingRate], Awaitable[None]]
```

---

## Services

### ExecutionEngine

Located in `src/funding_bot/services/execution.py`

```python
class ExecutionEngine:
    """
    Core execution engine for opening trades.

    Handles the complete execution flow:
    1. Preflight checks (depth, spread, opportunity validation)
    2. Quantity calculation (sizing based on margin and limits)
    3. Leg 1 execution (maker order on Lighter)
    4. Leg 2 execution (hedge IOC on X10)
    5. Trade finalization or rollback
    """

    def __init__(
        self,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: StorePort,
        settings: Settings,
    ):
        """Initialize the execution engine."""

    async def execute(self, opportunity: Opportunity) -> Trade | None:
        """
        Execute a trade for the given opportunity.

        Args:
            opportunity: The detected arbitrage opportunity.

        Returns:
            Trade object if successful, None if failed or rejected.
        """

    def get_active_executions(self) -> dict[str, ExecutionContext]:
        """Get currently executing trades (for monitoring)."""
```

### PositionManager

Located in `src/funding_bot/services/positions/manager.py`

```python
class PositionManager:
    """
    Manages open trades and evaluates exit conditions.

    Runs periodically to:
    - Evaluate exit strategies for all open trades
    - Execute closes when exit conditions are met
    - Update funding accrual
    - Handle delta rebalancing
    """

    def __init__(
        self,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: StorePort,
        settings: Settings,
    ):
        """Initialize the position manager."""

    async def check_trades(self) -> None:
        """
        Evaluate all open trades for exit conditions.

        Called periodically by the supervisor loop.
        """

    async def close_trade(self, trade: Trade, reason: str) -> CloseResult:
        """
        Close a trade with the given reason.

        Args:
            trade: The trade to close.
            reason: Exit reason for logging.

        Returns:
            CloseResult with execution details.
        """

    async def force_close_all(self) -> list[CloseResult]:
        """Close all open trades (shutdown/emergency)."""
```

### OpportunityEngine

Located in `src/funding_bot/services/opportunities/engine.py`

```python
class OpportunityEngine:
    """
    Tracks symbol health and cooldowns for opportunity scanning.

    Prevents hammering symbols that consistently fail execution.
    """

    def __init__(self, settings: Settings):
        """Initialize with settings."""

    def should_skip_symbol(self, symbol: str) -> bool:
        """
        Check if a symbol should be skipped due to recent failures.

        Returns True if symbol is in cooldown period.
        """

    def record_symbol_failure(self, symbol: str) -> None:
        """Record a failure for a symbol (extends cooldown)."""

    def record_symbol_success(self, symbol: str) -> None:
        """Record a success for a symbol (resets failure count)."""
```

---

## Configuration

### Settings

Located in `src/funding_bot/config/settings.py`

```python
class Settings:
    """
    Root settings object loaded from config.yaml and environment.

    Nested settings classes:
    - exchanges: ExchangeSettings
    - trading: TradingSettings
    - execution: ExecutionSettings
    - reconciliation: ReconciliationSettings
    - risk: RiskSettings
    - websocket: WebSocketSettings
    - database: DatabaseSettings
    - logging: LoggingSettings
    - telegram: TelegramSettings
    - historical: HistoricalSettings
    - shutdown: ShutdownSettings
    """

    @classmethod
    def from_yaml(cls, path: str) -> Settings:
        """Load settings from YAML file."""

    def validate_for_live_trading(self) -> None:
        """Validate settings are safe for live trading."""


def get_settings() -> Settings:
    """Get the global settings instance (singleton)."""
```

### Key Settings Classes

#### TradingSettings

```python
class TradingSettings:
    """Trading parameters."""

    desired_notional_usd: Decimal
    max_open_trades: int
    leverage_multiplier: Decimal

    # Entry filters
    min_apy_filter: Decimal
    min_expected_profit_entry_usd: Decimal
    min_ev_spread_ratio: Decimal
    max_breakeven_hours: Decimal
    max_spread_filter_percent: Decimal
    max_spread_cap_percent: Decimal

    # Depth gate
    depth_gate_enabled: bool
    depth_gate_mode: str  # "L1" or "IMPACT"
    min_l1_notional_usd: Decimal
    min_l1_notional_multiple: Decimal

    # Hold time
    min_hold_seconds: int
    max_hold_hours: Decimal

    # Exit settings
    min_profit_exit_usd: Decimal
    early_take_profit_enabled: bool
    early_take_profit_net_usd: Decimal
    funding_flip_hours_threshold: Decimal

    # And many more...
```

#### ExecutionSettings

```python
class ExecutionSettings:
    """Order execution parameters."""

    lead_exchange: str  # "LIGHTER" or "X10"
    maker_order_timeout_seconds: Decimal
    maker_order_max_retries: int
    maker_max_aggressiveness: Decimal
    maker_force_post_only: bool
    parallel_execution_timeout: Decimal
    rollback_delay_seconds: Decimal
    taker_order_slippage: Decimal
    taker_close_slippage: Decimal

    # Leg1 escalation
    leg1_escalate_to_taker_enabled: bool
    leg1_escalate_to_taker_after_seconds: Decimal
    leg1_escalate_to_taker_slippage: Decimal

    # Hedge IOC
    hedge_ioc_max_attempts: int
    hedge_ioc_fill_timeout_seconds: Decimal
    hedge_ioc_slippage_step: Decimal
    hedge_ioc_max_slippage: Decimal

    # And many more...
```

#### RiskSettings

```python
class RiskSettings:
    """Risk management parameters."""

    max_consecutive_failures: int
    max_drawdown_pct: Decimal
    max_exposure_pct: Decimal
    max_trade_size_pct: Decimal
    min_free_margin_pct: Decimal
    broken_hedge_cooldown_seconds: Decimal
```

---

## Events

Located in `src/core/events.py`

### CriticalError

```python
@dataclass
class CriticalError:
    """Critical error event for notification."""

    message: str
    details: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
```

### NotificationEvent

```python
@dataclass
class NotificationEvent:
    """Generic notification event."""

    level: str  # "INFO", "WARNING", "ERROR", "CRITICAL"
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
```

### EventBus

Located in `src/infrastructure/event_bus.py`

```python
class EventBus:
    """Simple async pub/sub event bus."""

    async def start(self) -> None:
        """Start the event bus."""

    async def stop(self) -> None:
        """Stop the event bus and drain pending events."""

    def publish(self, event: Any) -> None:
        """Publish an event (non-blocking)."""

    def subscribe(
        self,
        event_type: type,
        handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """Subscribe a handler to an event type."""
```

**Example:**
```python
event_bus = EventBus()
await event_bus.start()

async def handle_critical(event: CriticalError):
    print(f"Critical: {event.message}")

event_bus.subscribe(CriticalError, handle_critical)

# Later...
event_bus.publish(CriticalError(message="Connection lost"))
```

---

## Error Handling

### Domain Errors

Located in `src/funding_bot/domain/errors.py`

```python
class DomainError(Exception):
    """Base domain error."""

class OrderRejectedError(DomainError):
    """Order was rejected by the exchange."""

class InsufficientBalanceError(DomainError):
    """Insufficient balance to place order."""

class PositionNotFoundError(DomainError):
    """Position not found."""

class ExecutionError(DomainError):
    """Trade execution failed."""
```

---

## Adapters

### LighterAdapter

Implements `ExchangePort` for Lighter Exchange.

```python
class LighterAdapter(ExchangePort):
    """
    Lighter Exchange adapter.

    Features:
    - REST API for market data and orders
    - WebSocket for orderbook and order updates
    - Post-only maker orders
    - Account index support
    """

    def __init__(self, settings: ExchangeSettings):
        """Initialize with exchange settings."""

    # Implements all ExchangePort methods
```

### X10Adapter

Implements `ExchangePort` for X10 Exchange.

```python
class X10Adapter(ExchangePort):
    """
    X10 (Extended) Exchange adapter.

    Features:
    - REST API for market data and orders
    - WebSocket for orderbook streams
    - Stark curve signing for orders
    """

    def __init__(self, settings: ExchangeSettings):
        """Initialize with exchange settings."""

    # Implements all ExchangePort methods
```

---

## Utilities

### Decimal Utilities

Located in `src/funding_bot/utils/decimals.py`

```python
def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    """Round price to tick size."""

def quantize_qty(value: Decimal, step_size: Decimal) -> Decimal:
    """Round quantity to step size."""

def safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safely convert any value to Decimal."""
```

### Async Utilities

Located in `src/utils/async_utils.py`

```python
async def with_timeout(coro: Coroutine, timeout: float) -> Any:
    """Execute coroutine with timeout."""

async def retry_async(
    func: Callable[..., Awaitable[T]],
    max_attempts: int = 3,
    delay: float = 1.0,
    exceptions: tuple = (Exception,),
) -> T:
    """Retry async function with exponential backoff."""
```
