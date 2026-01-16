# Funding Bot - Repository Index

> **Token-optimized reference** (58K → 3K tokens reduction)
> Generated: 2026-01-15 | 127 source files | 27,102 LOC

---

## Quick Navigation

| Need | Go To |
|------|-------|
| Architecture rules | `PLANNING.md` |
| Current tasks | `TASK.md` |
| Troubleshooting | `KNOWLEDGE.md` |
| This index | `docs/memory/REPO_INDEX.md` |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        APPLICATION LAYER                         │
│  app/run.py → app/supervisor/manager.py (Supervisor)            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         SERVICE LAYER                            │
│  ┌────────────────┐  ┌────────────────┐  ┌─────────────────┐   │
│  │ OpportunityEng │  │ ExecutionEngine│  │ PositionManager │   │
│  │ (scan/filter)  │  │ (2-leg entry)  │  │ (exit/close)    │   │
│  └────────────────┘  └────────────────┘  └─────────────────┘   │
│         │                    │                    │              │
│         └──────────────┬─────┴────────────┬──────┘              │
│                        ▼                  ▼                      │
│              ┌─────────────────┐  ┌───────────────┐             │
│              │ MarketDataSvc   │  │ FundingTracker│             │
│              └─────────────────┘  └───────────────┘             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        ADAPTER LAYER                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │ LighterAdapter   │  │ X10Adapter       │  │ SQLiteStore  │  │
│  │ (Premium)        │  │ (Extended)       │  │ (write-behind)│  │
│  └──────────────────┘  └──────────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         DOMAIN LAYER                             │
│  models.py (Trade, Order, Position, FundingRate, Opportunity)   │
│  rules.py (ExitDecision, RuleResult)                            │
│  errors.py (DomainError hierarchy)                              │
│  events.py (TradeOpened, TradeClosed, etc.)                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Index

### Domain (`domain/`)
Core business logic, SDK-independent.

| File | Purpose | Key Classes |
|------|---------|-------------|
| `models.py` | Canonical models | `Trade`, `Order`, `Position`, `FundingRate`, `Opportunity`, `TradeLeg` |
| `rules.py` | Exit rule evaluation | `ExitDecision`, `RuleResult` (first-hit-wins) |
| `errors.py` | Error hierarchy | `DomainError` → `ExecutionError`, `OrderError`, `RollbackError` |
| `events.py` | Domain events | `TradeOpened`, `TradeClosed`, `LegFilled`, `BrokenHedgeDetected` |
| `professional_exits.py` | Advanced exit strategies | `ZScoreExitStrategy`, `FundingVelocityExitStrategy` |

### Ports (`ports/`)
Abstract interfaces (Hexagonal Architecture).

| File | Purpose | Interface |
|------|---------|-----------|
| `exchange.py` | Exchange operations | `ExchangePort` (initialize, place_order, get_positions, get_funding_rate) |
| `store.py` | Persistence | `TradeStorePort` (save_trade, get_open_trades, save_funding) |
| `event_bus.py` | Event publishing | `EventBusPort` (publish, subscribe) |
| `notification.py` | Alerts | `NotificationPort` (send_alert) |

### Adapters (`adapters/`)
Concrete implementations.

| Path | Purpose | Key Points |
|------|---------|------------|
| `exchanges/lighter/adapter.py:202` | Lighter Premium | `LighterAdapter`, 24K req/min, WebSocket circuit breaker |
| `exchanges/x10/adapter.py:150` | X10/Extended | `X10Adapter`, pagination (100/page), depth=1 for L1 |
| `store/sqlite/store.py:79` | SQLite persistence | `SQLiteTradeStore`, write-behind queue, WAL mode |
| `messaging/telegram.py:45` | Alerts | `TelegramAdapter` |

### Services (`services/`)
Business orchestration.

| File | Purpose | Key Functions |
|------|---------|---------------|
| `execution.py:69` | Trade entry | `ExecutionEngine.execute()` - 2-leg delta-neutral |
| `positions/manager.py:61` | Exit management | `PositionManager` - exit eval, coordinated close |
| `positions/close.py` | Close logic | `_close_both_legs_coordinated`, `_post_close_readback` |
| `opportunities/engine.py:39` | Opportunity scan | `OpportunityEngine.scan()`, `get_best_opportunity()` |
| `market_data/service.py:64` | Market data | `MarketDataService` - parallel refresh (20 symbols) |
| `funding.py:26` | Funding tracking | `FundingTracker` - rate collection, history |
| `liquidity_gates.py` | Pre-trade checks | L1 depth gates, VWAP impact estimation |
| `metrics/collector.py:58` | Performance metrics | `MetricsCollector` - latency, rate limits |

### Application (`app/`)
Entry points and orchestration.

| File | Purpose |
|------|---------|
| `run.py` | Main entry point |
| `supervisor/manager.py:47` | `Supervisor` - main loop orchestration |
| `supervisor/loops.py` | Background tasks (refresh, exit eval) |
| `run_safety.py` | Safety checks before run |

### Config (`config/`)
Settings and configuration.

