# Symbol Quick Reference

> Fast lookup for classes, functions, and constants.
> Use with `Ctrl+F` for instant navigation.

---

## Classes by Layer

### Domain Layer
| Class | File:Line | Purpose |
|-------|-----------|---------|
| `Exchange` | `domain/models.py:22` | Enum: LIGHTER, X10 |
| `Side` | `domain/models.py:29` | Enum: BUY, SELL |
| `OrderType` | `domain/models.py:50` | Enum: LIMIT, MARKET |
| `TimeInForce` | `domain/models.py:57` | Enum: GTC, IOC, POST_ONLY, FOK |
| `OrderStatus` | `domain/models.py:66` | Order lifecycle |
| `TradeStatus` | `domain/models.py:95` | Trade lifecycle: PENDING→OPEN→CLOSING→CLOSED |
| `ExecutionState` | `domain/models.py:108` | Granular execution FSM |
| `FundingRate` | `domain/models.py:140` | Funding snapshot (hourly decimal) |
| `MarketInfo` | `domain/models.py:176` | Market metadata |
| `Balance` | `domain/models.py:206` | Account balance |
| `Position` | `domain/models.py:217` | Exchange position |
| `OrderRequest` | `domain/models.py:250` | Order parameters |
| `Order` | `domain/models.py:269` | Order with fills |
| `TradeLeg` | `domain/models.py:320` | Single leg of arb trade |
| `Trade` | `domain/models.py:361` | Full arb trade (2 legs) |
| `Opportunity` | `domain/models.py:499` | Detected opportunity |
| `ExecutionAttempt` | `domain/models.py:544` | Attempt tracking |
| `RuleResult` | `domain/rules.py:39` | Exit rule evaluation |
| `ExitDecision` | `domain/rules.py:799` | Exit decision container |
| `ExitSignal` | `domain/professional_exits.py:25` | Professional exit signal |
| `ZScoreExitStrategy` | `domain/professional_exits.py:39` | Z-score mean reversion |
| `FundingVelocityExitStrategy` | `domain/professional_exits.py:259` | Velocity-based exit |

### Domain Errors
| Error | File:Line | When |
|-------|-----------|------|
| `DomainError` | `domain/errors.py:13` | Base error |
| `ValidationError` | `domain/errors.py:52` | Input validation |
| `InsufficientBalanceError` | `domain/errors.py:81` | Not enough funds |
| `OrderError` | `domain/errors.py:118` | Order operations |
| `OrderRejectedError` | `domain/errors.py:124` | Exchange rejected |
| `OrderTimeoutError` | `domain/errors.py:130` | Fill timeout |
| `ExecutionError` | `domain/errors.py:153` | Execution failures |
| `Leg1FailedError` | `domain/errors.py:159` | Maker leg failed |
| `Leg2FailedError` | `domain/errors.py:165` | Hedge leg failed |
| `RollbackError` | `domain/errors.py:188` | Rollback failures |
| `ReconciliationError` | `domain/errors.py:211` | Position mismatch |
| `ExchangeError` | `domain/errors.py:234` | Exchange issues |
| `RateLimitError` | `domain/errors.py:240` | Rate limited |

### Domain Events
| Event | File:Line | Trigger |
|-------|-----------|---------|
| `DomainEvent` | `domain/events.py:28` | Base event |
| `TradeOpened` | `domain/events.py:40` | Trade entry complete |
| `TradeClosed` | `domain/events.py:55` | Trade exit complete |
| `TradeStateChanged` | `domain/events.py:68` | Status transition |
| `LegFilled` | `domain/events.py:80` | Leg fill |
| `RollbackInitiated` | `domain/events.py:94` | Rollback started |
| `FundingCollected` | `domain/events.py:115` | Funding payment |
| `BrokenHedgeDetected` | `domain/events.py:145` | Missing leg |
| `CircuitBreakerTripped` | `domain/events.py:136` | WS circuit break |

### Ports (Interfaces)
| Interface | File:Line | Methods |
|-----------|-----------|---------|
| `ExchangePort` | `ports/exchange.py:26` | initialize, close, load_markets, place_order, get_positions |
| `TradeStorePort` | `ports/store.py:19` | save_trade, get_open_trades, update_trade |
| `EventBusPort` | `ports/event_bus.py:18` | publish, subscribe |
| `NotificationPort` | `ports/notification.py:12` | send_alert |

### Adapters
| Class | File:Line | Exchange |
|-------|-----------|----------|
| `LighterAdapter` | `adapters/exchanges/lighter/adapter.py:202` | Lighter Premium |
| `X10Adapter` | `adapters/exchanges/x10/adapter.py:150` | X10/Extended |
| `SQLiteTradeStore` | `adapters/store/sqlite/store.py:79` | SQLite + write-behind |
| `TelegramAdapter` | `adapters/messaging/telegram.py:45` | Notifications |
| `InMemoryEventBus` | `adapters/messaging/event_bus.py:26` | Event pub/sub |
| `WsClient` | `adapters/exchanges/lighter/ws_client.py:35` | Lighter WebSocket |
| `LocalOrderbook` | `adapters/exchanges/lighter/orderbook.py:25` | Local OB cache |
| `_WebSocketCircuitBreaker` | `adapters/exchanges/lighter/adapter.py:71` | WS protection |

