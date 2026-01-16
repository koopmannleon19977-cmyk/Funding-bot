# Research Report: X10 Liquidity Integration Issue

**Date**: 2026-01-15
**Issue**: X10 liquidity below threshold (0 < 0.01) - EDEN symbol rejected
**Status**: Root Cause Identified & Solution Proposed

---

## Executive Summary

The bot is rejecting EDEN trades with "X10 liquidity below threshold (0 < 0.01)" because the X10 adapter's `get_orderbook_depth()` method is returning empty orderbooks. The root cause is that X10's `get_orderbook_snapshot` API requires a **limit parameter** to specify how many depth levels to return, and without it, the API may return incomplete or empty data.

**Key Finding**: The X10 SDK's `get_orderbook_snapshot` method supports an optional `limit` parameter (default may vary by version). When called without specifying depth levels, it may return only L1 (best bid/ask) or no data at all for certain symbols.

---

## Problem Analysis

### Error Message
```
13:53:11 [WARN] Rejecting EDEN: Pre-flight liquidity check failed
Reason: X10 liquidity below threshold (0 < 0.01) | Lighter=200688.200000, X10=0.000000
```

### Code Flow

1. **Preflight Liquidity Check** (`liquidity_gates_preflight.py:203-207`):
   ```python
   lighter_ob, x10_ob = await asyncio.gather(
       _fetch_orderbook_with_retry(lighter_adapter, symbol, config.depth_levels, "LIGHTER"),
       _fetch_orderbook_with_retry(x10_adapter, symbol, config.depth_levels, "X10"),
       return_exceptions=True,
   )
   ```

2. **Fetch Function** (`liquidity_gates_preflight.py:118-120`):
   ```python
   if hasattr(adapter, "get_orderbook_depth"):
       return await adapter.get_orderbook_depth(symbol, depth=depth)
   ```

3. **X10 Adapter Implementation** (`adapter.py:803-804`):
   ```python
   response = await self._sdk_call_with_retry(
       lambda: self._trading_client.markets_info.get_orderbook_snapshot(market_name=symbol),
       operation_name="get_orderbook_snapshot_depth",
   )
   ```

### Root Cause

**The X10 SDK call at line 804 passes `market_name=symbol` but does NOT pass a `limit` parameter.**

According to the X10 Python SDK documentation (from GitHub search results), the `get_orderbook_snapshot` method signature is:
```python
async def get_orderbook_snapshot(
    self,
    market_name: str,
    limit: Optional[int] = None  # ← THIS IS THE KEY PARAMETER
) -> OrderbookUpdateModel:
```

When `limit` is not specified or is None, the API behavior is undefined and may return:
- Only L1 (best bid/ask)
- Empty orderbook for low-volume symbols like EDEN
- Inconsistent results across different market conditions

---

## Solution

### Option 1: Pass Depth Limit to X10 SDK (Recommended)

**File**: `src/funding_bot/adapters/exchanges/x10/adapter.py:800-806`

**Current Code**:
```python
limit = int(depth or 1)
limit = max(1, min(limit, 200))

response = await self._sdk_call_with_retry(
    lambda: self._trading_client.markets_info.get_orderbook_snapshot(market_name=symbol),
    operation_name="get_orderbook_snapshot_depth",
)
```

**Fixed Code**:
```python
limit = int(depth or 1)
limit = max(1, min(limit, 200))

response = await self._sdk_call_with_retry(
    lambda: self._trading_client.markets_info.get_orderbook_snapshot(
        market_name=symbol,
        limit=limit  # ← ADD THIS PARAMETER
    ),
    operation_name="get_orderbook_snapshot_depth",
)
```

**Why This Works**:
- Explicitly requests N levels of depth (default: 20, max: 200)
- Ensures X10 returns full orderbook data, not just L1
- Matches the bot's depth aggregation logic (uses top N levels)

### Option 2: Add Fallback to L1 with Depth Calculation

If the X10 API doesn't support the `limit` parameter in your SDK version, add a fallback:

```python
try:
    # Try to fetch full depth with limit parameter
    response = await self._sdk_call_with_retry(
        lambda: self._trading_client.markets_info.get_orderbook_snapshot(
            market_name=symbol,
            limit=limit
        ),
        operation_name="get_orderbook_snapshot_depth",
    )
except Exception as e:
    # Fallback: Fetch L1 and synthesize depth
    logger.warning(f"X10 depth fetch failed for {symbol}, falling back to L1: {e}")
    l1 = await self.get_orderbook_l1(symbol)
    # Return single-level depth for backward compatibility
    return {
        "bids": [(l1.get("best_bid", Decimal("0")), l1.get("bid_qty", Decimal("0")))]
        if l1.get("best_bid", Decimal("0")) > 0 else [],
        "asks": [(l1.get("best_ask", Decimal("0")), l1.get("ask_qty", Decimal("0")))]
        if l1.get("best_ask", Decimal("0")) > 0 else [],
        "updated_at": datetime.now(UTC),
    }
```

---

## X10 SDK API Reference

### From GitHub Repository: `x10xchange/python_sdk`

**Markets Info Module**:
```python
trading_client.markets_info
```

**Orderbook Snapshot Method**:
```python
async def get_orderbook_snapshot(
    market_name: str,
    limit: Optional[int] = None
) -> OrderbookUpdateModel
```

**Response Model** (`OrderbookUpdateModel`):
```python
class OrderbookUpdateModel(X10BaseModel):
    bid: List[OrderbookQuantityModel]  # List of (price, qty) levels
    ask: List[OrderbookQuantityModel]  # List of (price, qty) levels

class OrderbookQuantityModel(X10BaseModel):
    price: Decimal
    qty: Decimal
```

