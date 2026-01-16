# Troubleshooting Report: ONDO Orderbook Snapshot Issue

**Date**: 2026-01-15
**Error**: `Failed to fetch fresh data for candidate ONDO: No depth-valid orderbook snapshot after retries`
**Status**: Root Cause Identified & Solution Proposed

---

## Error Analysis

### Error Message
```
14:56:42 [WARN] Failed to fetch fresh data for candidate ONDO:
No depth-valid orderbook snapshot after retries
(symbol=ONDO, attempts=3, fallback_max_age_s=8.0, lighter_err=None, x10_err=None)
```

### Key Observations
1. **`lighter_err=None`**: Lighter returned valid orderbook data
2. **`x10_err=None`**: X10 also returned orderbook data (no exception)
3. **Problem**: The snapshot is not "depth-valid" for both exchanges simultaneously

---

## Root Cause

### Issue Location
**File**: `src/funding_bot/adapters/exchanges/x10/adapter.py:731`

### Problem Code
```python
# Only cache if the snapshot is depth-valid (prevents poisoning the cache with zeros/partials).
if best_bid > 0 and best_ask > 0 and best_bid < best_ask and bid_qty > 0 and ask_qty > 0:
    self._orderbook_cache[symbol] = result
    # ... cache it
```

### What's Happening

1. **X10 returns orderbook with `bid_qty=0` or `ask_qty=0`**
   - For low-volume symbols like ONDO, EDEN
   - Or when the orderbook is thin
   - Or when only one side has depth

2. **Adapter refuses to cache it**
   - Because `bid_qty > 0 and ask_qty > 0` check fails
   - This is a safety measure to prevent "cache poisoning"

3. **Every retry fails the same way**
   - Call 1: Fetch from X10 → gets `bid_qty=0` → doesn't cache
   - Call 2: Cache miss → fetch from X10 → gets `bid_qty=0` → doesn't cache
   - Call 3: Same pattern → raises error

4. **get_fresh_orderbook() requires BOTH exchanges to be depth-valid**
   - File: `src/funding_bot/services/market_data/fresh.py:95-116`
   - Function: `_snapshot_depth_ok()`
   - Requirements:
     - `lighter_bid_qty > 0` AND `lighter_ask_qty > 0` ✅
     - `x10_bid_qty > 0` AND `x10_ask_qty > 0` ❌ ← FAILS HERE

---

## Why This Happens

### Low-Volume Symbols (ONDO, EDEN)

These symbols may have:
- **Thin orderbooks**: Only 1-2 levels of depth
- **One-sided depth**: Bids but no asks (or vice versa)
- **Zero quantity**: Best bid/ask price exists but qty=0

### X10 API Behavior

When calling `get_orderbook_snapshot(market_name=symbol, limit=1)`:
- Returns best bid/ask level
- If that level has `qty=0`, the adapter marks it as "not depth-valid"
- **This is correct behavior** - we don't want to trade on symbols with no liquidity

### The Real Problem

**The issue is NOT that X10 returns bad data - it's that the validation is too strict for the use case.**

For **opportunity scanning** (not execution), we might want to:
1. Accept orderbooks with ONE side depth > 0
2. Use cached data even if slightly stale
3. Have a lower "minimum liquidity" threshold for scanning

---

## WebSocket Streams Analysis

### Current Implementation

The bot already has WebSocket infrastructure:
- **File**: `src/funding_bot/adapters/exchanges/x10/adapter.py:218-234`
- **Variables**:
  - `_ob_ws_task`: Public orderbook WebSocket task
  - `_ob_ws_allowlist`: Market symbols to subscribe
  - `_ob_ws_depth`: Depth level (currently 1)
  - `_per_market_ws_tasks`: Per-market WebSocket connections

### WebSocket Benefit

**WebSocket streams provide:**
- Real-time orderbook updates
- Push-based (no polling latency)
- More consistent data than REST polling

**But WebSocket won't fix this issue because:**
- If X10's orderbook has `qty=0`, WebSocket will also report `qty=0`
- The validation logic (`bid_qty > 0`) will still fail
- **The problem is not the data source, but the validation criteria**

---

## Solution Options

### Option 1: Relax Validation for Scanning (Recommended)

**Context**: Opportunity scanning is different from execution

**Change**: For `get_fresh_orderbook()`, accept ONE-SIDED depth

**File**: `src/funding_bot/services/market_data/fresh.py:95-116`

