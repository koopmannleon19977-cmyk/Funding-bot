# X10 SDK Integration - Critical Parameters

**Date**: 2026-01-15
**Category: Exchange Integration

---

## Critical Discovery: `limit` Parameter Required

The X10 SDK's `get_orderbook_snapshot()` method has an optional `limit` parameter that **must be explicitly passed** for reliable behavior.

### Without `limit` Parameter (BROKEN)
```python
# Undefined behavior - may return empty/partial data
response = await sdk.get_orderbook_snapshot(market_name=symbol)
```

### With `limit` Parameter (CORRECT)
```python
# Returns exactly N depth levels (max 200)
response = await sdk.get_orderbook_snapshot(market_name=symbol, limit=N)
```

## Impact

**Low-volume symbols** (EDEN, ONDO):
- Without `limit`: Returns 0 or empty data
- With `limit`: Returns available depth levels

**High-volume symbols** (BTC, ETH):
- Without `limit`: May return inconsistent data
- With `limit`: Returns full requested depth

## Implementation

### L1 Fetch (Best Bid/Ask)
```python
response = await self._sdk_call_with_retry(
    lambda: self._trading_client.markets_info.get_orderbook_snapshot(
        market_name=symbol, 
        limit=1  # ← Explicitly request L1 only
    ),
    operation_name="get_orderbook_snapshot",
)
```

### Depth Fetch (Multiple Levels)
```python
response = await self._sdk_call_with_retry(
    lambda: self._trading_client.markets_info.get_orderbook_snapshot(
        market_name=symbol, 
        limit=limit  # ← Request N depth levels
    ),
    operation_name="get_orderbook_snapshot_depth",
)
```

## Monitoring

Log when partial depth is returned:
```python
if len(bids) < limit or len(asks) < limit:
    logger.debug(
        f"[{symbol}] X10 returned partial depth: "
        f"{len(bids)} bids (requested {limit}), "
        f"{len(asks)} asks (requested {limit})"
    )
```

## API Limits

- **Maximum limit**: 200 levels
- **Recommended for scanning**: 5-20 levels
- **Recommended for execution**: 1-5 levels

## Related Files

- `src/funding_bot/adapters/exchanges/x10/adapter.py:688,807,841-847`
- `tests/unit/adapters/x10/test_orderbook_depth.py`

## Test Coverage

7 comprehensive unit tests covering:
- L1 fetch with limit=1
- Depth fetch with limit=N
- Low-volume symbol handling
- Empty orderbook handling
- List format parsing
- Maximum limit (200) enforcement
- SDK error handling

