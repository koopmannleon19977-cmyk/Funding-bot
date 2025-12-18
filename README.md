# 🚀 Funding Arbitrage Bot

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Lighter + X10](https://img.shields.io/badge/Platform-Lighter%20%2B%20X10-green.svg)](#)

⚠️ **Disclaimer: This is a professional trading bot for perpetual futures arbitrage. Use at your own risk. The authors are not responsible for any financial losses incurred through the use of this software. Arbitrage trading involves significant risks, including exchange failures, liquidation, and execution slippage.**

A high-performance **funding rate arbitrage system** that captures the spread between **Lighter Protocol** (DEX) and **X10 Exchange** (CEX). The bot automatically identifies profitable opportunities, executes hedged multi-leg trades, and earns funding payments while maintaining delta-neutral exposure.

---

## 📦 Table of Contents

- [Key Features](#-key-features)
- [Architecture Overview](#-architecture-overview)
- [Getting Started](#-getting-started)
- [Configuration Reference](#-configuration-reference)
- [Core Concepts](#-core-concepts)
- [Module Reference](#-module-reference)
- [API Reference](#-api-reference)
- [Safety & Risk Controls](#-safety--risk-controls)
- [Maintenance & Tools](#-maintenance--tools)
- [Troubleshooting](#-troubleshooting)
- [Pro Tips](#-pro-tips)
- [License](#-license)

---

## ✨ Key Features

### Trading & Execution
| Feature | Description |
|---------|-------------|
| **Parallel Leg Execution** | Executes Lighter (Maker) and X10 (Taker) legs simultaneously to minimize unhedged exposure |
| **Intelligent Maker Logic** | Dynamic timeout calculation based on orderbook liquidity; automatic price adjustments for fills |
| **Optimistic Rollback** | Automatically reverts partially filled trades to prevent "orphan" positions |
| **Maker-to-Taker Escalation** | Falls back to taker orders if maker doesn't fill within timeout (with EV-recheck) |
| **Batch Order Support** | Lighter batch transactions for reduced latency and gas costs |

### Financial Precision
| Feature | Description |
|---------|-------------|
| **Decimal Arithmetic** | All financial calculations use `Decimal` for precision |
| **Real-time Fee Management** | Dynamic fee tracking via `FeeManager` with exchange API integration |
| **Accurate PnL Tracking** | Persisted realized/unrealized PnL with hedge pair calculations |
| **Funding Payment Tracking** | Automatic capture of hourly funding payments from both exchanges |

### Safety & Reliability
| Feature | Description |
|---------|-------------|
| **State Persistence** | SQLite-backed state machine with crash recovery |
| **Volatility Monitoring** | Panic-close on extreme volatility (Flash Crash Protection) |
| **Circuit Breakers** | Max drawdown limits, consecutive failure protection |
| **Graceful Shutdown** | Idempotent shutdown with position verification |
| **Latency Arbitrage Detection** | Exploits funding rate update delays between exchanges |

### Monitoring & Alerting
| Feature | Description |
|---------|-------------|
| **Structured JSON Logging** | JSONL format for Grafana/ELK dashboards |
| **Telegram Integration** | Real-time alerts for trades, errors, and shutdowns |
| **WebSocket Health Monitoring** | Auto-reconnect with exponential backoff |

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         FUNDING ARBITRAGE BOT                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐   │
│  │   Lighter    │◄──►│   Opportunity    │◄──►│       X10        │   │
│  │   Adapter    │    │     Finder       │    │     Adapter      │   │
│  └──────┬───────┘    └────────┬─────────┘    └────────┬─────────┘   │
│         │                     │                       │              │
│         ▼                     ▼                       ▼              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              PARALLEL EXECUTION MANAGER                      │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │    │
│  │  │  Leg 1      │  │  Leg 2      │  │  Rollback           │  │    │
│  │  │  (Lighter)  │  │  (X10)      │  │  Processor          │  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────────────┘  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                       │
│         ┌────────────────────┼────────────────────┐                 │
│         ▼                    ▼                    ▼                 │
│  ┌────────────┐    ┌─────────────────┐    ┌──────────────┐          │
│  │   State    │    │     Trade       │    │   Funding    │          │
│  │  Manager   │    │   Management    │    │   Tracker    │          │
│  └─────┬──────┘    └─────────────────┘    └──────────────┘          │
│        │                                                             │
│        ▼                                                             │
│  ┌─────────────────┐    ┌────────────────┐    ┌─────────────────┐   │
│  │    SQLite DB    │    │ Volatility     │    │    Shutdown     │   │
│  │  (Persistence)  │    │ Monitor        │    │  Orchestrator   │   │
│  └─────────────────┘    └────────────────┘    └─────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
1. Opportunity Detection     2. Trade Execution          3. Position Management
┌──────────────────┐        ┌───────────────────┐       ┌───────────────────┐
│ Scan both        │        │ Send Lighter Leg  │       │ Monitor PnL       │
│ exchanges for    ├───────►│ (Maker/Limit)     ├──────►│ Track Funding     │
│ funding spread   │        │ + X10 Leg (Taker) │       │ Check Exit Cond.  │
└──────────────────┘        └───────────────────┘       └───────────────────┘
        │                           │                           │
        │  Filters:                 │  State Machine:           │  Exit when:
        │  • APY > 35%              │  PENDING → LEG1_SENT      │  • Min profit hit
        │  • Spread < 0.2%          │  → LEG1_FILLED            │  • Max hold time
        │  • Breakeven < 8h         │  → LEG2_SENT              │  • APY flips
        │  • Liquidity OK           │  → COMPLETE               │  • Volatility spike
        └───────────────────────────┴───────────────────────────┘
```

---

## 🎯 Getting Started

### Step 1: Prerequisites

```bash
# Required
- Python 3.10 or higher
- pip (Python package manager)
- Node.js 18+ (for Lighter TypeScript SDK)

# Exchange Accounts
- Lighter Protocol account with API keys
- X10 Exchange account with API keys
- Funded accounts on both exchanges (recommended: $200+ each)
```

### Step 2: Installation

```powershell
# Clone the repository
git clone <repository-url>
cd funding-bot

# Create virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install Python dependencies
pip install -r requirements.txt

# Install Lighter Python SDK (from GitHub)
pip install git+https://github.com/elliottech/lighter-python.git

# Build Lighter TypeScript SDK (for WebSocket order client)
cd lighter-ts-main
npm install
npm run build
cd ..
```

### Step 3: Configuration

Create a `.env` file in the project root:

```bash
# ═══════════════════════════════════════════════════════════════════
# LIGHTER PROTOCOL CREDENTIALS
# ═══════════════════════════════════════════════════════════════════
# Get these from: https://lighter.xyz → Settings → API Keys

LIGHTER_PRIVATE_KEY=0x...              # Your wallet private key
LIGHTER_API_PRIVATE_KEY=0x...          # API key private key (for signing)
# Note: LIGHTER_ACCOUNT_INDEX and LIGHTER_API_KEY_INDEX are set in config.py

# ═══════════════════════════════════════════════════════════════════
# X10 EXCHANGE CREDENTIALS  
# ═══════════════════════════════════════════════════════════════════
# Get these from: X10 Exchange → Settings → API Keys

X10_PRIVATE_KEY=0x...                  # StarkNet private key
X10_PUBLIC_KEY=0x...                   # StarkNet public key
X10_API_KEY=your-api-key-here          # REST API key
X10_VAULT_ID=12345                     # Your vault ID (integer)

# ═══════════════════════════════════════════════════════════════════
# OPTIONAL: TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN=                    # From @BotFather
TELEGRAM_CHAT_ID=                      # Your chat ID
```

### Step 4: Customize Trading Parameters

Edit `config.py` to adjust your trading strategy:

```python
# Position Settings
DESIRED_NOTIONAL_USD = 150.0     # USD amount per trade
MAX_OPEN_TRADES = 2               # Maximum concurrent positions
LEVERAGE_MULTIPLIER = 5.0         # Maximum leverage (exchange limit)

# Profitability Filters
MIN_APY_FILTER = 0.35             # 35% minimum APY to enter
MAX_BREAKEVEN_HOURS = 8.0         # Trade must breakeven within 8h
MIN_PROFIT_EXIT_USD = 0.10        # Minimum profit to close

# Safety Settings
MAX_HOLD_HOURS = 72.0             # Force close after 72 hours
MAX_SPREAD_FILTER_PERCENT = 0.002 # 0.2% max bid-ask spread
CB_MAX_DRAWDOWN_PCT = 0.20        # 20% max drawdown (circuit breaker)
```

### Step 5: Run the Bot

```powershell
# Option 1: Use the startup batch file (Windows)
.\START_BOT2.bat

# Option 2: Run directly
python src/main.py

# Option 3: Dry run mode (simulation without real trades)
# Edit config.py first:
#   LIGHTER_DRY_RUN = True
#   X10_DRY_RUN = True
python src/main.py
```

---

## ⚙️ Configuration Reference

### Position & Capital Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DESIRED_NOTIONAL_USD` | `150.0` | USD value per trade |
| `MAX_OPEN_TRADES` | `2` | Maximum concurrent positions |
| `LEVERAGE_MULTIPLIER` | `5.0` | Max leverage (usually 2-5x actual) |
| `MIN_POSITION_SIZE_USD` | `50.0` | Hard minimum for any trade |
| `MAX_NOTIONAL_USD` | `225.0` | Hard cap per trade (1.5x desired) |

### Profitability Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_APY_FILTER` | `0.35` | 35% APY minimum to enter trade |
| `MIN_APY_FALLBACK` | `0.25` | 25% fallback if dynamic calc fails |
| `MAX_BREAKEVEN_HOURS` | `8.0` | Must breakeven within 8 hours |
| `MIN_PROFIT_EXIT_USD` | `0.10` | Minimum USD profit to close |
| `MIN_NET_PROFIT_EXIT_USD` | `0.05` | Net profit after all fees |

### Hold Time & Exit Conditions

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MINIMUM_HOLD_SECONDS` | `7200` | 2 hour minimum hold (capture funding) |
| `MAX_HOLD_HOURS` | `72.0` | Force close after 72 hours |
| `MIN_MAINTENANCE_APY` | `0.20` | Exit if APY drops below 20% |
| `MIN_FUNDING_BEFORE_EXIT_USD` | `0.03` | Min funding collected before exit |

### Safety & Circuit Breakers

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CB_MAX_CONSECUTIVE_FAILURES` | `5` | Stop after 5 consecutive failures |
| `CB_MAX_DRAWDOWN_PCT` | `0.20` | 20% max drawdown limit |
| `CB_ENABLE_KILL_SWITCH` | `True` | Enable automatic shutdown |
| `VOLATILITY_PANIC_THRESHOLD` | `8.0` | 8% = panic close position |
| `MAX_SPREAD_FILTER_PERCENT` | `0.002` | 0.2% max bid-ask spread |

### Exchange Fees

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TAKER_FEE_X10` | `0.000225` | 0.0225% X10 taker fee |
| `MAKER_FEE_X10` | `0.0` | 0.00% X10 maker fee |
| `MAKER_FEE_LIGHTER` | `0.0` | 0.00% Lighter maker fee |
| `TAKER_FEE_LIGHTER` | `0.0` | 0.00% Lighter taker fee |

### WebSocket & Timeouts

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIGHTER_ORDER_TIMEOUT_SECONDS` | `45.0` | Maker order timeout |
| `MAKER_ORDER_MAX_RETRIES` | `3` | Max retries for maker orders |
| `WS_RECONNECT_DELAY_INITIAL` | `2` | Initial reconnect delay (seconds) |
| `WS_RECONNECT_DELAY_MAX` | `120` | Max reconnect delay |

---

## 📚 Core Concepts

### 1. The Arbitrage Strategy

The bot earns profit by going **Long** on one exchange and **Short** on another for the same asset. This creates a **delta-neutral** position that earns the funding rate spread:

```
┌─────────────────────────────────────────────────────────────────┐
│  FUNDING RATE ARBITRAGE                                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Lighter: LONG ETH   (+0.02% hourly = receiving funding)        │
│  X10:     SHORT ETH  (-0.01% hourly = paying funding)           │
│  ────────────────────────────────────────────────────           │
│  Net Funding: +0.01% per hour                                    │
│                                                                  │
│  With $150 position on each side:                               │
│  • Hourly profit: $300 × 0.01% = $0.03/hour                     │
│  • Daily profit: $0.72                                           │
│  • Annual APY: ~88%                                              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2. Leg Execution (Maker-Leg Priority)

The bot optimizes for lowest fees by using Maker orders on Lighter:

```
Step 1: Analyze Opportunity
┌────────────────────────────────────────┐
│ • Check funding rates on both exchanges │
│ • Calculate expected profit             │
│ • Verify liquidity and spread           │
│ • Pass APY and breakeven filters        │
└────────────────────────────────────────┘
                    ↓
Step 2: Execute Lighter Leg (Maker)
┌────────────────────────────────────────┐
│ • Place POST_ONLY limit order           │
│ • Wait for fill (dynamic timeout)       │
│ • If timeout: retry with better price   │
│ • If still no fill: escalate to Taker   │
└────────────────────────────────────────┘
                    ↓
Step 3: Execute X10 Leg (Taker)
┌────────────────────────────────────────┐
│ • Send IOC (Immediate-or-Cancel) order  │
│ • Match size with Lighter fill          │
│ • Confirm hedge completion              │
└────────────────────────────────────────┘
                    ↓
Step 4: Position Management
┌────────────────────────────────────────┐
│ • Track funding payments (hourly)       │
│ • Monitor for exit conditions           │
│ • Close both legs when profitable       │
└────────────────────────────────────────┘
```

### 3. State Machine

Each trade follows a defined state machine:

```
┌──────────┐     ┌───────────┐     ┌─────────────┐     ┌───────────┐     ┌──────────┐
│ PENDING  │────►│ LEG1_SENT │────►│ LEG1_FILLED │────►│ LEG2_SENT │────►│ COMPLETE │
└──────────┘     └───────────┘     └─────────────┘     └───────────┘     └──────────┘
                       │                  │                   │
                       ▼                  ▼                   ▼
                 ┌──────────┐      ┌─────────────────┐  ┌──────────────────────┐
                 │  FAILED  │      │ ROLLBACK_QUEUED │  │ ROLLBACK_IN_PROGRESS │
                 └──────────┘      └─────────────────┘  └──────────────────────┘
```

### 4. Price Units & Scaling

```python
# Position sizes are in USD
size_usd = 150.0  # $150 per leg

# Quantities are calculated per exchange
quantity_coins = size_usd / price  # e.g., $150 / $4000 = 0.0375 ETH

# Quantities are aligned to exchange step sizes
x10_step = 0.001      # 3 decimal places
lighter_step = 0.0001 # 4 decimal places
common_qty = align_to_step(quantity_coins, max(x10_step, lighter_step))
```

---

## 🛠️ Module Reference

### Core Modules

#### `src/main.py`
Entry point for the bot. Initializes all components and starts the main event loop.

#### `src/parallel_execution.py`
The **execution engine** that manages hedged trade execution:
- `ParallelExecutionManager`: Main class for trade execution
- `execute_trade_parallel()`: Execute both legs simultaneously
- `_rollback_processor()`: Background task for failed trade recovery
- State machine: `PENDING → LEG1_SENT → LEG1_FILLED → LEG2_SENT → COMPLETE`

```python
# Example usage
execution_manager = ParallelExecutionManager(x10_adapter, lighter_adapter, db)
await execution_manager.start()

success, x10_order_id, lighter_order_id = await execution_manager.execute_trade_parallel(
    symbol="ETH-USD",
    side_x10="SHORT",
    side_lighter="LONG",
    size_x10=Decimal("0.0375"),
    size_lighter=Decimal("0.0375"),
    price_lighter=Decimal("4000.00")
)
```

#### `src/core/opportunities.py`
**Opportunity detection** module:
- `find_opportunities()`: Scans both exchanges for profitable trades
- `calculate_expected_profit()`: Computes expected PnL including fees
- Filters: APY, spread, liquidity, volatility, breakeven time

```python
# Returns list of opportunities sorted by APY
opportunities = await find_opportunities(
    lighter=lighter_adapter,
    x10=x10_adapter,
    open_syms={"BTC-USD"},  # Already open positions
    is_farm_mode=False
)

# Example opportunity dict:
{
    "symbol": "ETH-USD",
    "apy": 0.65,                    # 65% APY
    "side_lighter": "LONG",
    "side_x10": "SHORT",
    "expected_profit_24h": 0.45,    # $0.45 in 24h
    "breakeven_hours": 4.2,
    "spread_pct": 0.0015,
    "is_latency_arb": False
}
```

#### `src/state_manager.py`
**In-memory state manager** with write-behind persistence:
- `InMemoryStateManager`: Singleton for trade state
- `TradeState`: Dataclass for trade information
- Write-behind pattern: Memory-first, async DB writes

```python
# Get state manager
state_manager = get_state_manager()

# Add a new trade
trade = TradeState(
    symbol="ETH-USD",
    status=TradeStatus.OPEN,
    side_x10="SHORT",
    side_lighter="LONG",
    size_usd=150.0,
    entry_price_x10=4000.0,
    entry_price_lighter=4001.50
)
await state_manager.add_trade(trade)

# Get all open trades
open_trades = state_manager.get_all_open_trades()

# Close a trade with PnL
await state_manager.close_trade("ETH-USD", pnl=Decimal("0.45"), funding=Decimal("0.12"))
```

#### `src/core/trade_management.py`
**Position management** and exit logic:
- `manage_open_trades()`: Main monitoring loop
- `calculate_realized_pnl()`: PnL estimation for exit decisions
- `calculate_realized_close_pnl()`: Accurate PnL for accounting
- Exit conditions: Profit target, max hold time, APY flip, volatility

```python
# Called periodically by the main loop
exit_results = await manage_open_trades(lighter, x10, state_manager)

# Returns list of trades that were closed
for result in exit_results:
    print(f"Closed {result['symbol']}: PnL ${result['pnl']:.4f}")
```

#### `src/shutdown.py`
**Graceful shutdown coordinator**:
- `ShutdownOrchestrator`: Idempotent shutdown sequence
- Position verification and close
- State persistence

```python
orchestrator = get_shutdown_orchestrator()
orchestrator.configure(
    lighter=lighter_adapter,
    x10=x10_adapter,
    state_manager=state_manager,
    execution_manager=execution_manager
)

result = await orchestrator.shutdown(reason="User requested")
print(f"Shutdown complete. Errors: {result.errors}")
```

### Adapters

#### `src/adapters/lighter_adapter.py`
**Lighter Protocol integration** (292KB):
- REST API client with rate limiting
- WebSocket order submission
- Nonce management with hard refresh
- Order types: LIMIT, MARKET, IOC, POST_ONLY
- Batch order support

```python
lighter = LighterAdapter()
await lighter.initialize()

# Place a maker order
result = await lighter.place_limit_order(
    symbol="ETH-USD",
    side="BUY",
    size=0.0375,
    price=4000.0,
    post_only=True
)

# Get positions
positions = await lighter.get_positions()

# Close position
await lighter.close_position("ETH-USD", reduce_only=True)
```

#### `src/adapters/x10_adapter.py`
**X10 Exchange integration** (139KB):
- StarkNet signing for authentication
- WebSocket for real-time data
- Order types: MARKET, LIMIT, IOC
- Self-trade protection (STP)

```python
x10 = X10Adapter()
await x10.initialize()

# Place a market order (IOC)
result = await x10.place_order(
    symbol="ETH-USD",
    side="SELL",
    size=0.0375,
    order_type="MARKET"
)

# Get account balance
balance = await x10.get_balance()
print(f"Available: ${balance['available']}")
```

### Monitoring Modules

#### `src/funding_tracker.py`
**Funding payment tracking**:
- Fetches realized funding from both exchanges
- Updates trade state with funding collected
- Saves PnL snapshots to database

#### `src/volatility_monitor.py`
**Volatility-based risk control**:
- Tracks 24h price volatility
- Regime detection: LOW, NORMAL, HIGH, EXTREME
- Dynamic spread limits
- Panic-close triggers

#### `src/fee_manager.py`
**Dynamic fee management**:
- Fetches real-time fees from exchanges
- Tier-based fee calculation
- Fee estimation for PnL calculations

### Utility Modules

#### `src/websocket_manager.py`
**WebSocket connection manager** (144KB):
- Auto-reconnect with exponential backoff
- Ping/pong handling (Lighter uses server-initiated pings)
- Subscription management
- Error 1006 handling

#### `src/database.py`
**SQLite database layer**:
- Async database operations
- Trade history persistence
- Funding payment records
- PnL snapshots

#### `src/rate_limiter.py`
**API rate limiting**:
- Per-endpoint rate limits
- Shutdown-safe (allows reduce-only orders)

---

## 📊 API Reference

### Exchange Adapters

#### LighterAdapter

| Method | Description |
|--------|-------------|
| `initialize()` | Connect to Lighter, load markets |
| `place_limit_order(symbol, side, size, price, post_only=True)` | Place limit order |
| `place_market_order(symbol, side, size)` | Place market order |
| `cancel_order(order_id)` | Cancel specific order |
| `cancel_all_orders(symbol=None)` | Cancel all orders |
| `get_positions()` | Get all open positions |
| `get_orderbook(symbol)` | Get orderbook snapshot |
| `get_funding_rate(symbol)` | Get current funding rate |
| `close_position(symbol, reduce_only=True)` | Close position |

#### X10Adapter

| Method | Description |
|--------|-------------|
| `initialize()` | Connect to X10, authenticate |
| `place_order(symbol, side, size, order_type, price=None)` | Place order |
| `cancel_order(order_id)` | Cancel specific order |
| `get_positions()` | Get all open positions |
| `get_balance()` | Get account balance |
| `get_funding_rate(symbol)` | Get current funding rate |
| `get_funding_history(symbol, since)` | Get funding payments |

### State Manager

| Method | Description |
|--------|-------------|
| `add_trade(trade: TradeState)` | Add new trade to state |
| `get_trade(symbol)` | Get trade by symbol |
| `get_all_open_trades()` | Get all open trades |
| `update_trade(symbol, updates: dict)` | Update trade fields |
| `close_trade(symbol, pnl, funding)` | Close and persist trade |
| `start()` | Start background tasks |
| `stop()` | Graceful shutdown |

### Database Operations

| Method | Description |
|--------|-------------|
| `save_trade(trade_dict)` | Save trade to DB |
| `update_trade(symbol, updates)` | Update trade record |
| `get_open_trades()` | Get all open trades from DB |
| `get_trade_history(limit)` | Get recent closed trades |
| `save_funding_payment(payment)` | Save funding record |

---

## 🔒 Safety & Risk Controls

### Circuit Breakers

```python
# Stop after consecutive failures
CB_MAX_CONSECUTIVE_FAILURES = 5

# Stop on excessive drawdown  
CB_MAX_DRAWDOWN_PCT = 0.20  # 20%

# Kill switch enabled
CB_ENABLE_KILL_SWITCH = True
```

### Volatility Protection

```python
# Panic close at high volatility
VOLATILITY_PANIC_THRESHOLD = 8.0      # 8% 24h volatility

# Block new entries at extreme volatility
VOLATILITY_HARD_CAP_THRESHOLD = 50.0  # 50%
```

### Position Limits

```python
# Maximum exposure
MAX_OPEN_TRADES = 2
MAX_EXPOSURE_PCT = 10.0

# Minimum margin
MIN_FREE_MARGIN_PCT = 0.05  # 5%
```

### Blacklisted Symbols

Symbols that are excluded from trading:

```python
BLACKLIST_SYMBOLS = {
    "XAU-USD", "XAG-USD", "USDJPY-USD",  # Invalid margin mode
    "MEGA-USD",                            # Ghost positions
    "S-USD", "LINEA-USD"                  # Low liquidity
}
```

---

## 🔧 Maintenance & Tools

### Log Files

| File | Description |
|------|-------------|
| `logs/funding_bot.log` | Human-readable logs |
| `logs/funding_bot_json.jsonl` | Structured JSON logs (for Grafana/ELK) |
| `logs/funding_bot_*_FULL.log` | Per-session full logs |

### Database

```bash
# Database location
data/funding.db

# Check database
python scripts/check_db.py

# Export funding history
python scripts/export_funding_history.py
```

### Scripts

| Script | Description |
|--------|-------------|
| `scripts/check_db.py` | Inspect database state |
| `scripts/check_exchange_positions.py` | View current positions |
| `scripts/export_funding_history.py` | Export funding to CSV |
| `scripts/backup.py` | Create full project backup |
| `src/maintenance/cleanup_unhedged.py` | Emergency unhedged position cleanup |

### Backup & Recovery

```bash
# Create backup
python scripts/backup.py

# Backups are saved to:
backups/YYYYMMDD_HHMMSS/
```

---

## 🔍 Troubleshooting

### Common Issues

#### WebSocket Error 1006 (Abnormal Closure)
```
Problem: Connection closed without close frame
Solution: The bot auto-reconnects. If persistent:
  1. Check internet connection
  2. Verify Lighter WS is responding to our pong messages
  3. Check for rate limit violations
```

#### "Invalid Nonce" Errors
```
Problem: Transaction nonce out of sync
Solution: The bot performs hard nonce refresh automatically.
If persistent, restart the bot to re-sync.
```

#### Position Mismatch
```
Problem: One leg filled but other didn't
Solution: Optimistic rollback will automatically close
the orphan position. Check logs for ROLLBACK entries.
```

#### No Opportunities Found
```
Problem: "0 opportunities" in logs
Possible causes:
  1. Funding rates not favorable (APY < 35%)
  2. Spreads too wide (> 0.2%)
  3. All symbols already have open positions
  4. Market in low-funding regime
```

### Health Check Commands

```bash
# Check exchange positions
python scripts/check_exchange_positions.py

# View database state
python scripts/check_db.py

# Test connectivity
python -c "from src.adapters.lighter_adapter import LighterAdapter; import asyncio; asyncio.run(LighterAdapter().test_connection())"
```

---

## 💡 Pro Tips

### 1. Capital Allocation
```
Ensure you have enough USDC on BOTH exchanges:
• X10: At least $100-200 available
• Lighter: At least $100-200 + gas in ETH for L2 transactions

Arbitrage only works if both exchanges can execute!
```

### 2. Start Small
```python
# For testing, use smaller position sizes:
DESIRED_NOTIONAL_USD = 60.0   # Minimum reliable size
MAX_OPEN_TRADES = 1           # Single position first
LIGHTER_DRY_RUN = True        # Simulate Lighter trades
```

### 3. Monitor First Hour
```
After starting the bot:
1. Watch for successful opportunity detection
2. Verify first trade executes on BOTH exchanges
3. Check that funding tracking starts
4. Confirm no error loops in logs
```

### 4. Ghost Fills
```
Lighter sometimes reports orders as "cancelled" or "expired" 
even if they partially filled. The bot's "Ghost Guardian" logic
detects these and hedges accordingly.
```

### 5. Optimal Trading Times
```
Funding rates are typically highest when:
• Market is volatile (but not too volatile)
• Open interest is imbalanced
• Near funding payment times (hourly)
```

### 6. JSON Logs for Monitoring
```bash
# Tail structured logs
Get-Content logs/funding_bot_json.jsonl -Wait -Tail 50 | ConvertFrom-Json

# Filter for trades only
Get-Content logs/funding_bot_json.jsonl | ConvertFrom-Json | Where-Object {$_.event -eq "trade_opened"}
```

---

## 📖 Example Trade Flow

```
14:23:15 [INFO] 🔍 Scanning for opportunities...
14:23:16 [INFO] 📊 Found 62 valid pairs, 3 opportunities
14:23:16 [INFO] 🎯 Best: ETH-USD | APY: 67.2% | Spread: 0.12% | Breakeven: 3.2h

14:23:16 [INFO] 🚀 Starting parallel execution for ETH-USD
14:23:17 [INFO] 📤 [Lighter] Sending LONG 0.0375 ETH @ $4001.50 (POST_ONLY)
14:23:17 [INFO] 📤 [X10] Sending SHORT 0.0375 ETH (MARKET/IOC)

14:23:19 [INFO] ✅ [Lighter] Order FILLED @ $4001.25 (0.0375 ETH)
14:23:19 [INFO] ✅ [X10] Order FILLED @ $4000.80 (0.0375 ETH)

14:23:19 [INFO] 🎉 Trade COMPLETE: ETH-USD
    Lighter: LONG  0.0375 ETH @ $4001.25
    X10:     SHORT 0.0375 ETH @ $4000.80
    Size: $150.00 | Expected APY: 67.2%

15:00:01 [INFO] 💰 Funding received: ETH-USD
    Lighter: +$0.0312 (received)
    X10:     -$0.0156 (paid)
    Net:     +$0.0156

16:00:05 [INFO] 💰 Funding received: ETH-USD
    Net cumulative: +$0.0312

... (2 hours later) ...

16:23:45 [INFO] 📊 Exit check: ETH-USD
    Gross PnL:     $0.0624 (funding)
    Price PnL:     $0.0125 (convergence)
    Est. Exit Fee: $0.0340
    Net PnL:       $0.0409 ✅ (above $0.05 threshold)

16:23:46 [INFO] 🔄 Closing ETH-USD...
16:23:48 [INFO] ✅ Both legs closed successfully
16:23:48 [INFO] 💵 Final PnL: $0.0409 (Funding: $0.0624, Price: $0.0125, Fees: -$0.0340)
```

---

## 📜 License

MIT License - see [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

---

## 📞 Support

- Check the [Troubleshooting](#-troubleshooting) section
- Review log files for error details
- Open an issue on GitHub with:
  - Bot version
  - Relevant log snippets
  - Steps to reproduce

---

**⚠️ RISK WARNING**: Cryptocurrency trading involves substantial risk. This bot is provided as-is without warranty. Always test with small amounts first and never trade more than you can afford to lose.