| File | Key Classes |
|------|-------------|
| `settings.py:531` | `Settings` (pydantic) - all config keys |
| `settings.py:24` | `ExchangeSettings` - per-exchange config |
| `settings.py:272` | `ExecutionSettings` - maker timeout, IOC retries |

---

## Critical Code Paths

### Entry Flow
```
Supervisor.run_loop()
  → OpportunityEngine.scan()
    → evaluator._evaluate_symbol()
    → liquidity_gates.check_l1_depth()
  → ExecutionEngine.execute(opportunity)
    → execution_leg1._execute_leg1() [Lighter maker]
    → execution_flow._execute_leg2() [X10 taker]
    → store.save_trade()
```

### Exit Flow
```
Supervisor.run_loop()
  → PositionManager.evaluate_exits()
    → exit_eval._evaluate_exit() [first-hit-wins]
    → rules.py: Layer1 → Layer2 → Layer3
  → PositionManager.close_trade()
    → close._close_both_legs_coordinated()
    → close._post_close_readback() [VWAP correction]
    → store.update_trade()
```

### PnL Calculation
```
pnl._calculate_realized_pnl()
  → VWAP: sum(delta_qty * fill_price) / sum(delta_qty)
  → price_pnl + funding_collected + accrued_funding
  → Readback correction if diff > 3bps or $0.30
```

---

## Key Constants

| Constant | Value | Location |
|----------|-------|----------|
| `funding_rate_interval_hours` | `1` (HOURLY) | settings.py |
| Lighter Premium rate limit | 24,000/min | adapter.py |
| X10 pagination limit | 100/page | adapter.py |
| Connection pool | 200 total, 100/host | settings.py |
| DNS cache TTL | 30 min | settings.py |
| Market cache TTL | 1 hour | settings.py |
| Parallel market refresh | 20 concurrent | refresh.py |
| Parallel exit eval | 10 concurrent | manager.py |
| Z-score lookback | 168 hours (7d) | manager.py |
| Funding velocity lookback | 6 hours | professional_exits.py |

---

## Exit Rule Priority (First-Hit-Wins)

```
Layer 1 (Emergency):
  - BrokenHedge: Missing leg detected
  - LiquidationImminent: Health distance < threshold

Layer 2 (Economic):
  - NetEVNegative: Expected value turned negative
  - OpportunityCost: Better opportunity available
  - FundingFlip: Funding direction reversed

Layer 3 (Optional):
  - TakeProfit: Target PnL reached
  - VelocityExit: Funding velocity declining
  - ZScoreExit: Statistical mean reversion
```

---

## Test Structure

```
tests/
├── unit/                    # 276 tests, offline
│   ├── adapters/            # Adapter mocks
│   ├── services/            # Service logic
│   │   ├── execution/       # Entry tests
│   │   └── positions/       # Exit/PnL tests
│   └── domain/              # Model tests
├── integration/             # Requires API keys
└── verification/            # E2E scenarios
```

Run: `pytest tests/unit/ -q` (no SDK needed)

---

## Common Patterns

### Import Guard (SDK optional)
```python
try:
    from lighter import LighterClient
    LIGHTER_SDK_AVAILABLE = True
except ImportError:
    LIGHTER_SDK_AVAILABLE = False
    LighterClient = None
```

### Funding Rate (DECIMAL, no /100!)
```python
# ✅ CORRECT
rate_hourly = Decimal(str(rate_val))

# ❌ WRONG
rate_hourly = Decimal(str(rate_val)) / Decimal("100")
```

### account_index Semantics
```python
# 0 ≠ None!
account_index = 0    # Explicitly use account 0
account_index = None # Use adapter default
```

---

## File Size Reference

| Category | Files | Lines |
|----------|-------|-------|
| Source | 127 | 27,102 |
| Tests | 96 | ~15,000 |
| Total | 223 | ~42,000 |

Top files by complexity:
1. `positions/close.py` - ~2,000 LOC (close orchestration)
2. `adapters/exchanges/lighter/adapter.py` - ~1,200 LOC
3. `adapters/exchanges/x10/adapter.py` - ~900 LOC
4. `domain/rules.py` - ~800 LOC (exit rules)
5. `services/execution.py` - ~700 LOC

---

## Quick Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| All opportunities rejected | `hedge_depth_preflight_multiplier` | Reduce to 2.0 |
| Missing funding history | X10 pagination | Implement cursor loop |
| WebSocket disconnects | Message rate | <200 msg/min |
| Ghost positions | Shutdown flush | Wait for `store.close()` |
| APY 100x wrong | `/100` division bug | Remove division |
| Tests need SDK | Import guard | Use try/except pattern |

---

## Cross-References

- **Exit Rules**: `domain/rules.py` → `services/positions/exit_eval.py`
- **PnL Calc**: `services/positions/pnl.py` → `domain/models.py:Trade.calculate_pnl()`
- **Market Data**: `services/market_data/service.py` → both adapters
- **Config**: `config/settings.py` → all services
- **Events**: `domain/events.py` → `adapters/messaging/event_bus.py`

---

*This index enables 94% token reduction when navigating the codebase.*
