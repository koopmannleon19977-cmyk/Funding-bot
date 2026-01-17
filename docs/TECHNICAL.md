# Technical Documentation

## System Architecture

### Overview

Funding Bot follows a **Hexagonal Architecture** (Ports and Adapters) pattern combined with **Domain-Driven Design** principles. This architecture ensures:

- Clear separation between business logic and infrastructure
- Easy testability through dependency injection
- Flexibility to swap exchange implementations

```
┌─────────────────────────────────────────────────────────────────┐
│                        Application Layer                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ FundingBot  │  │ Supervisor  │  │    OpportunityEngine    │  │
│  │   (run.py)  │  │   (loops)   │  │  (scanner, evaluator)   │  │
│  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
│         │                │                      │                │
│  ┌──────▼──────────────────────────────────────▼──────────────┐ │
│  │                    Domain Services                          │ │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │ │
│  │  │ Execution   │  │  Position   │  │    Market Data      │ │ │
│  │  │   Engine    │  │   Manager   │  │      Service        │ │ │
│  │  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │ │
│  └─────────┼────────────────┼───────────────────┼─────────────┘ │
└────────────┼────────────────┼───────────────────┼───────────────┘
             │                │                   │
┌────────────▼────────────────▼───────────────────▼───────────────┐
│                         Ports (Interfaces)                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ ExchangePort│  │  StorePort  │  │   NotificationPort      │  │
│  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
└─────────┼───────────────┼──────────────────────┼────────────────┘
          │               │                      │
┌─────────▼───────────────▼──────────────────────▼────────────────┐
│                         Adapters                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Lighter   │  │    X10      │  │      SQLite Store       │  │
│  │   Adapter   │  │   Adapter   │  │  (trades, funding, etc) │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  WebSocket  │  │  Telegram   │  │      Event Bus          │  │
│  │   Manager   │  │    Bot      │  │   (pub/sub messaging)   │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Structure

### Core Modules (`src/core/`)

| Module | Purpose |
|--------|---------|
| `startup.py` | Bot initialization, database setup, adapter creation |
| `trading.py` | Core trading logic, opportunity evaluation |
| `state.py` | In-memory state management, position cache |
| `events.py` | Event definitions (CriticalError, NotificationEvent) |
| `circuit_breaker.py` | Failure tracking and automatic circuit breaking |
| `adaptive_threshold.py` | Dynamic threshold adjustment based on market conditions |

### Domain Layer (`src/funding_bot/domain/`)

| Module | Purpose |
|--------|---------|
| `models.py` | Core entities: `Trade`, `Position`, `Order`, `FundingRate`, `Opportunity` |
| `rules.py` | Business rules for entry/exit decisions |
| `events.py` | Domain events for position changes |
| `errors.py` | Domain-specific exceptions |

#### Key Domain Models

```python
# Exchange identifiers
class Exchange(Enum):
    LIGHTER = "Lighter"
    X10 = "X10"

# Order side
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

# Trade represents an active arbitrage position
@dataclass
class Trade:
    id: str
    symbol: str
    status: TradeStatus
    lighter_leg: TradeLeg
    x10_leg: TradeLeg
    entry_apy: Decimal
    entry_spread: Decimal
    created_at: datetime
    # ...

# Position on a single exchange
@dataclass
class Position:
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal

    @property
    def notional_usd(self) -> Decimal:
        return self.quantity * self.mark_price
```

### Ports (`src/funding_bot/ports/`)

Ports define interfaces that adapters must implement:

```python
class ExchangePort(ABC):
    """Abstract interface for exchange operations."""

    @property
    @abstractmethod
    def exchange(self) -> Exchange: ...

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def list_positions(self) -> List[Position]: ...

    # ... more methods
