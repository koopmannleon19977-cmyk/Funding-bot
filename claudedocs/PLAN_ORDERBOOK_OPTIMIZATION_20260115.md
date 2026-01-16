# Implementation Plan: Orderbook Fetching Optimization

**Date**: 2026-01-15
**Problem**: "No depth-valid orderbook snapshot after retries" for MEGA and other low-volume symbols
**Status**: Planning Phase

---

## Problem Analysis

### Root Cause

The error occurs in `fresh.py:258-263` when:
1. The orderbook fetch succeeds (no API errors: `lighter_err=None, x10_err=None`)
2. But the returned data fails validation in `_snapshot_depth_ok()`
3. After 3 retries with exponential backoff, the symbol is rejected

### Why MEGA Fails

MEGA is a **low-volume symbol** where:
- One or both sides of the orderbook may be empty
- Market makers may not be present
- The orderbook can be one-sided (only bids OR only asks)

### Current Code Flow

```
get_fresh_orderbook()
  → adapter.get_orderbook_l1() [REST with limit=1]
  → _snapshot_depth_ok() validation
  → Retry up to 3 times
  → RuntimeError if all retries fail
```

### Current Limitations

1. **REST-only**: Uses REST API with 3 retries instead of WebSocket
2. **No caching fallback**: If API returns empty data, we don't use cached data
3. **Poor diagnostics**: No logging of WHY validation failed

---

## Solution Architecture

Based on the API research, the optimal approach is:

### Priority 1: Enhanced Diagnostics (Quick Win)

Add detailed logging to understand exactly WHY symbols fail.

### Priority 2: WebSocket-First Architecture

| Exchange | Current | Optimized |
|----------|---------|-----------|
| **Lighter** | REST `limit=1` | WebSocket `order_book/{MARKET_INDEX}` (50ms) |
| **X10** | REST `limit=1` | WebSocket `orderbooks/{market}?depth=1` (10ms) |

### Priority 3: Intelligent Fallback Chain

```
1. WebSocket cache (if fresh < 2s)
2. REST API with explicit limit parameter
3. Cached data with age warning
4. Skip symbol (don't fail the entire scan)
```

---

## Implementation Plan

### Phase 1: Enhanced Diagnostics (30 min)

**File**: `src/funding_bot/services/market_data/fresh.py`

**Changes**:
1. Add detailed logging BEFORE validation
2. Log exactly which field(s) caused validation to fail
3. Log the actual values returned from each exchange

**Code**:
```python
# Before _snapshot_depth_ok() call, log the actual data
logger.debug(
    f"[{symbol}] Orderbook data attempt {attempt+1}/{attempts}: "
    f"lighter(bid={l_bid}, ask={l_ask}, bq={l_bq}, aq={l_aq}), "
    f"x10(bid={x_bid}, ask={x_ask}, bq={x_bq}, aq={x_aq})"
)
```

### Phase 2: Graceful Degradation (1 hour)

**File**: `src/funding_bot/services/market_data/fresh.py`

**Changes**:
1. Don't raise RuntimeError for low-volume symbols
2. Return cached data with staleness warning
3. Add `partial_data` flag to snapshot

**New Behavior**:
```python
# Instead of raising RuntimeError:
if cached_snapshot and cached_age < max_stale_age:
    logger.warning(
        f"[{symbol}] Using stale orderbook data (age={cached_age:.1f}s)"
    )
    return cached_snapshot

# If truly no data, return empty snapshot with warning
logger.warning(f"[{symbol}] No orderbook data available, skipping")
return None  # Caller handles None gracefully
```

### Phase 3: WebSocket Integration (2-3 hours)

#### 3.1 Lighter WebSocket

**File**: `src/funding_bot/adapters/exchanges/lighter/adapter.py`

The adapter already has WebSocket infrastructure (`_orderbook_ws_loop`, `ensure_orderbook_ws`).

**Optimize**:
1. Use existing WS cache in `get_orderbook_l1()`
2. Only fall back to REST if WS cache is stale (> 2s)

**Code Pattern**:
```python
async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
    # Check WS cache first (50ms updates)
    if symbol in self._orderbook_cache:
        cached = self._orderbook_cache[symbol]
        cache_age = (datetime.now(UTC) - self._orderbook_updated_at.get(symbol, datetime.min)).total_seconds()
        if cache_age < 2.0 and self._is_depth_valid(cached):
            return cached

    # Fall back to REST
    return await self._fetch_orderbook_rest(symbol)
```

#### 3.2 X10 WebSocket with depth=1

**File**: `src/funding_bot/adapters/exchanges/x10/adapter.py`

**Current Issue**: X10 WebSocket doesn't use `depth=1` parameter.

