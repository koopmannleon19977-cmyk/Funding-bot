# PLANNING.md - Architecture & Design Principles

**This document is read automatically by Claude Code at session start.**

> **Purpose**: Architecture, design principles, absolute rules, and invariant guarantees.
> **When to Read**: Session start, before implementation.
> **When to Update**: Architecture changes, new absolute rules, design decisions.

---

## Project Overview

This is a **production-grade funding arbitrage bot** (Lighter Premium + X10/Extended exchanges).

**Core Philosophy**: Prioritize correctness (PnL/accounting), safety (risk controls), and cost-aware profit.

---

## Absolute Rules (Non-Negotiables)

### 1. Security & Secrets
- **NEVER** commit or log secrets (API keys, private keys, seeds, mnemonics, session tokens)
- No live trading during development unless explicitly enabled via dedicated flag
- All secrets must be environment variables or secure config

### 2. Exit Rule Semantics
- **Preserve first-hit-wins semantics** in exit rules: exactly one decision per evaluation tick
- Exit rule priority must be strictly maintained (Layer 1 → Layer 2 → Layer 3)

### 3. Exchange Field Semantics
- Treat exchange order fields (`filled_qty`, `fee`) as **cumulative** unless proven otherwise
- Always compute realized exit price using VWAP:
  ```
  exit_vwap = sum(delta_qty * fill_price) / sum(delta_qty)
  ```

### 4. Change Requirements
- Any change touching **execution, PnL, accounting, close logic, or adapters** MUST include:
  - Tests OR
  - Written test plan

### 5. Testing Requirements
- Default `pytest -q` must run **offline** (no DNS, no exchange SDK required)
- Unit tests must pass without exchange SDKs installed (use mocks)
- External/API tests must be marked `integration` and skipped unless env vars present

---

## Architecture Principles

### Safety Modes (dev/paper/prod)

Default behavior:
- Paper/sim is the default unless `live_trading=true`
- Execution must support "dry-run" path (compute decisions + costs) without placing orders

Any PR touching order placement must ensure:
- `close_positions_on_exit` and any live flags are respected
- "emergency close" behavior is explicit and tested

### PnL & Accounting Rules

#### Definitions
- `price_pnl`: Realizable PnL based on bid/ask or depth-walk, minus execution fees
- `funding_collected`: Realized funding payments from exchange APIs (pure funding)
- `accrued_funding`: Forecast estimate (not included in exchange realized)
- `total_pnl`: `price_pnl + funding_collected + accrued_fnl`

#### Fill Aggregation Rules
1. Always compute realized exit price using VWAP
2. If exchange reports cumulative fields, compute deltas per order_id before adding totals
3. Post-close **readback** is source of truth if discrepancy exceeds tolerance:
   - VWAP diff > 3 bps OR net_pnl diff > $0.30 → warn + correct

#### Funding Rate Cadence (HOURLY)
- **Both exchanges apply funding HOURLY** (every hour on the hour)
- The `/8` in formulas is for **realization/normalization**, NOT payment cadence
- **Always keep `funding_rate_interval_hours = 1`** (hourly, no division)
- Setting `interval_hours = 8` causes **8x EV/APY errors**

#### account_index Semantics (Lighter adapter)
- `account_index = 0`: Explicitly use account 0 (default/main trading account)
- `account_index = None`: Use adapter's default behavior (may be 0 or system-selected)
- **Never** treat `0` and `None` as the same - they have different semantic meanings

### Close Engine Rules (Execution Correctness)

1. **Idempotency**: If `trade.status == CLOSING` and close_reason exists:
   - Never place new orders
   - Verify positions
   - Perform post-close readback
   - Finalize

2. **Coordinated Close**: Maker-first on both legs, then synchronized IOC (non-emergency)
3. **Emergency Closes**: May bypass min-hold, must prioritize risk reduction
4. **REBALANCE reasons**: Must trigger rebalance path (no full close)
5. **Unhedged Window**: Never leave unhedged longer than configured maker timeout

### Exit Rules Ruleset (Strategy Correctness)

- Maintain strict priority ordering (first-hit-wins)
- Min-hold blocks only non-emergency exits
- All new exit signals must have stable reason codes and be logged

**Priority Stack:**
```
Layer 1 (Emergency): Broken Hedge, Liquidation Imminent
Layer 2 (Economic): NetEV Negative, Opportunity Cost, Funding Flip
Layer 3 (Optional): Take Profit, Velocity Exit, Z-Score Exit
```

