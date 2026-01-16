# Technical Documentation - Funding Arbitrage Bot

> **Version**: 2.0.0
> **Generated**: 2026-01-16
> **Architecture**: Hexagonal (Ports & Adapters)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Module Reference](#module-reference)
4. [Domain Models](#domain-models)
5. [Ports (Interfaces)](#ports-interfaces)
6. [Services](#services)
7. [Adapters](#adapters)
8. [Configuration](#configuration)
9. [Observability](#observability)
10. [Performance Characteristics](#performance-characteristics)

---

## Overview

This is a **production-grade funding arbitrage bot** implementing delta-neutral trading strategies between:
- **Lighter** (DEX) - Premium account with 24,000 req/min
- **X10/Extended** (CEX)

### Core Strategy

The bot exploits funding rate differentials between perpetual futures exchanges:

```
Net Funding = |Lighter Rate - X10 Rate|
Position: Long on lower-rate exchange, Short on higher-rate exchange
Profit: Collect net funding while maintaining delta-neutral exposure
```

### Key Metrics

| Metric | Target |
|--------|--------|
| Market Data Refresh | ≤ 500ms (10 symbols) |
| Exit Evaluation | ≤ 150ms (10 positions) |
| DB Batch Write | ≤ 10ms (100 trades) |
| Connection Pool | 200 total, 100 per-host |

---

## Architecture

### Hexagonal Architecture (Ports & Adapters)

```
┌─────────────────────────────────────────────────────────────┐
│                        Application                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    Domain Core                       │   │
│  │  ┌─────────┐  ┌──────────┐  ┌─────────────────┐    │   │
│  │  │ Models  │  │  Events  │  │ Rules (Exits)   │    │   │
│  │  └─────────┘  └──────────┘  └─────────────────┘    │   │
│  └─────────────────────────────────────────────────────┘   │
│                            │                                │
│  ┌─────────────────────────┴───────────────────────────┐   │
│  │                      Services                        │   │
│  │  ┌──────────────┐  ┌────────────┐  ┌────────────┐   │   │
│  │  │ Execution    │  │ Positions  │  │ Market Data│   │   │
│  │  │ (Leg1/Leg2)  │  │ (Manager)  │  │ (Service)  │   │   │
│  │  └──────────────┘  └────────────┘  └────────────┘   │   │
│  │  ┌──────────────┐  ┌────────────┐  ┌────────────┐   │   │
│  │  │ Opportunities│  │ Liquidity  │  │ Historical │   │   │
│  │  │ (Scanner)    │  │ (Gates)    │  │ (Backfill) │   │   │
│  │  └──────────────┘  └────────────┘  └────────────┘   │   │
│  └─────────────────────────────────────────────────────┘   │
│                            │                                │
│  ┌─────────────────────────┴───────────────────────────┐   │
│  │                       Ports                          │   │
│  │  ┌──────────────┐  ┌────────────┐  ┌────────────┐   │   │
│  │  │ ExchangePort │  │ StorePort  │  │ EventBus   │   │   │
│  │  └──────────────┘  └────────────┘  └────────────┘   │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────┴─────────────────────────────────┐
│                        Adapters                             │
│  ┌──────────────┐  ┌────────────┐  ┌────────────────────┐  │
│  │ Lighter SDK  │  │  X10 SDK   │  │ SQLite Store       │  │
│  │ (exchanges/) │  │ (exchanges)│  │ (store/sqlite/)    │  │
│  └──────────────┘  └────────────┘  └────────────────────┘  │
│  ┌──────────────┐  ┌────────────┐                          │
│  │ Telegram     │  │ Event Bus  │                          │
│  │ (messaging/) │  │ (in-memory)│                          │
│  └──────────────┘  └────────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Module Reference

### Directory Structure

```
src/funding_bot/
├── __init__.py              # Package root (v2.0.0)
├── __main__.py              # Entry point
├── domain/                  # Domain models & business rules
│   ├── models.py           # Core domain entities
│   ├── events.py           # Domain events
│   ├── historical.py       # Historical data models
│   ├── rules.py            # Exit rule definitions
│   └── professional_exits.py # Advanced exit strategies
├── ports/                   # Abstract interfaces
│   ├── exchange.py         # ExchangePort interface
│   ├── store.py            # TradeStorePort interface
│   ├── event_bus.py        # EventBusPort interface
│   └── notification.py     # NotificationPort interface
├── adapters/                # Concrete implementations
│   ├── exchanges/          # Exchange adapters
│   │   ├── lighter/        # Lighter DEX adapter
│   │   └── x10/            # X10 CEX adapter
│   ├── store/sqlite/       # SQLite persistence
│   └── messaging/          # Telegram, EventBus
├── services/                # Business logic
│   ├── execution*.py       # Trade execution (Leg1/Leg2)
│   ├── positions/          # Position management
│   ├── opportunities/      # Opportunity scanning
│   ├── market_data/        # Market data service
│   ├── historical/         # Historical data backfill
│   ├── liquidity_gates*.py # Liquidity validation
│   └── metrics/            # Internal metrics
├── config/                  # Configuration
│   ├── settings.py         # Settings dataclass
│   └── config.yaml         # Default config
├── observability/           # Logging & metrics
│   ├── logging.py          # Structured logging
│   └── metrics.py          # Prometheus metrics
├── app/                     # Application bootstrap
│   ├── run.py              # Main run loop
│   └── supervisor/         # Loop supervisors
├── ui/                      # Terminal UI
│   └── dashboard.py        # Rich dashboard
└── utils/                   # Utilities
    ├── decimals.py         # Decimal helpers
    ├── json_parser.py      # JSON parsing
    └── rest_pool.py        # HTTP connection pool
```

---

## Domain Models

### Core Entities (`domain/models.py`)

#### Enums

| Enum | Values | Description |
|------|--------|-------------|
| `Exchange` | LIGHTER, X10 | Supported exchanges |
| `Side` | BUY, SELL | Order/position direction |
| `OrderType` | LIMIT, MARKET | Order types |
| `TimeInForce` | GTC, IOC, POST_ONLY, FOK | Order time-in-force |
| `OrderStatus` | PENDING, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED | Order lifecycle |
| `TradeStatus` | PENDING, OPENING, OPEN, CLOSING, CLOSED, REJECTED, FAILED, ROLLBACK | Trade lifecycle |
| `ExecutionState` | PENDING → LEG1_SUBMITTED → LEG1_FILLED → LEG2_SUBMITTED → COMPLETE | Execution state machine |

#### Value Objects

```python
@dataclass(frozen=True, slots=True)
class FundingRate:
    """Normalized HOURLY funding rate."""
    symbol: str
    exchange: Exchange
    rate: Decimal          # Hourly rate
    next_funding_time: datetime | None

    @property
    def rate_annual(self) -> Decimal:
        """APY = hourly * 24 * 365"""
        return self.rate * Decimal("24") * Decimal("365")
```

```python
@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Market metadata and precision settings."""
    symbol: str
    exchange: Exchange
    price_precision: int = 2
    qty_precision: int = 4
    tick_size: Decimal = Decimal("0.01")
    min_qty: Decimal
    max_qty: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
```

#### Entities

```python
@dataclass(slots=True)
class Trade:
    """Delta-neutral funding arbitrage trade."""
    trade_id: str
    symbol: str
    leg1: TradeLeg          # Lighter (maker)
    leg2: TradeLeg          # X10 (hedge)
    target_qty: Decimal
    status: TradeStatus
    execution_state: ExecutionState
    funding_collected: Decimal
    realized_pnl: Decimal
    entry_apy: Decimal
    close_reason: str | None
```

```python
@dataclass(slots=True)
class TradeLeg:
    """One leg of a delta-neutral trade."""
    exchange: Exchange
    side: Side
    order_id: str | None
    qty: Decimal
    filled_qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    fees: Decimal

    @property
    def pnl(self) -> Decimal:
        """Net PnL including fees."""
```

```python
@dataclass(frozen=True, slots=True)
class Opportunity:
    """Detected funding arbitrage opportunity (immutable)."""
    symbol: str
    net_funding_hourly: Decimal
    apy: Decimal
    spread_pct: Decimal
    suggested_qty: Decimal
    expected_value_usd: Decimal
    breakeven_hours: Decimal
```

---

## Ports (Interfaces)

### ExchangePort (`ports/exchange.py`)

Abstract interface for all exchange operations:

```python
class ExchangePort(ABC):
    """Exchange adapter interface using only domain types."""

    # Properties
    @property
    def exchange(self) -> Exchange: ...
    @property
    def is_connected(self) -> bool: ...

    # Lifecycle
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # Market Data
    async def load_markets(self) -> dict[str, MarketInfo]: ...
    async def get_mark_price(self, symbol: str) -> Decimal: ...
    async def get_funding_rate(self, symbol: str) -> FundingRate: ...
    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]: ...

    # Account
    async def get_available_balance(self) -> Balance: ...
    async def get_fee_schedule(self, symbol: str | None) -> dict[str, Decimal]: ...

    # Positions
    async def list_positions(self) -> list[Position]: ...
    async def get_position(self, symbol: str) -> Position | None: ...
    async def get_realized_funding(self, symbol: str, start_time: datetime | None) -> Decimal: ...

    # Orders
    async def place_order(self, request: OrderRequest) -> Order: ...
    async def get_order(self, symbol: str, order_id: str) -> Order | None: ...
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    async def cancel_all_orders(self, symbol: str | None) -> int: ...

    # WebSocket (optional)
    async def subscribe_positions(self, callback: PositionCallback) -> None: ...
    async def subscribe_orders(self, callback: OrderCallback) -> None: ...
    async def subscribe_orderbook_l1(self, symbols: list[str] | None) -> None: ...
```

### TradeStorePort (`ports/store.py`)

Abstract interface for persistence:

```python
class TradeStorePort(ABC):
    """Trade storage interface."""

    # Lifecycle
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # Trade CRUD
    async def create_trade(self, trade: Trade) -> str: ...
    async def get_trade(self, trade_id: str) -> Trade | None: ...
    async def update_trade(self, trade_id: str, updates: dict[str, Any]) -> bool: ...
    async def list_open_trades(self) -> list[Trade]: ...

    # Events (Audit Trail)
    async def append_event(self, trade_id: str, event: DomainEvent) -> None: ...

    # Funding
    async def record_funding(self, trade_id: str, exchange: str, amount: Decimal, timestamp: datetime) -> None: ...
    async def get_recent_apy_history(self, symbol: str, hours_back: int) -> list[Decimal]: ...

    # Historical Data
    async def insert_funding_candles(self, candles: list[FundingCandle]) -> int: ...

    # Statistics
    async def get_stats(self) -> dict[str, Any]: ...
```

---

## Services

### Execution Service (`services/execution*.py`)

Handles trade execution with Leg1 (maker) → Leg2 (hedge) flow:

```
┌─────────────────────────────────────────────────────────────┐
│                    Execution Flow                           │
├─────────────────────────────────────────────────────────────┤
│  1. execution_impl.py      → Entry point, orchestration     │
│  2. execution_leg1.py      → Leg1 maker order management    │
│  3. execution_leg2.py      → Leg2 hedge order management    │
│  4. execution_rollback.py  → Rollback on partial fills      │
│  5. execution_flow.py      → State machine transitions      │
└─────────────────────────────────────────────────────────────┘
```

**Key Files:**

| File | Responsibility |
|------|----------------|
| `execution_impl.py` | Main execution orchestration |
| `execution_leg1.py` | Leg1 (Lighter maker) order flow |
| `execution_leg1_pricing.py` | Maker price calculation |
| `execution_leg1_fill_ops.py` | Fill detection and handling |
| `execution_leg1_attempts.py` | Retry logic with backoff |
| `execution_leg2.py` | Leg2 (X10 hedge) order flow |
| `execution_leg2_helpers.py` | Hedge latency tracking |
| `execution_rollback.py` | Partial fill rollback |
| `execution_orderbook.py` | Orderbook utilities |

### Position Manager (`services/positions/`)

Manages open trades and exit evaluation:

```python
class PositionManager:
    """
    Manages open trades and handles exit logic.

    Responsibilities:
    - Monitor trades for exit conditions
    - Close both legs simultaneously
    - Calculate and record PnL
    - Verify positions are actually closed
    """

    async def evaluate_and_close(self) -> None: ...
    async def close_trade(self, trade: Trade, reason: str) -> CloseResult: ...
    async def rebalance_trade(self, trade: Trade) -> bool: ...
```

**Exit Rule Priority Stack:**

```
Layer 1 (Emergency):
  - BROKEN_HEDGE      → Missing leg detected
  - LIQUIDATION       → Liquidation imminent

Layer 2 (Economic):
  - NET_EV_NEGATIVE   → Expected value turned negative
  - OPPORTUNITY_COST  → Better opportunity available
  - FUNDING_FLIP      → Funding direction reversed

Layer 3 (Optional):
  - TAKE_PROFIT       → Target profit reached
  - VELOCITY_EXIT     → Funding rate declining rapidly
  - Z_SCORE_EXIT      → Statistical deviation from mean
```

### Opportunity Engine (`services/opportunities/`)

Scans and evaluates funding arbitrage opportunities:

```python
class OpportunityEngine:
    """
    Scans for and evaluates funding arbitrage opportunities.

    Filter Pipeline:
    1. Blacklist check
    2. Spread limit
    3. Minimum APY
    4. Liquidity score
    5. Expected value (including fees)
    6. Breakeven time
    """

    async def scan(self) -> list[Opportunity]: ...
    async def get_best_opportunity(self) -> Opportunity | None: ...
```

**Key Files:**

| File | Responsibility |
|------|----------------|
| `scanner.py` | Main scanning logic |
| `evaluator.py` | Symbol evaluation |
| `filters.py` | Filter pipeline |
| `scoring.py` | EV, breakeven, liquidity |
| `sizing.py` | Position sizing |
| `diagnostics.py` | Scan diagnostics |

### Market Data Service (`services/market_data/`)

Provides unified market data access:

```python
class MarketDataService:
    """
    Unified market data access with caching.

    Features:
    - Parallel refresh (20 concurrent)
    - L1 orderbook caching
    - Funding rate caching
    - WebSocket integration
    """

    async def refresh_all(self) -> None: ...
    async def get_funding_rates(self, symbol: str) -> tuple[FundingRate, FundingRate]: ...
    async def get_orderbook_l1(self, symbol: str, exchange: Exchange) -> dict: ...
```

### Historical Service (`services/historical/`)

Handles historical data backfill with rate limiting:

```python
async def backfill(
    symbols: list[str],
    days_back: int = 7,
    max_concurrency: int = 2,
    rate_limit_delay: float = 0.5,
) -> BackfillResult:
    """
    Backfill historical funding rates with session reuse.

    Features:
    - aiohttp session pooling (10 connections)
    - Transient error handling (429, 5xx)
    - Bounds validation (max 1% hourly rate)
    - Progress logging
    """
```

### Liquidity Gates (`services/liquidity_gates*.py`)

Pre-trade liquidity validation:

| File | Responsibility |
|------|----------------|
| `liquidity_gates.py` | Main gate facade |
| `liquidity_gates_l1.py` | Level 1 (spread/depth) checks |
| `liquidity_gates_preflight.py` | Pre-execution validation |
| `liquidity_gates_impact_core.py` | Price impact calculation |
| `liquidity_gates_config.py` | Gate thresholds |
| `liquidity_gates_types.py` | Gate result types |

---

## Adapters

### Lighter Adapter (`adapters/exchanges/lighter/`)

```python
class LighterAdapter(ExchangePort):
    """
    Lighter DEX adapter with Premium rate limits.

    Features:
    - WebSocket orderbook streaming
    - Account index support (0 vs None semantics)
    - Connection pooling (100 per-host)
    - Rate limit: 24,000 req/min
    """
```

Key files:
- `adapter.py` - Main adapter implementation
- `ws_client.py` - WebSocket client
- `orderbook.py` - Orderbook management

### X10 Adapter (`adapters/exchanges/x10/`)

```python
class X10Adapter(ExchangePort):
    """
    X10 (Extended) CEX adapter.

    Features:
    - StarkNet integration
    - Funding rate API
    - Position management
    """
```

### SQLite Store (`adapters/store/sqlite/`)

```python
class SQLiteStore(TradeStorePort):
    """
    SQLite persistence with write-behind pattern.

    Features:
    - WAL mode for concurrency
    - Async write queue (batching)
    - Automatic migrations
    - PnL snapshot history
    """
```

Key files:

| File | Responsibility |
|------|----------------|
| `store.py` | Main store implementation |
| `schema.py` | Database schema |
| `migrations.py` | Schema migrations |
| `write_queue.py` | Write-behind queue |
| `trades.py` | Trade CRUD |
| `funding.py` | Funding records |
| `historical.py` | Historical data |
| `events.py` | Event logging |
| `stats.py` | Aggregations |
| `utils.py` | DB utilities |

---

## Configuration

### Settings Structure (`config/settings.py`)

```python
@dataclass
class Settings:
    # Mode
    testing_mode: bool = False
    live_trading: bool = False

    # Exchange settings
    lighter: ExchangeSettings
    x10: ExchangeSettings

    # Strategy settings
    strategy: StrategySettings

    # Exit rules
    exit_rules: ExitRulesSettings

    # Risk limits
    risk: RiskSettings

    # Logging
    logging: LoggingSettings

@dataclass
class StrategySettings:
    min_apy: Decimal = Decimal("10")
    max_spread_pct: Decimal = Decimal("0.1")
    min_expected_value_usd: Decimal = Decimal("1")
    max_breakeven_hours: int = 4

@dataclass
class ExitRulesSettings:
    min_hold_hours: float = 1.0
    net_ev_exit_threshold_usd: Decimal = Decimal("-1")
    take_profit_apy: Decimal | None = None
    velocity_exit_enabled: bool = False
    z_score_exit_enabled: bool = False
```

### Config File (`config/config.yaml`)

```yaml
testing_mode: false
live_trading: false

lighter:
  api_key: ${LIGHTER_API_KEY}
  private_key: ${LIGHTER_PRIVATE_KEY}
  account_index: 0
  funding_rate_interval_hours: 1  # ALWAYS 1 (hourly)

x10:
  api_key: ${X10_API_KEY}
  private_key: ${X10_PRIVATE_KEY}
  funding_rate_interval_hours: 1  # ALWAYS 1 (hourly)

strategy:
  min_apy: 10
  max_spread_pct: 0.1
  symbols:
    - BTC-USD
    - ETH-USD

exit_rules:
  min_hold_hours: 1.0
  net_ev_exit_threshold_usd: -1

risk:
  max_position_notional_usd: 10000
  max_total_notional_usd: 50000
  max_concurrent_positions: 5
```

---

## Observability

### Logging (`observability/logging.py`)

```python
# Log tags for categorization
LOG_TAG_TRADE = "[TRADE]"    # Trade events (cyan)
LOG_TAG_PROFIT = "[PROFIT]"  # PnL events (cyan)
LOG_TAG_HEALTH = "[HEALTH]"  # Health checks (blue)
LOG_TAG_SCAN = "[SCAN]"      # Scans (grey/dimmed)

# Setup
logger = setup_logging(settings)

# Usage
logger.info(f"{LOG_TAG_TRADE} Opened trade {trade_id}")
```

Features:
- Colored console output (BotLogFormatter)
- JSON file logging (optional)
- Sensitive data masking (API keys, tokens)
- Rotating file handlers

### Metrics (`observability/metrics.py`)

Prometheus metrics (optional dependency):

```python
# Close operations
record_close_operation(symbol, reason, success, duration_seconds, pnl_usd)

# Execution
record_execution(symbol, outcome, duration_seconds, notional_usd)
record_leg1_order(symbol, outcome, fill_rate)
record_leg2_order(symbol, outcome, fill_rate)
record_hedge_latency(symbol, latency_seconds)

# Context managers
with track_close_duration("ETH", "ECON_EXIT") as ctx:
    # perform close
    ctx["success"] = True
    ctx["pnl_usd"] = 15.50
```

Available metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `funding_bot_close_operations_total` | Counter | Close operations by symbol/reason/success |
| `funding_bot_close_duration_seconds` | Histogram | Close operation duration |
| `funding_bot_close_pnl_usd` | Histogram | Realized PnL distribution |
| `funding_bot_execution_total` | Counter | Trade executions by outcome |
| `funding_bot_leg1_fill_rate` | Gauge | Leg1 maker fill rate |
| `funding_bot_hedge_latency_seconds` | Histogram | Leg1→Leg2 latency |
| `funding_bot_active_positions` | Gauge | Current open positions |

---

## Performance Characteristics

### Premium-Optimized Settings

```python
# Connection pooling (utils/rest_pool.py)
connector = TCPConnector(
    limit=200,           # Total connections
    limit_per_host=100,  # Per-host connections
    keepalive_timeout=300,  # 5 minutes
    ttl_dns_cache=1800,  # 30 minutes DNS cache
)

# Parallel operations
MARKET_DATA_CONCURRENCY = 20  # Parallel symbol refresh
EXIT_EVAL_CONCURRENCY = 10    # Parallel position eval
DB_BATCH_SIZE = 100           # executemany batch size

# Caching
MARKET_INFO_CACHE_TTL = 3600  # 1 hour
ORDERBOOK_CACHE_TTL = 1       # 1 second (L1)
```

### Rate Limits

| Exchange | Rate Limit | Headroom |
|----------|------------|----------|
| Lighter (Premium) | 24,000 req/min | 80% alert threshold |
| X10 | Varies | Adaptive backoff |

### Historical Backfill Rate Limits

```python
# Constants (services/historical/ingestion.py)
API_TIMEOUT_SECONDS = 30
SESSION_POOL_LIMIT = 10
SESSION_KEEPALIVE_SECONDS = 30
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
FUNDING_RATE_MAX_ABS = Decimal("0.01")  # Max 1% hourly
```

---

## Entry Points

### Main Entry (`__main__.py`)

```bash
# Run the bot
python -m funding_bot

# Or via installed package
funding-bot
```

### Application Bootstrap (`app/run.py`)

```python
async def run_bot(settings: Settings) -> None:
    """Main application entry point."""
    # 1. Initialize adapters
    # 2. Initialize services
    # 3. Start supervisor loops
    # 4. Handle graceful shutdown
```

### Supervisor Loops (`app/supervisor/loops.py`)

```python
# Main loops
async def opportunity_loop(engine, executor): ...
async def position_loop(manager): ...
async def funding_loop(manager): ...
async def health_loop(adapters): ...
```

---

## Testing

### Test Structure

```
tests/
├── unit/                    # Offline unit tests
│   ├── domain/             # Domain model tests
│   ├── services/           # Service tests
│   └── adapters/           # Adapter tests (mocked)
├── integration/            # Integration tests (marked)
└── fixtures/               # Test fixtures
```

### Running Tests

```bash
# Unit tests only (offline, no SDK required)
pytest tests/unit/ -q

# Full test suite
pytest -q

# With coverage
pytest --cov=funding_bot tests/
```

### Test Requirements

- Unit tests must run **offline** (no DNS, no exchange SDK)
- Use `try/except ImportError` for optional SDK imports
- Integration tests marked with `@pytest.mark.integration`
- Currently: **394 unit tests passing**

---

## See Also

- [PLANNING.md](../PLANNING.md) - Architecture principles, absolute rules
- [TASK.md](../TASK.md) - Current tasks, backlog
- [KNOWLEDGE.md](../KNOWLEDGE.md) - Troubleshooting, best practices
- [CONTRIBUTING.md](../CONTRIBUTING.md) - Contribution guidelines