**Fix**:
1. Modify `_start_orderbook_websocket()` to use `depth=1` parameter
2. Get 10ms updates instead of 100ms

**Code Pattern**:
```python
async def _start_orderbook_websocket(self, symbol: str) -> None:
    # Use depth=1 for 10ms updates (best bid/ask only)
    url = f"{self._get_stream_api_url()}/orderbooks/{symbol}?depth=1"
    # ... existing WebSocket logic
```

### Phase 4: Intelligent Skip Logic (30 min)

**File**: `src/funding_bot/services/opportunities/scanner.py`

**Changes**:
1. Track symbols that consistently fail
2. Temporarily skip symbols with N consecutive failures
3. Re-enable after cooldown period

**Code Pattern**:
```python
class OpportunityScanner:
    def __init__(self):
        self._symbol_failures: dict[str, int] = {}
        self._symbol_cooldown: dict[str, datetime] = {}
        self._max_failures = 5
        self._cooldown_minutes = 10

    def _should_skip_symbol(self, symbol: str) -> bool:
        """Skip symbols with too many consecutive failures."""
        if symbol in self._symbol_cooldown:
            if datetime.now(UTC) < self._symbol_cooldown[symbol]:
                return True
            # Cooldown expired, reset
            del self._symbol_cooldown[symbol]
            self._symbol_failures[symbol] = 0

        return self._symbol_failures.get(symbol, 0) >= self._max_failures

    def _record_symbol_failure(self, symbol: str) -> None:
        """Record a failure and possibly enter cooldown."""
        self._symbol_failures[symbol] = self._symbol_failures.get(symbol, 0) + 1
        if self._symbol_failures[symbol] >= self._max_failures:
            self._symbol_cooldown[symbol] = datetime.now(UTC) + timedelta(minutes=self._cooldown_minutes)
            logger.warning(f"[{symbol}] Entering {self._cooldown_minutes}min cooldown after {self._max_failures} failures")
```

---

## API Reference (from Research)

### Lighter

| Method | Endpoint | Parameters | Update Speed |
|--------|----------|------------|--------------|
| REST L1 | `/api/v1/orderBookOrders` | `market_id`, `limit=1-100` | On-demand |
| WS Orderbook | `wss://.../stream` → `order_book/{MARKET_INDEX}` | None | 50ms |

### X10

| Method | Endpoint | Parameters | Update Speed |
|--------|----------|------------|--------------|
| REST | `/api/v1/info/markets/{market}/orderbook` | None | On-demand |
| WS Full | `wss://.../orderbooks/{market}` | None | 100ms |
| WS L1 | `wss://.../orderbooks/{market}?depth=1` | `depth=1` | **10ms** |

---

## Testing Strategy

### Unit Tests

1. Test `_snapshot_depth_ok()` with various edge cases:
   - Empty orderbook (both sides)
   - One-sided orderbook (bid only / ask only)
   - Valid two-sided orderbook
   - Stale timestamps

2. Test graceful degradation:
   - Returns cached data when API fails
   - Returns None for truly empty symbols
   - Doesn't block scanner on single symbol failure

### Integration Tests

1. Test with real MEGA symbol
2. Verify WebSocket connection and updates
3. Measure latency improvement (REST vs WS)

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| WebSocket disconnection | Automatic reconnection + REST fallback |
| Rate limit exhaustion | WS doesn't count against rate limits |
| Stale data trading | Age check before using cached data |
| All symbols failing | Circuit breaker at scanner level |

---

## Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| MEGA fetch success rate | ~0% | >80% |
| Orderbook fetch latency | 300ms (REST) | <50ms (WS) |
| Scanner throughput | N/A | No single-symbol blocking |

---

## Recommended Implementation Order

1. **Phase 1: Diagnostics** - Understand exactly why MEGA fails
2. **Phase 2: Graceful Degradation** - Stop blocking the scanner
3. **Phase 3.2: X10 WebSocket depth=1** - Fastest improvement (10ms)
4. **Phase 3.1: Lighter WebSocket** - Use existing infrastructure
5. **Phase 4: Skip Logic** - Handle persistently failing symbols

---

## Decision Point

Before implementing, we need to answer:

**Is MEGA a valid trading symbol?**
- If MEGA has no liquidity on either exchange, we should blacklist it
- If MEGA occasionally has liquidity, we should gracefully handle empty orderbooks

**Run this diagnostic first:**
```bash
# Check if MEGA exists on both exchanges
curl -s "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders?market_id=X&limit=10"
curl -s "https://api.starknet.extended.exchange/api/v1/info/markets/MEGA-USD/orderbook"
```

---

**Next Step**: Run Phase 1 diagnostics to understand the exact failure mode.
