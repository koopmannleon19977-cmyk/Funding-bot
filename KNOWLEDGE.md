# KNOWLEDGE.md - Insights & Best Practices

**This document is read automatically by Claude Code at session start.**

> **Purpose**: Accumulated insights, best practices, troubleshooting, and technical knowledge.
> **When to Read**: When encountering issues, learning patterns, onboarding.
> **When to Update**: After fixing bugs, learning new patterns, discovering edge cases.

---

## Installation & Dependencies

### Recommended Installation

```bash
python -m venv venv
venv\Scripts\activate
python -m pip install -U pip
pip install -e ".[dev]"
```

**Critical**: Use `pip install -e ".[dev]"` (NOT `pip install -r requirements-exchange.txt`)

**Why**:
- `pyproject.toml` includes all core dependencies including `websockets>=12.0`
- `requirements-exchange.txt` is ONLY for Exchange SDK development/testing
- Installing via `pyproject.toml` ensures correct versions across all environments

### websockets Compatibility

- **Required**: `websockets>=12.0` for async WebSocket (primary mode)
- **Sync client**: Requires `websockets>=12.0` (gracefully degrades on older versions)
- The `ws_client` module has an import guard - it will NOT crash on import if `websockets.sync` is missing
- If sync client is used with `websockets<12.0`, a clear `RuntimeError` is raised at runtime

### Project Structure

```
funding-bot/
├── src/funding_bot/
│   ├── adapters/          # Exchange adapters, store implementations
│   │   ├── exchanges/     # Lighter, X10 adapters
│   │   ├── store/         # SQLite store with write-behind
│   │   └── messaging/     # Telegram notifications
│   ├── config/            # Settings, config loader
│   ├── domain/            # Domain models, errors, rules
│   ├── observability/     # Logging, metrics
│   ├── ports/             # Port definitions (interfaces)
│   ├── services/          # Business logic
│   │   ├── execution/     # Order execution
│   │   ├── historical/    # Historical data
│   │   ├── market_data/   # Market data service
│   │   ├── opportunities/ # Opportunity scanner
│   │   └── positions/     # Position management
│   └── utils/             # Utilities (decimals, JSON)
├── tests/
│   ├── unit/              # Offline unit tests
│   ├── integration/       # Integration tests (require env vars)
│   └── verification/      # End-to-end scenario tests
├── docs/                  # Documentation
├── scripts/               # Utility scripts
├── pyproject.toml         # Project config
├── CLAUDE.md              # Project instructions (this file split)
├── PLANNING.md            # Architecture & design
├── TASK.md                # Current tasks & backlog
├── CONTRIBUTING.md        # Workflow & PR guidelines
└── README.md              # Project overview
```

---

## Exchange-Specific Knowledge

### Lighter Exchange

#### Account Tier Detection
- **Standard**: 60 weighted requests/minute
- **Premium**: 24,000 weighted requests/minute (400x more capacity)
- Tier is auto-detected on adapter initialization via `/api/v1/info/tier`

#### Funding Rate Cadence
- **HOURLY** funding (applied every hour on the hour)
- `funding_rate_interval_hours = 1` (always, never change to 8)
- The `/8` in formulas is for **realization normalization**, NOT payment cadence

#### account_index Semantics
- `account_index = 0`: Explicitly use account 0 (default/main trading account)
- `account_index = None`: Use adapter's default behavior (may be 0 or system-selected)
- `account_index > 0`: Custom API key account (1-254)

**Critical**: Never treat `0` and `None` as the same:
- `0` = "I explicitly want account 0"
- `None` = "Use whatever the adapter thinks is best"

#### WebSocket Constraints
- **Message limit**: 200 messages/minute (all account types)
- **Orderbook streams**: Can disconnect with "Too Many Websocket Messages"
- **Prefer**: On-demand per-symbol WS over full subscription

### X10 Exchange

#### Pagination
- API returns max 100 records per request
- **Must** implement pagination loop for funding history
- Previous bug: only fetched first page, lost ~37% of funding data

#### Market Data
- Supports either full book (no depth param) or `depth=1` (best bid/ask)
- We default to `depth=1` for lowest latency
- Full-book stream is high throughput, tends to disconnect

---

## Funding Rate API Formats

### Critical: Both Exchanges Return DECIMAL Format

