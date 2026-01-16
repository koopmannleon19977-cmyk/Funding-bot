# Knowledge Base Update: Orderbook Depth & Low-Volume Symbols

**Date**: 2026-01-15
**Category: Market Data Integration

---

## Key Insight: One-Sided Depth is Normal

For low-volume symbols (ONDO, EDEN, etc.), one-sided orderbook depth is **normal market behavior**, not bad data:
- Thin orderbooks with 1-2 levels
- Bids but no asks (or vice versa)
- Zero quantity on one side

## Solution: Relaxed Validation for Scanning

**Opportunity Scanning** (permissive):
- Accept if AT LEAST ONE side has depth on BOTH exchanges
- Still require valid prices (bid > 0, ask > 0, bid < ask)
- Still require fresh data (respect fallback_max_age)

**Execution** (strict):
- Pre-flight liquidity check validates both sides
- Position sizing respects available depth
- Safety gates remain in place

## Implementation Pattern

```python
def _snapshot_depth_ok(ob: OrderbookSnapshot, now: datetime) -> bool:
    # Accept one-sided depth for scanning
    lighter_has_depth = ob.lighter_bid_qty > 0 or ob.lighter_ask_qty > 0
    x10_has_depth = ob.x10_bid_qty > 0 or ob.x10_ask_qty > 0
    
    if not lighter_has_depth or not x10_has_depth:
        return False
    
    # Still require valid prices
    if ob.lighter_bid <= 0 or ob.lighter_ask <= 0:
        return False
    if ob.x10_bid <= 0 or ob.x10_ask <= 0:
        return False
    if ob.lighter_bid >= ob.lighter_ask or ob.x10_bid >= ob.x10_ask:
        return False
    
    # Require timestamps and freshness
    if not ob.lighter_updated or not ob.x10_updated:
        return False
    if fallback_max_age > 0:
        if (now - ob.lighter_updated).total_seconds() > fallback_max_age:
            return False
        if (now - ob.x10_updated).total_seconds() > fallback_max_age:
            return False
    
    return True
```

## Related Files

- `src/funding_bot/services/market_data/fresh.py:94-115`
- `src/funding_bot/services/liquidity_gates_preflight.py:106-350`

## Test Results

301 unit tests passing after all improvements.

---

## Update 2026-01-15: Log Improvements Complete

### Changes Made
1. **Partial depth warnings**: Changed from WARNING to DEBUG level
2. **Historical APY warnings**: Consolidated 20 individual warnings into single summary
3. **Nonce gap logs**: Added flag to prevent duplicate logging during WS sync
4. **X10 close timeout**: Added 5s timeouts (was 18s hanging)
5. **Duplicate logs removed**: REST pool init/close, X10 adapter init

### Root Cause Confirmed
Low-volume symbol rejections are **correct behavior**:
- KAITO: $862 L1 notional (need $4,000)
- MEGA: Both sides thin
- 1000SHIB: Missing X10 SELL orders

The depth gates correctly protect against "APY traps" where high rates mask low liquidity.