**Current**:
```python
def _snapshot_depth_ok(ob: OrderbookSnapshot, now: datetime) -> bool:
    if (
        ob.lighter_bid <= 0
        or ob.lighter_ask <= 0
        or ob.x10_bid <= 0
        or ob.x10_ask <= 0
        or ob.lighter_bid_qty <= 0
        or ob.lighter_ask_qty <= 0
        or ob.x10_bid_qty <= 0
        or ob.x10_ask_qty <= 0  # ← STRICT: Requires BOTH sides
    ):
        return False
```

**Proposed**:
```python
def _snapshot_depth_ok(ob: OrderbookSnapshot, now: datetime) -> bool:
    # For opportunity scanning, accept if AT LEAST ONE side has depth on BOTH exchanges
    lighter_has_depth = ob.lighter_bid_qty > 0 or ob.lighter_ask_qty > 0
    x10_has_depth = ob.x10_bid_qty > 0 or ob.x10_ask_qty > 0

    if not lighter_has_depth or not x10_has_depth:
        return False

    # Still require valid prices (bid < ask)
    if ob.lighter_bid <= 0 or ob.lighter_ask <= 0:
        return False
    if ob.x10_bid <= 0 or ob.x10_ask <= 0:
        return False

    if ob.lighter_bid >= ob.lighter_ask or ob.x10_bid >= ob.x10_ask:
        return False

    return True
```

**Impact**: Would allow scanning to proceed even if one side has `qty=0`

---

### Option 2: Cache "Partial" Orderbooks (Less Recommended)

**Change**: Allow caching of orderbooks with ONE side depth

**File**: `src/funding_bot/adapters/exchanges/x10/adapter.py:731`

**Current**:
```python
if best_bid > 0 and best_ask > 0 and best_bid < best_ask and bid_qty > 0 and ask_qty > 0:
    self._orderbook_cache[symbol] = result
```

**Proposed**:
```python
# Cache if we have valid prices and AT LEAST ONE side has depth
if best_bid > 0 and best_ask > 0 and best_bid < best_ask and (bid_qty > 0 or ask_qty > 0):
    self._orderbook_cache[symbol] = result
```

**Risk**: Could lead to false positives in opportunity scanning

---

### Option 3: Use WebSocket Streams (Doesn't Fix Root Cause)

**Benefit**: More real-time data, lower latency

**Implementation**:
1. Enable orderbook WebSocket subscriptions for candidate symbols
2. Consume WebSocket updates in real-time
3. Use cached WebSocket data instead of REST polling

**Limitation**: If orderbook has `qty=0`, WebSocket will also report `qty=0`

**Recommendation**: Implement for performance, but not as fix for this issue

---

## Recommended Action Plan

### Immediate Fix (Option 1)

1. **Relax validation for opportunity scanning**
   - Modify `_snapshot_depth_ok()` to accept one-sided depth
   - File: `src/funding_bot/services/market_data/fresh.py:95-116`

2. **Add debug logging**
   - Log when orderbook has partial depth
   - Log which side has depth

3. **Update pre-flight liquidity check**
   - Already handles one-sided depth correctly
   - Should work better after fix

### Longer-term Enhancement (Option 3)

1. **Enable WebSocket streams for X10**
   - Subscribe to orderbook updates for active markets
   - Reduce REST polling latency
   - Improve data freshness

2. **Add "partial depth" support**
   - Distinguish between "no data" and "partial data"
   - Use partial data for scanning, full data for execution

---

## Testing Verification

### Before Fix
```
[WARN] Failed to fetch fresh data for candidate ONDO:
No depth-valid orderbook snapshot after retries
```

### After Fix (Expected)
```
[DEBUG] ONDO: Using partial depth (Lighter: bid_qty=100, ask_qty=0; X10: bid_qty=50, ask_qty=0)
[INFO] Successfully fetched orderbook for ONDO (attempts=1)
```

---

## Configuration Check

Check your WebSocket settings:
```yaml
websocket:
  x10_ws_enabled: true  # Should be enabled
  orderbook_l1_retry_attempts: 3
  orderbook_l1_retry_delay_seconds: 0.3
  orderbook_l1_fallback_max_age_seconds: 10.0
```

---

## Summary

**Root Cause**: Validation requires BOTH `bid_qty > 0` AND `ask_qty > 0` for both exchanges, but X10 returns `qty=0` for low-volume symbols.

**Impact**: Cannot scan low-volume symbols (ONDO, EDEN, etc.)

**Solution**: Relax validation to accept ONE-SIDED depth for opportunity scanning.

**Risk**: Low - scanning is not execution, we can be more permissive here.

---

**Generated**: 2026-01-15
**Next**: Implement Option 1 and test with ONDO symbol