```

### Adapters (`src/adapters/`, `src/funding_bot/adapters/`)

| Adapter | Implementation |
|---------|----------------|
| `LighterAdapter` | Lighter Exchange via REST + WebSocket |
| `X10Adapter` | X10 Exchange via REST + WebSocket |
| `SQLiteStore` | Persistent storage for trades, funding, events |
| `TelegramBot` | Notification delivery via Telegram |
| `EventBus` | In-process pub/sub messaging |

---

## Execution Engine

### Trade Execution Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                     ExecutionEngine.execute()                     │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                      1. Preflight Checks                          │
│  • Validate opportunity still profitable                          │
│  • Check depth gate (L1 liquidity)                               │
│  • Verify hedge depth (preflight multiplier)                     │
│  • Confirm spread within limits                                  │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     2. Calculate Quantity                         │
│  • Determine notional based on available margin                  │
│  • Apply leverage multiplier                                     │
│  • Respect min_qty requirements                                  │
│  • Apply sizing caps (max_trade_size_pct)                        │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                   3. Execute Leg 1 (Lighter)                      │
│  • Place maker order at best price                               │
│  • Wait for fill (WS updates + polling fallback)                 │
│  • Escalate to taker IOC if timeout exceeded                     │
│  • Track partial fills                                           │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │    Leg1 Filled?     │
                    └──────────┬──────────┘
                       │              │
                      Yes            No
                       │              │
                       ▼              ▼
┌─────────────────────────┐  ┌─────────────────────────────────────┐
│  4. Execute Leg 2 (X10) │  │         Rollback Leg 1              │
│  • Submit IOC hedge     │  │  • Cancel open orders               │
│  • Retry with slippage  │  │  • Close any partial fills          │
│  • Track fill status    │  │  • Log failure reason               │
└───────────┬─────────────┘  └─────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    5. Finalize Trade                              │
│  • Update trade record in database                               │
│  • Calculate entry metrics (APY, spread, fees)                   │
│  • Start position monitoring                                     │
│  • Send notification                                             │
└──────────────────────────────────────────────────────────────────┘
```

### Leg 1 Execution Details

The execution engine uses a **maker-first** strategy on Lighter:

1. **Initial Order**: Post-only maker order at best bid/ask
2. **Repricing Loop**: Periodically reprice if order not filled
3. **WS Fill Monitoring**: Real-time fill updates via WebSocket
4. **Taker Escalation**: After `leg1_escalate_to_taker_after_seconds`, switch to IOC
5. **Partial Fill Handling**: Track accumulated fills, proceed when sufficient

```python
# Simplified execution flow
async def _execute_leg1(self, ctx: ExecutionContext) -> Leg1Result:
    # Prepare maker order
    price = self._calculate_maker_price(ctx)
    order = await self.lighter.place_order(
        OrderRequest(
            symbol=ctx.symbol,
            side=ctx.lighter_side,
            order_type=OrderType.LIMIT,
            quantity=ctx.quantity,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,
        )
    )

    # Wait for fill
    filled = await self._wait_for_fill(order, timeout=maker_timeout)

    if not filled and escalate_enabled:
        # Escalate to taker
        order = await self._place_lighter_taker_ioc(ctx)
        filled = await self._wait_for_fill(order, timeout=ioc_timeout)

    return Leg1Result(filled=filled, order=order)
```

### Leg 2 (Hedge) Execution

X10 hedge uses **aggressive IOC** with retry logic:

1. **Initial Attempt**: IOC at best price + base slippage
2. **Retry Loop**: Increment slippage by `hedge_ioc_slippage_step`
3. **Max Attempts**: Configurable via `hedge_ioc_max_attempts`
4. **Depth Salvage**: If hedge fails, attempt to close Leg1 partial fills

```python
# Hedge execution with retries
for attempt in range(max_attempts):
    slippage = base_slippage + (attempt * slippage_step)
    price = mark_price * (1 + slippage if is_buy else 1 - slippage)

    order = await self.x10.place_order(
        OrderRequest(
            symbol=symbol,
            side=hedge_side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
            time_in_force=TimeInForce.IOC,
        )
    )

    if order.is_filled:
        return HedgeResult(success=True, order=order)

    await asyncio.sleep(retry_delay)
```

---

## Position Management

### Exit Evaluation Pipeline

The `PositionManager` evaluates exits in a prioritized pipeline:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Exit Evaluation Pipeline                      │
│                    (Priority: High → Low)                        │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ LAYER 1: EMERGENCY EXITS (Override all gates)                    │
│  • Liquidation Distance < 15%                                    │
│  • Delta Bound Breach > 3%                                       │
│  • Funding Flip (negative funding)                               │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Not triggered
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 2: EARLY EXITS (Bypass min_hold gate)                      │
│  • Early Take-Profit >= $4.00 net                                │
│  • Early Edge Exit (funding collapse after 2h)                   │
│  • Z-Score Crash < -2.0 (for trades > 7 days)                    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Not triggered
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ MIN_HOLD GATE                                                    │
│  • Trade age < min_hold_seconds (48h)? → SKIP remaining layers   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Gate passed
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 3: STANDARD EXITS                                          │
│  • Profit Target >= min_profit_exit_usd                          │
│  • Max Hold Time exceeded                                        │
│  • Basis Convergence (80% spread collapse)                       │
│  • Net EV Exit (expected value negative)                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Not triggered
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 4: OPPORTUNITY COST                                        │
│  • Better opportunity available (APY diff > 80%)                 │
│  • Rotation cooldown respected                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Coordinated Close