**Common Mistake**: Dividing by 100 because APIs return "percent" format.

**Reality**: Both X10 and Lighter APIs return **DECIMAL format** (e.g., -0.000386 = -0.0386%).

### X10 (Extended) Funding Rate Format

```python
# X10 API returns DECIMAL format (e.g., -0.000360 = -31.5% annualized)
# NO division by 100 needed - API already returns decimal format

# ✅ CORRECT
rate_hourly = Decimal(str(rate_val))  # Already in decimal format

# ❌ WRONG
rate_hourly = Decimal(str(rate_val)) / Decimal("100")  # Don't do this!
```

**Example**:
- API returns: `-0.000360`
- Hourly rate: `-0.000360` (no division)
- APY: `-0.000360 * 24 * 365 = -3.15%`

**Locations in codebase**:
- `src/funding_bot/adapters/exchanges/x10/adapter.py:552-557`
- `src/funding_bot/services/historical/ingestion.py:520-524`

### Lighter Funding Rate Format

```python
# Lighter API returns DECIMAL format (e.g., -0.000386 = -0.0386%)
# NO division by 100 needed - API already returns decimal format

# ✅ CORRECT
rate_hourly = rate_raw / interval_hours  # interval_hours is usually 1

# ❌ WRONG
rate_hourly = (rate_raw / interval_hours) / Decimal("100")  # Don't do this!
```

**Example**:
- API returns: `-0.000386`
- Hourly rate: `-0.000386 / 1 = -0.000386` (no division by 100)
- APY: `-0.000386 * 24 * 365 = -3.38%`

**Direction Field**:
- Lighter returns unsigned rate + `direction` field
- `direction="short"` → shorts pay longs → long funding is NEGATIVE
- Otherwise → longs pay shorts → long funding is POSITIVE

**Locations in codebase**:
- `src/funding_bot/adapters/exchanges/lighter/adapter.py:740-745`
- `src/funding_bot/adapters/exchanges/lighter/adapter.py:954-960`
- `src/funding_bot/services/historical/ingestion.py:336-366`

### APY Calculation Formula

Both exchanges use the same APY formula:

```python
APY = hourly_rate * 24 * 365
```

**Example**:
```python
# X10: hourly_rate = -0.000360
funding_apy = -0.000360 * 24 * 365 = -3.154

# Lighter: hourly_rate = -0.000386
funding_apy = -0.000386 * 24 * 365 = -3.384
```

### Verification Against External Sources

**loris.tools** comparison:
- Bot APY should match loris.tools exactly
- If bot shows different values, check for `/100` division bugs

**Example verification**:
```
Bot: MEGA APY=234.8% (Lighter 1h: 0.0012%, X10 1h: -0.0256%)
loris.tools: Same values ✅
```

### Historical Impact

If you discover this bug in production:
1. **Fix the code** (remove `/100`)
2. **Run migration** to fix existing data
3. **Run partial backfill** to update recent data

**Migration pattern**:
```sql
-- Multiply all rates by 100 to fix incorrect division
UPDATE funding_history
SET rate_hourly = rate_hourly * 100,
    rate_apy = rate_apy * 100
WHERE exchange = 'LIGHTER'  -- or 'X10'
```

---

## PnL & Accounting Deep Dive

### VWAP Calculation

**Always** compute realized exit price using VWAP:
```
exit_vwap = sum(delta_qty * fill_price) / sum(delta_qty)
```

**Example**:
```python
# Order 1: Buy 0.5 BTC @ $50,000
# Order 2: Buy 0.3 BTC @ $50,100
# Order 3: Buy 0.2 BTC @ $49,900

exit_vwap = (0.5*50000 + 0.3*50100 + 0.2*49900) / (0.5 + 0.3 + 0.2)
         = (25000 + 15030 + 9980) / 1.0
         = $50,010
```

### Cumulative Fields

Many exchanges report cumulative `filled_qty` and `fee`. **Always** compute deltas:

```python
# WRONG: treating fields as absolute
total_filled = order['filled_qty']  # ✗ Cumulative!

# RIGHT: compute delta per order
first_fill = order_1['filled_qty'] - 0
second_fill = order_2['filled_qty'] - order_1['filled_qty']
total_filled = first_fill + second_fill  # ✓
```

### Funding Double-Count Prevention

**Rule**: Never add funding again if a venue's "realized_pnl" already includes funding.

