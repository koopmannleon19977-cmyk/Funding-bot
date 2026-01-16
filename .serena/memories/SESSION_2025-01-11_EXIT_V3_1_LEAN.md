# Funding Bot - Session Context: Exit v3.1 Lean Refactoring

**Date:** 2025-01-11
**Branch:** `exit-v3.1-lean`
**Status:** ✅ COMPLETED - Ready for merge to main

---

## Project Overview

**Funding Arbitrage Bot** - A market-neutral trading bot that captures funding rate differences between Lighter and X10 exchanges.

### Architecture
- **Language:** Python 3.11+
- **Framework:** Custom with hexagonal architecture (ports/adapters)
- **Key Exchanges:** Lighter (lead/maker), X10 (hedge/taker)
- **Database:** SQLite with connection pooling
- **Configuration:** Unified YAML config

### Key Directories
- `src/funding_bot/domain/` - Business logic (models, rules, professional exits)
- `src/funding_bot/services/` - Business services (execution, positions, opportunities, market data)
- `src/funding_bot/adapters/` - Exchange and storage adapters
- `src/funding_bot/ports/` - Interface definitions
- `docs/` - Documentation

---

## Exit Strategy v3.1 Lean Refactoring - Summary

### What Changed

| Category | Before | After |
|----------|--------|-------|
| **Strategies** | 15 (redundant) | 7 (layered) |
| **Architecture** | Flat list | 3-Layer Priority System |
| **APY Crash Exit** | Fixed % threshold | Removed (replaced by Velocity) |
| **Trailing Stop** | Static 20% | ATR-based (volatility-adaptive) |

### New Strategies

1. **Funding Velocity Exit** ([professional_exits.py:259](src/funding_bot/domain/professional_exits.py#L259))
   - Proactive crash detection via linear regression
   - Measures rate of change (velocity) and acceleration
   - Config: `velocity_threshold_hourly: -0.0015`

2. **ATR Trailing Stop** ([professional_exits.py:131](src/funding_bot/domain/professional_exits.py#L131))
   - Volatility-adaptive profit protection
   - Uses 14-period ATR as volatility proxy
   - Config: `atr_multiplier: 2.0`, `atr_min_activation_usd: 3.0`

3. **Emergency Layer** ([rules.py:529-665](src/funding_bot/domain/rules.py#L529))
   - Liquidation Distance Monitor (pending adapter)
   - Delta Bound Violation (3% threshold)
   - Catastrophic Funding Flip (-200% APY)

### Updated Thresholds

| Parameter | Before | After |
|-----------|--------|-------|
| `early_take_profit_net_usd` | $1.50 | **$4.00** |
| `exit_ev_exit_cost_multiple` | 1.2 | **1.4** |
| `early_edge_exit_min_age_seconds` | 3600 | **7200 (2h)** |

---

## Exit Architecture (3-Layer)

### Layer 1: Emergency Exits (Priority 1 - Override min_hold)
1. Liquidation Distance Monitor (< 15% buffer)
2. Delta Bound Violation (> 3% delta)
3. Catastrophic Funding Flip (< -200% APY)
4. Early Take Profit ($4+ Price-PnL)
5. Early Edge Exit (Funding Flip after 2h)

### Gate 1: Min Hold Time (48h)

### Layer 2: Economic Exits (Priority 2 - Protection)
1. Max Hold Time (240h)
2. Netto EV Exit (1.4× cost multiple)
3. Yield vs Cost Exit (>24h to cover)
4. Basis Convergence Exit (spread → 0)

### Layer 3: Optimization Exits (Priority 3 - Maximize)
1. Funding Velocity Exit (proactive)
2. ATR Trailing Stop (volatility-adapt)
3. Z-Score Exit (7+ days fallback)
4. Profit Target ($1.50)
5. Kelly Rotation (opportunity cost)

---

## Key Files Modified

### Domain Layer
- [rules.py](src/funding_bot/domain/rules.py) - Main `evaluate_exit_rules()` function, reason constants
- [professional_exits.py](src/funding_bot/domain/professional_exits.py) - ATR, Velocity, Z-Score strategies

### Services
- [exit_eval.py](src/funding_bot/services/positions/exit_eval.py) - Calls evaluate_exit_rules()
- [close.py](src/funding_bot/services/positions/close.py) - Close execution with cumulative fills

### Config
- [config.yaml](src/funding_bot/config/config.yaml) - All thresholds and settings

### Documentation
- [EXIT_STRATEGIES_COMPLETE_GUIDE.md](docs/EXIT_STRATEGIES_COMPLETE_GUIDE.md) - Complete reference
- [EXIT_STRATEGY_OVERHAUL_PLAN.md](docs/EXIT_STRATEGY_OVERHAUL_PLAN.md) - Release notes

---

## Test Status: 50/50 Passing ✅

All verification tests passing:
- `test_exit_rules_all_current_settings.py`
- `test_exit_early_take_profit.py`
- `test_exit_net_ev.py`
- `test_exit_opportunity_cost_ev.py`
- `test_exit_early_edge_exit.py`
- `test_close_with_current_settings.py`

---

## Known Limitations

1. **Liquidation Distance Monitor** - Requires adapter extension for `liquidation_price` field
2. **ATR Calculation** - Uses PnL history as proxy (sufficient for funding arb)
3. **Funding Velocity** - Depends on historical APY data availability

---

## Next Steps

1. **Deployment:** Ready to merge `exit-v3.1-lean` → `main`
2. **Monitoring:** Track Win Rate, False Positives, Avg Hold Duration for 30 days
3. **v3.2 Planning:** Optional enhancements (EWMA Z-Score, Multi-Asset correlation)

---

## Reason Code Constants

```python
REASON_EMERGENCY = "EMERGENCY"       # Liq, Delta, Catastrophic Flip
REASON_EARLY_TP = "EARLY_TAKE_PROFIT"
REASON_EARLY_EDGE = "EARLY_EDGE"
REASON_NETEV = "NetEV"
REASON_YIELD_COST = "YIELD_COST"
REASON_BASIS = "BASIS"
REASON_VELOCITY = "VELOCITY"
REASON_Z_SCORE = "Z-SCORE"
```

---

## Config Snippet (Key Settings)

```yaml
trading:
  # Time Gates
  min_hold_seconds: 172800  # 48h
  max_hold_hours: "240.0"   # 10 days

  # Early TP
  early_take_profit_enabled: true
  early_take_profit_net_usd: "4.00"
  early_take_profit_slippage_multiple: "2.5"

  # Net EV
  exit_ev_enabled: true
  exit_ev_horizon_hours: "12.0"
  exit_ev_exit_cost_multiple: "1.4"

  # ATR Trailing
  atr_trailing_enabled: true
  atr_period: 14
  atr_multiplier: "2.0"

  # Funding Velocity
  funding_velocity_exit_enabled: true
  velocity_threshold_hourly: "-0.0015"
```

---

**Session Status:** Context loaded successfully. Ready for development tasks.