When closing a position, the bot executes a **coordinated close** on both legs:

```python
async def close_trade(self, trade: Trade, reason: str) -> CloseResult:
    async with self._get_close_lock(trade.id):
        # 1. Fetch current positions
        lighter_pos = await self.lighter.get_position(trade.symbol)
        x10_pos = await self.x10.get_position(trade.symbol)

        # 2. Calculate close quantities
        lighter_qty = lighter_pos.quantity
        x10_qty = x10_pos.quantity

        # 3. Execute parallel close
        async with asyncio.TaskGroup() as tg:
            lighter_task = tg.create_task(
                self._close_leg(self.lighter, trade.symbol, lighter_qty)
            )
            x10_task = tg.create_task(
                self._close_leg(self.x10, trade.symbol, x10_qty)
            )

        # 4. Verify both legs closed
        lighter_result = lighter_task.result()
        x10_result = x10_task.result()

        # 5. Update trade record
        await self._update_trade(trade, "CLOSED", reason)

        return CloseResult(
            lighter=lighter_result,
            x10=x10_result,
            total_pnl=lighter_result.pnl + x10_result.pnl
        )
```

### Delta Rebalancing

When hedge drift exceeds threshold (1-3%), the bot rebalances:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Rebalance Flow                                │
└─────────────────────────────────────────────────────────────────┘

1. Detect drift:
   delta_pct = abs(lighter_notional - x10_notional) / max_notional

2. If delta_pct >= rebalance_min_delta_pct (1%):
   → Log warning, continue monitoring

3. If delta_pct >= rebalance_max_delta_pct (3%):
   → Calculate rebalance quantity
   → Determine which leg to adjust
   → Place maker order (with IOC fallback)
   → Verify delta restored to acceptable range
```

---

## Market Data Service

### Data Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                      Market Data Service                          │
└──────────────────────────────────────────────────────────────────┘

┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  WebSocket      │────▶│   Data Cache    │────▶│  Opportunity    │
│  Streams        │     │  (TTL-based)    │     │    Scanner      │
└─────────────────┘     └─────────────────┘     └─────────────────┘
       │                        │
       │                        ▼
       │               ┌─────────────────┐
       │               │  Funding Rates  │
       │               │  (per symbol)   │
       │               └─────────────────┘
       │                        │
       ▼                        ▼
┌─────────────────┐     ┌─────────────────┐
│  Orderbook L1   │     │  Mark Prices    │
│  (bid/ask)      │     │  (mid price)    │
└─────────────────┘     └─────────────────┘
```

### Caching Strategy

| Data Type | TTL | Refresh Strategy |
|-----------|-----|------------------|
| Funding Rates | 60s | Batch refresh every 15s |
| Orderbook L1 | 5s | WebSocket stream (hot symbols) |
| Depth Snapshot | 5s | On-demand REST fetch |
| Mark Prices | 5s | Derived from L1 mid |

### WebSocket Streams

```python
# Hot symbol streaming
market_data_streams_enabled: true
market_data_streams_symbols: ["ETH", "BTC", "SOL"]
market_data_streams_max_symbols: 6
```

For high-priority symbols, the bot maintains persistent WebSocket connections for:
- Orderbook L1 updates (best bid/ask)
- Trade stream (last price)
- Funding rate updates

---

## Risk Management

### Circuit Breaker

The circuit breaker prevents cascading failures:

```python
class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 300.0):
        self.failures = 0
        self.threshold = threshold
        self.cooldown = cooldown
        self.last_failure: Optional[float] = None

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.threshold:
            logger.warning("Circuit breaker OPEN")

    def is_open(self) -> bool:
        if self.failures < self.threshold:
            return False
        if time.time() - self.last_failure > self.cooldown:
            self.reset()
            return False
        return True
```