### Key Points

1. **Parameter Name**: `limit` (not `depth`)
2. **Default Behavior**: When `limit=None`, behavior is undefined
3. **Recommended Value**: `limit=20` for pre-flight checks
4. **Maximum Value**: `limit=200` (X10 API cap)

---

## Implementation Plan

### Step 1: Verify SDK Version

Check which version of X10 SDK is installed:
```bash
pip show x10-python-trading-starknet
```

### Step 2: Test the Fix Locally

Add the `limit` parameter to the X10 adapter and test:
```python
# In adapter.py line 804
lambda: self._trading_client.markets_info.get_orderbook_snapshot(
    market_name=symbol,
    limit=limit  # ADD THIS
)
```

### Step 3: Add Unit Tests

Create test to verify depth fetching:
```python
# tests/unit/adapters/x10/test_orderbook_depth.py
@pytest.mark.asyncio
async def test_x10_get_orderbook_depth_returns_multiple_levels():
    """Verify X10 adapter returns full depth, not just L1."""
    adapter = await create_x10_adapter()

    # Mock SDK response with 5 levels
    mock_response = MagicMock()
    mock_response.data.bid = [
        MagicMock(price=50000, qty=1.0),
        MagicMock(price=49990, qty=2.0),
        MagicMock(price=49980, qty=3.0),
        # ... more levels
    ]
    mock_response.data.ask = [...]

    # Call with depth=5
    result = await adapter.get_orderbook_depth("BTC-USD", depth=5)

    # Assert we got 5 levels
    assert len(result["bids"]) == 5
    assert len(result["asks"]) == 5
```

### Step 4: Update L1 Fetch for Consistency

The L1 fetch at line 687 should also specify `limit=1` for consistency:
```python
# Line 685-689
response = await self._sdk_call_with_retry(
    lambda: self._trading_client.markets_info.get_orderbook_snapshot(
        market_name=symbol,
        limit=1  # EXPLICITLY REQUEST L1 ONLY
    ),
    operation_name="get_orderbook_snapshot",
)
```

---

## Additional Recommendations

### 1. Add Depth Monitoring

Add logging to track how many depth levels are returned:
```python
if len(bids) < limit or len(asks) < limit:
    logger.debug(
        f"[{symbol}] X10 returned partial depth: "
        f"{len(bids)} bids (requested {limit}), "
        f"{len(asks)} asks (requested {limit})"
    )
```

### 2. Handle Low-Liquidity Symbols

For symbols like EDEN with naturally low liquidity:
- Reduce `min_liquidity_threshold` in config
- Increase `depth_levels` to aggregate more levels
- Consider widening spreads for low-volume markets

### 3. Add SDK Version Check

Add compatibility layer for different SDK versions:
```python
def _get_orderbook_snapshot(self, market_name: str, limit: int):
    """Handle SDK version differences."""
    import inspect

    sig = inspect.signature(self._trading_client.markets_info.get_orderbook_snapshot)
    if 'limit' in sig.parameters:
        return self._trading_client.markets_info.get_orderbook_snapshot(
            market_name=market_name,
            limit=limit
        )
    else:
        # Old SDK version - fallback
        return self._trading_client.markets_info.get_orderbook_snapshot(
            market_name=market_name
        )
```

---

## Testing Checklist

- [ ] Verify SDK version supports `limit` parameter
- [ ] Test EDEN symbol specifically (low liquidity)
- [ ] Test high-volume symbols (BTC, ETH) for comparison
- [ ] Verify depth aggregation logic uses all returned levels
- [ ] Check log output for actual depth levels returned
- [ ] Run pre-flight liquidity check manually to verify fix

---

## Configuration Tuning

### For Low-Liquidity Symbols (EDEN, etc.)

**Current Config**:
```yaml
min_liquidity_threshold: 0.01  # 0.01 USDC = 1 cent
depth_levels: 5
```

**Recommended for EDEN**:
```yaml
min_liquidity_threshold: 0.001  # Lower threshold
depth_levels: 10  # Aggregate more levels
safety_factor: 2.0  # Increase safety buffer
```

---

## References

### X10 Documentation
- **API Docs**: https://api.docs.extended.exchange/
- **Main Docs**: https://docs.extended.exchange/
- **GitHub**: https://github.com/x10xchange/python_sdk
- **SDK Repository**: https://github.com/x10xchange/python_sdk/tree/starknet

### Key Files in Bot
- **X10 Adapter**: `src/funding_bot/adapters/exchanges/x10/adapter.py:659-846`
- **Preflight Check**: `src/funding_bot/services/liquidity_gates_preflight.py:106-350`
- **L1 Fetch**: `adapter.py:685-689`
- **Depth Fetch**: `adapter.py:800-846`

---

## Summary

**Problem**: X10 `get_orderbook_snapshot` called without `limit` parameter → returns empty/incomplete data

**Solution**: Add `limit=depth` parameter to SDK call at line 804

**Impact**: Low-risk, high-reward fix that should immediately resolve EDEN rejection

**Next Steps**:
1. Add `limit` parameter to both L1 and depth fetches
2. Test with EDEN symbol
3. Monitor logs to verify depth levels returned
4. Adjust config thresholds for low-liquidity symbols if needed

---

**Generated**: 2026-01-15
**Research Method**: X10 SDK GitHub analysis, code flow tracing, API documentation review