### Config & Migration Policy

1. Config keys are part of the **public contract**
2. Do not silently change semantics of existing keys
3. Any new key must:
   - Be added to settings parser with defaults
   - Be documented in docs
   - Appear in `test_exit_rules_all_current_settings.py`
4. If removing keys:
   - Document migration
   - Remove from parser and tests deliberately
   - Keep backwards compatibility only if explicitly required

---

## Invariant Guarantees

### Thread Safety
- Close operations are thread-safe per symbol (symbol-specific locks)
- Market data reads are lock-free (immutable caches)

### Data Consistency
- Trade status transitions are atomic (OPEN → CLOSING → CLOSED)
- PnL snapshots are written atomically to database
- Funding events are never double-counted

### Performance Invariants
- Market data refresh: ≤ 500ms for 10 symbols (Premium-optimized)
- Exit evaluation: ≤ 150ms for 10 positions (Premium-optimized)
- DB batch writes: ≤ 10ms for 100 trades (executemany)

---

## Technology Stack

### Core
- **Python 3.14+** with async/await
- **aiohttp** for HTTP (Premium-optimized connection pooling)
- **aiosqlite** for database (WAL mode, write-behind)

### Exchange SDKs
- **Lighter SDK**: `https://github.com/elliottech/lighter-python`
- **X10 SDK**: `https://github.com/x10xchange/python_sdk/tree/starknet`

### Testing
- **pytest** with async support
- **pytest-aiohttp** for mocking
- Coverage reporting enabled

---

## Design Patterns Used

### Write-Behind Pattern (Database)
- In-memory cache for fast reads
- Async write queue with batching
- Flush on shutdown (critical for ghost position prevention)

### Circuit Breaker Pattern (WebSocket)
- Stops reconnection after repeated failures
- Automatic recovery after cooldown
- Per-connection state tracking

### Semaphore-Controlled Concurrency (Premium)
- Market data refresh: 20 concurrent symbols
- Exit evaluation: 10 concurrent positions
- Rate limit compliance with headroom

---

## Performance Targets (Premium-Optimized)

| Operation | Target | Optimization |
|-----------|--------|--------------|
| Market Data Refresh (10 symbols) | ≤ 500ms | Parallel (20 concurrent) |
| Exit Evaluation (10 positions) | ≤ 150ms | Parallel (10 concurrent) |
| DB Batch Write (100 trades) | ≤ 10ms | executemany batching |
| Connection Pool Size | 200 total | 100 per-host |
| DNS Cache TTL | 30 minutes | 6x improvement |
| Keep-Alive Timeout | 5 minutes | 10x improvement |
| Market Cache TTL | 1 hour | Auto-refresh |

---

## Risk Controls

### Position Level
- Max position size per symbol
- Liquidation distance monitoring
- Delta imbalance detection

### Portfolio Level
- Max total notional exposure
- Max concurrent positions
- Emergency close on exit

### Exchange Level
- Rate limit proximity alerts (80% threshold)
- Broken hedge detection (missing leg)
- Order failure tracking

---

## Monitoring & Observability

### Guaranteed Per-Trade Metrics (logged every 60s)
- PnL: price_pnl, funding_collected, accrued_funding, total_pnl
- Risk: liquidation_distance, delta_imbalance, health_distance
- Position: suggested_notional, exit_cost_estimate, entry_age_hours

### Guaranteed Exit Event Logs
- Exit: reason, layer, final_pnl, hold_duration_hours
- Execution: leg_vwaps, leg_fees, post_close_correction

### Historical Data Requirements
- **Funding Velocity Exit**: 6 hours of hourly net APY
- **Z-Score Exit**: 168 hours (7 days) of hourly net APY
- **ATR Trailing Stop**: Requires funding volatility (not yet implemented)

---

## Upgrade Path

### From Standard to Premium
1. Update connection pool settings (adapter `__init__`)
2. Enable parallel market data refresh
3. Enable parallel exit evaluation
4. Update database batch size
5. Monitor rate limit proximity

### Breaking Changes
- Always document in CLAUDE.md
- Add migration notes to this file
- Update tests to reflect new behavior

---

## See Also

- **KNOWLEDGE.md**: Installation, dependencies, troubleshooting
- **TASK.md**: Current tasks, priorities, backlog
- **CONTRIBUTING.md**: Workflow guidelines, PR process