### Risk Limits

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_consecutive_failures` | 3 | Circuit breaker threshold |
| `max_drawdown_pct` | 20% | Maximum allowed drawdown |
| `max_exposure_pct` | 80% | Maximum capital exposure |
| `max_trade_size_pct` | 50% | Maximum single trade size |
| `min_free_margin_pct` | 5% | Minimum margin reserve |

### Liquidation Monitoring

```python
# Liquidation distance monitoring (when enabled)
liquidation_distance_monitoring_enabled: true
liquidation_distance_min_pct: 15.0  # 15% buffer
liquidation_distance_check_interval_seconds: 15
```

---

## Database Schema

### Core Tables

```sql
-- Active and historical trades
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    lighter_side TEXT,
    lighter_qty REAL,
    lighter_entry_price REAL,
    x10_side TEXT,
    x10_qty REAL,
    x10_entry_price REAL,
    entry_apy REAL,
    entry_spread REAL,
    total_funding_collected REAL DEFAULT 0,
    created_at TIMESTAMP,
    closed_at TIMESTAMP,
    close_reason TEXT
);

-- Execution attempts
CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    symbol TEXT,
    status TEXT,
    leg1_exchange TEXT,
    leg1_order_id TEXT,
    leg1_fill_qty REAL,
    leg2_exchange TEXT,
    leg2_order_id TEXT,
    leg2_fill_qty REAL,
    created_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

-- Funding rate history
CREATE TABLE funding_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    exchange TEXT,
    rate REAL,
    timestamp TIMESTAMP,
    UNIQUE(symbol, exchange, timestamp)
);

-- Event log
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    trade_id TEXT,
    data TEXT,  -- JSON
    timestamp TIMESTAMP
);
```

### Write Queue

Database writes are batched for performance:

```python
class WriteQueue:
    def __init__(self, batch_size: int = 50):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.batch_size = batch_size

    async def enqueue(self, operation: Callable):
        await self.queue.put(operation)

    async def flush(self):
        batch = []
        while not self.queue.empty() and len(batch) < self.batch_size:
            batch.append(await self.queue.get())

        async with self.db.transaction():
            for op in batch:
                await op()
```

---

## Performance Optimizations

### Parallel Execution

1. **Parallel Symbol Evaluation**: Opportunities evaluated concurrently
2. **Parallel Position Fetches**: Both exchanges queried simultaneously
3. **Batch Parallelization**: Historical data ingestion in parallel chunks

### Caching

1. **Depth Cache**: 5-second TTL for orderbook depth
2. **Position Cache**: 10-second TTL for position data
3. **Funding Rate Cache**: 60-second TTL with batch refresh

### Memory Optimization

1. **`itertools.islice`**: Avoid full list materialization
2. **Single-pass Filtering**: Combined filter operations
3. **Streaming Ingestion**: Process historical data in chunks

---

## Observability

### Logging

```python
# Structured JSON logging
logging:
  level: "INFO"
  json_enabled: true
  json_file: "logs/funding_bot_json.jsonl"
  json_max_bytes: 50000000  # 50MB rotation
```

### Metrics

Key metrics tracked:
- Execution latency (Leg1, Leg2, total)
- Fill rates (maker vs taker)
- Funding rate collection
- PnL tracking (per trade, cumulative)
- WebSocket health (latency, reconnects)

### Telegram Notifications

```python
# Event types sent to Telegram
- Trade opened: symbol, APY, notional
- Trade closed: symbol, PnL, reason
- Critical errors: error message, stack trace
- Circuit breaker events
```

---

## Testing Strategy

### Test Categories

| Category | Location | Focus |
|----------|----------|-------|
| Unit | `tests/unit/` | Isolated component testing |
| Integration | `tests/integration/` | Adapter and service integration |
| E2E | `tests/e2e/` | Full system workflows |

### Mocking Strategy

```python
# Example: Mocking exchange adapter
@pytest.fixture
def mock_lighter():
    adapter = AsyncMock(spec=LighterAdapter)
    adapter.exchange = Exchange.LIGHTER
    adapter.get_funding_rate.return_value = FundingRate(
        symbol="ETH",
        rate=Decimal("0.0001"),
        next_funding_time=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    return adapter
```

### Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest -m unit

# Integration tests
pytest -m integration

# With coverage
pytest --cov=src/funding_bot --cov-report=html
```