**Best Practice**:
- Prefer dedicated funding endpoints (pure funding)
- If mixing is possible, subtract funding from realized_pnl before adding

### Post-Close Readback Correction

**When**: VWAP diff > 3 bps OR net_pnl diff > $0.30

**Action**:
1. Fetch actual fills from exchange
2. Recalculate VWAP from actual fills
3. Update trade with corrected values
4. Log correction event

---

## Testing Best Practices

### Offline Tests (Default)

**Requirement**: `pytest -q` runs without DNS or exchange SDK

**Pattern**:
```python
# ✗ BAD - requires exchange SDK
from lighter import LighterClient

def test_something():
    client = LighterClient(api_key)  # Requires SDK!

# ✓ GOOD - uses mocks
from unittest.mock import AsyncMock

def test_something():
    client = AsyncMock()
    client.get_positions.return_value = [...]
```

### Integration Tests

**Requirement**: Mark with `@pytest.mark.integration`, skip without env vars

**Pattern**:
```python
import pytest
import os

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("LIGHTER_API_KEY"), reason="No API key")
async def test_live_order_placement():
    # Only runs if LIGHTER_API_KEY is set
    pass
```

### Test Organization

| Type | Location | Requires | When to Run |
|------|----------|----------|-------------|
| Unit | `tests/unit/` | Nothing | Always (CI/CD) |
| Integration | `tests/integration/` | Env vars | Manual/CI |
| Verification | `tests/verification/` | Full setup | Before releases |

---

## Observability & Logging

### Guaranteed Per-Trade Metrics (logged every 60s)

```python
{
    "trade_id": "xxx",
    "symbol": "BTC-PERP",
    "pnl": {
        "price_pnl": "150.25",        # Realizable from spread
        "funding_collected": "45.30",  # Realized from exchanges
        "accrued_funding": "12.50",    # Forecast since last payment
        "total_pnl": "208.05"         # Net estimate
    },
    "risk": {
        "liquidation_distance": 0.15,  # % distance to liquidation
        "delta_imbalance": 0.02,       # Delta difference between legs
        "health_distance": 0.85        # Composite account health
    },
    "position": {
        "suggested_notional": "5000.00",
        "exit_cost_estimate": "2.50",
        "entry_age_hours": 4.5
    }
}
```

### Guaranteed Exit Event Logs

```python
{
    "event": "exit_triggered",
    "trade_id": "xxx",
    "exit": {
        "reason": "NetEV: Funding flipped negative",
        "layer": 2,  # 1=Emergency, 2=Econ, 3=Opt
        "final_pnl": "208.05",
        "hold_duration_hours": 4.5
    },
    "execution": {
        "leg_vwaps": {"lighter": "50123.45", "x10": "50128.90"},
        "leg_fees": {"lighter": "2.15", "x10": "1.85"},
        "post_close_correction": false,
        "correction_amount": "0.00"
    }
}
```

### What NOT to Log

- API keys, signer payloads, private addresses
- Raw auth headers
- Full order books (too large)
- Sensitive trading strategy parameters

---

## Common Issues & Solutions

### Issue: "no close frame received or sent"

**Cause**: Server drops TCP without WS close handshake (common transient disconnect)

**Solution**: Already handled in adapter - reconnection logic with circuit breaker

### Issue: "Too Many Websocket Messages"

**Cause**: Subscribed to too many markets on Lighter WS

**Solution**:
- Use on-demand per-symbol WS instead of full subscription
- Keep under 200 messages/minute limit
- Disable `market_data_streams_enabled` in config

### Issue: Funding history missing records

**Cause**: Not implementing pagination (X10 returns max 100 per page)

**Solution**: Use pagination loop with cursor

```python
all_payments = []
cursor = None
while True:
    params = {"market": symbol, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    data = await http_session.get(url, params=params)
    payments = data.get("data", [])
    if not payments:
        break
    all_payments.extend(payments)
    cursor = data.get("next_cursor")
```

### Issue: Ghost positions on startup

**Cause**: Shutdown didn't flush write queue - positions on exchange but not in DB

**Solution**:
- `store.close()` now waits up to 30s for flush completion
- If timeout, forces remaining items to DB
- Never skip flush on shutdown!

### Issue: Tests failing with "SDK not found"

