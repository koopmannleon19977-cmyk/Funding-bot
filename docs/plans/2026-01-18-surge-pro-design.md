# Surge Pro (Extended/X10) Design

**Goal:** Add a Surge Pro engine for Extended (X10) that runs alongside the existing
funding-arb strategy, focused on high-volume taker trading with tight risk controls.

**Context:** The current `funding_bot` runtime is designed for delta-neutral trades
with two legs (Lighter + X10). Surge Pro is single-exchange, directional, taker-only,
and should be isolated from the funding-arb trade model and execution flow.

## Strategy Requirements (Summary)

- Orderbook imbalance taker strategy for points generation.
- Lower frequency than Orderbook Surge; stricter entry thresholds.
- Single position per symbol, no pyramiding or scaling.
- Disciplined exits via stop-loss, take-profit, and signal flip.
- Focus on high daily volume while limiting drawdowns.

## Architecture

Add a new strategy stack that is separate from the funding-arb domain model:

- New domain model for single-leg trades (`SurgeTrade`) with its own persistence.
- New engine/service `services/surge_pro` to handle signals, orders, and lifecycle.
- New SQLite table `surge_trades` for state, metrics, and auditability.
- Strategy loop in the Supervisor to run independently from funding-arb loops.

This keeps delta-neutral logic intact and avoids overloading the existing `Trade`
and execution flows with incompatible assumptions.

## Components

- **Signal Engine:** Computes imbalance from X10 orderbook depth.
- **Symbol Selector:** Chooses top N symbols by L1 notional each interval.
- **Execution:** Places taker orders, handles partial fills, records fees.
- **Risk Guards:** Max positions, per-trade notional, daily loss cap, cooldowns.
- **Store:** Persist open/closed trades and lifecycle metadata.
- **Notifications:** Entry, exit, stop, and circuit-breaker alerts.

## Data Flow

1. Fetch L1 for all symbols -> pick top 10 by notional.
2. For each symbol, fetch orderbook depth (X10 adapter).
3. Compute imbalance signal and validate entry guards.
4. If signal passes, place taker order and record trade.
5. Track fills -> update trade state.
6. Monitor exit rules (stop/take/time/signal flip).
7. Close trade via taker order and finalize metrics.

## Execution Details

- **Orders:** Taker only (market or aggressive limit).
- **Positioning:** One open trade per symbol; no scaling.
- **Partial fills:** Accept filled size and proceed without re-entries.
- **Cooldown:** Minimum time between trades per symbol.

## Risk Controls

- `max_open_trades`, `max_trade_notional_usd`
- `daily_loss_cap_usd`
- `max_spread_bps`, `min_depth_usd`
- `max_trades_per_hour`
- Circuit breaker on repeated API/order failures

## Configuration Additions

Extend `config.yaml` and settings model:

- `strategies.enable_surge_pro`
- `strategies.surge_pro_exchanges`
- `surge_pro.symbol_limit` (default 10)
- `surge_pro.entry_imbalance_threshold`
- `surge_pro.exit_imbalance_threshold`
- `surge_pro.stop_loss_bps`
- `surge_pro.take_profit_bps`
- `surge_pro.max_open_trades`
- `surge_pro.max_trade_notional_usd`
- `surge_pro.daily_loss_cap_usd`
- `surge_pro.cooldown_seconds`
- `surge_pro.max_trades_per_hour`
- `surge_pro.max_spread_bps`
- `surge_pro.min_depth_usd`
- `surge_pro.paper_mode` (optional)

## Storage Schema

New SQLite table `surge_trades` (summary):

- `trade_id`, `symbol`, `exchange`, `side`, `status`
- `qty`, `entry_price`, `exit_price`, `fees`
- `realized_pnl`, `unrealized_pnl`
- `entry_reason`, `exit_reason`
- `created_at`, `opened_at`, `closed_at`

## Observability

- Structured logs for entries/exits and guard failures.
- Metrics: trades/hour, volume, fees, realized pnl.
- Notifications for critical events and halts.

## Testing

- Unit tests for imbalance calculation and guards.
- Store tests for `surge_trades` persistence.
- Integration tests with mocked exchange adapter.
- Optional paper-mode smoke test (no live orders).

## Rollout

- Start disabled by default, enable with small notional cap.
- Validate logs and trade flow in paper-mode or small live size.
- Gradually raise `max_trade_notional_usd` and `max_open_trades`.