### Services
| Class | File:Line | Purpose |
|-------|-----------|---------|
| `ExecutionEngine` | `services/execution.py:69` | 2-leg trade entry |
| `PositionManager` | `services/positions/manager.py:61` | Exit management |
| `OpportunityEngine` | `services/opportunities/engine.py:39` | Opportunity scanning |
| `MarketDataService` | `services/market_data/service.py:64` | Market data aggregation |
| `FundingTracker` | `services/funding.py:26` | Funding rate tracking |
| `MetricsCollector` | `services/metrics/collector.py:58` | Performance metrics |
| `ShutdownService` | `services/shutdown.py:41` | Graceful shutdown |
| `Reconciler` | `services/reconcile.py:59` | Position reconciliation |
| `NotificationService` | `services/notification.py:28` | Alert management |
| `HistoricalIngestionService` | `services/historical/ingestion.py:55` | Historical data |

### Application
| Class | File:Line | Purpose |
|-------|-----------|---------|
| `Supervisor` | `app/supervisor/manager.py:47` | Main orchestration |
| `RunSafetyDecision` | `app/run_safety.py:24` | Pre-run checks |

### Config
| Class | File:Line | Purpose |
|-------|-----------|---------|
| `Settings` | `config/settings.py:531` | Root config |
| `ExchangeSettings` | `config/settings.py:24` | Per-exchange |
| `TradingSettings` | `config/settings.py:95` | Trading params |
| `ExecutionSettings` | `config/settings.py:272` | Execution params |
| `RiskSettings` | `config/settings.py:384` | Risk limits |
| `DatabaseSettings` | `config/settings.py:458` | DB config |

---

## Key Functions

### Execution
| Function | File | Purpose |
|----------|------|---------|
| `_execute_impl` | `services/execution_flow.py` | Main execution |
| `_execute_leg1` | `services/execution_leg1.py` | Maker leg |
| `_execute_leg2` | `services/execution_flow.py` | Taker hedge |
| `_rollback` | `services/execution_flow.py` | Rollback on failure |
| `_wait_for_fill` | `services/execution_orders.py` | Fill polling |

### Position Management
| Function | File | Purpose |
|----------|------|---------|
| `_evaluate_exit` | `services/positions/exit_eval.py` | Exit rule check |
| `_close_both_legs_coordinated` | `services/positions/close.py` | Coordinated close |
| `_post_close_readback` | `services/positions/close.py` | VWAP correction |
| `_calculate_realized_pnl` | `services/positions/pnl.py` | PnL calculation |
| `_check_position_imbalance` | `services/positions/imbalance.py` | Delta check |
| `_handle_broken_hedge_if_needed` | `services/positions/broken_hedge.py` | Emergency close |

### Opportunity Scanning
| Function | File | Purpose |
|----------|------|---------|
| `scan` | `services/opportunities/scanner.py` | Market scan |
| `get_best_opportunity` | `services/opportunities/scanner.py` | Best candidate |
| `_evaluate_symbol` | `services/opportunities/evaluator.py` | Symbol evaluation |
| `_apply_filters` | `services/opportunities/filters.py` | Filter pipeline |
| `_calculate_expected_value` | `services/opportunities/scoring.py` | EV calculation |

### Market Data
| Function | File | Purpose |
|----------|------|---------|
| `_refresh_symbols_parallel` | `services/market_data/refresh.py` | Parallel refresh |
| `get_orderbook_l1` | `services/market_data/service.py` | L1 quotes |
| `get_funding_rate` | `services/market_data/service.py` | Current funding |

### Liquidity Gates
| Function | File | Purpose |
|----------|------|---------|
| `check_l1_depth` | `services/liquidity_gates_l1.py` | L1 depth check |
| `calculate_vwap_impact` | `services/liquidity_gates_impact_core.py` | Slippage estimate |

---

## Config Keys (Quick Reference)

| Key | Type | Default | Location |
|-----|------|---------|----------|
| `live_trading` | bool | `false` | TradingSettings |
| `funding_rate_interval_hours` | int | `1` | ExchangeSettings |
| `maker_timeout_seconds` | float | `5.0` | ExecutionSettings |
| `ioc_retry_attempts` | int | `3` | ExecutionSettings |
| `hedge_depth_preflight_multiplier` | Decimal | `2.0` | ExecutionSettings |
| `min_apy_threshold_bps` | int | `100` | TradingSettings |
| `max_notional_per_trade_usd` | Decimal | `5000` | TradingSettings |
| `write_batch_size` | int | `50` | DatabaseSettings |
| `connection_pool_size` | int | `200` | ExchangeSettings |

---

## Enum Values

### TradeStatus Flow
```
PENDING → OPENING → OPEN → CLOSING → CLOSED
                      ↓
                   FAILED / ROLLBACK
```

### ExecutionState FSM
```
PENDING → LEG1_SUBMITTED → LEG1_FILLED → LEG2_SUBMITTED → COMPLETE
              ↓                                              ↓
         ABORTED ←──────────── ROLLBACK_* ←───────────── FAILED
```

### Exit Rule Layers
```
Layer 1: BROKEN_HEDGE, LIQUIDATION_IMMINENT
Layer 2: NET_EV_NEGATIVE, OPPORTUNITY_COST, FUNDING_FLIP
Layer 3: TAKE_PROFIT, VELOCITY_EXIT, Z_SCORE_EXIT
```

---

*Use `Ctrl+F` with class/function names for instant navigation.*