**Cause**: Test imports exchange SDK without guard

**Solution**: Use try/except ImportError pattern

```python
try:
    from lighter import LighterClient
    LIGHTER_SDK_AVAILABLE = True
except ImportError:
    LIGHTER_SDK_AVAILABLE = False
    LighterClient = None  # Fallback

# In test:
@pytest.mark.skipif(not LIGHTER_SDK_AVAILABLE, reason="SDK not installed")
async def test_something():
    client = LighterClient(...)
```

---

## Performance Optimization (Premium)

### Connection Pooling

**Settings** (already optimized):
```python
TCPConnector(
    limit=200,              # Total connections
    limit_per_host=100,     # Per-host connections
    ttl_dns_cache=1800,     # 30 minutes DNS cache
    keepalive_timeout=300,  # 5 minutes keep-alive
)
```

### Parallel Operations

**Market Data Refresh**: 20 concurrent symbols
```python
# refresh.py
await self._refresh_symbols_parallel(symbols, batch_size=20)
```

**Exit Evaluation**: 10 concurrent positions
```python
# manager.py
eval_results = await self._evaluate_exits_parallel(trades, max_concurrent=10)
```

### Database Batching

**Already implemented**:
```python
# write_queue.py - using executemany for 10x improvement
await self._conn.executemany(sql, batch_values)
```

### Monitoring

**Rate Limit Proximity** (tracked automatically):
- Alert at 80% of limit
- Dynamic backoff on 429
- Exponential backoff with max cap

---

## Debugging Tips

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Check Exchange Rate Limits

```python
# Lighter adapter logs rate limit proximity
grep "rate.limit.proximity" logs/funding_bot_json.jsonl
```

### Verify Market Cache

```python
# Check if market cache is stale
if adapter._is_market_cache_stale():
    await adapter.load_markets()  # Will refresh
```

### Monitor Write Queue

```python
stats = store.get_queue_stats()
print(f"Queue size: {stats['queue_size']}/{stats['queue_maxsize']}")
print(f"Full rate: {stats['full_rate']:.2%}")
```

---

## Historical Data Features

### Funding Velocity Exit

**Requires**: 6 hours of hourly net APY

**Implementation**:
```python
recent_apy = await store.get_recent_apy_history(
    symbol=symbol,
    hours_back=6,  # Need 6 data points
)
```

**Graceful Degradation**: Disabled with one-time warning if insufficient data

### Z-Score Exit

**Requires**: 168 hours (7 days) of hourly net APY

**Implementation**:
```python
recent_apy = await store.get_recent_apy_history(
    symbol=symbol,
    hours_back=168,  # Need 7 days of data
)
```

**Graceful Degradation**: Returns None (no exit) if insufficient data

### Data Collection

Historical APY data is automatically collected by `FundingTracker._record_funding_history()`:
- Runs every funding rate refresh (30-60 seconds)
- Stores both LIGHTER and X10 rates in `funding_history` table
- Net APY = `abs(lighter_apy - x10_apy)` computed at query time

---

## Config Key Reference

### Market Data Settings

| Key | Default | Purpose |
|-----|---------|---------|
| `market_data_batch_refresh_interval_seconds` | 30.0 | How often to refresh market data |
| `market_cache_ttl_seconds` | 3600.0 | How long to cache market metadata |
| `orderbook_l1_retry_delay_seconds` | 0.3 | Delay between L1 fetch retries |
| `orderbook_l1_fallback_max_age_seconds` | 10.0 | Max age for cached L1 data |

### Execution Settings

| Key | Default | Purpose |
|-----|---------|---------|
| `maker_timeout_seconds` | 5.0 | How long to wait for maker fill |
| `ioc_retry_attempts` | 3 | How many IOC retries on maker timeout |
| `taker_close_slippage_bps` | 10 | Slippage tolerance for taker closes |

### Database Settings

| Key | Default | Purpose |
|-----|---------|---------|
| `write_batch_size` | 50 | How many writes to batch |
| `write_queue_max_size` | 1000 | Max queue size before backpressure |
| `open_trades_cache_ttl` | 5.0 | How long to cache open trades list |

---

## See Also

- **PLANNING.md**: Architecture, design principles, absolute rules
- **TASK.md**: Current tasks, priorities, backlog
- **CONTRIBUTING.md**: Workflow guidelines, PR process
