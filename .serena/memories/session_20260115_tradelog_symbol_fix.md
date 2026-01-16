# Session 2026-01-15: TradeLeg symbol Bugfix + Delta Imbalance Function

## Issues Found and Fixed

### Issue 1: TradeLeg symbol attribute missing (12x Wiederholung)
**File**: `src/funding_bot/services/positions/exit_eval.py:355-356`

**Problem**: Code accessed `trade.leg1.symbol` and `trade.leg2.symbol`, but `TradeLeg` has no `symbol` field.

**Fix**: Changed to `trade.symbol` (both legs share the same symbol).

```python
# Before:
leg1_position = await self.lighter.get_position(trade.leg1.symbol)  # ❌
leg2_position = await self.x10.get_position(trade.leg2.symbol)      # ❌

# After:
leg1_position = await self.lighter.get_position(trade.symbol)       # ✅
leg2_position = await self.x10.get_position(trade.symbol)           # ✅
```

### Issue 2: log_delta_imbalance Import Error
**File**: `src/funding_bot/services/positions/imbalance.py`

**Problem**: `exit_eval.py:418` imported `log_delta_imbalance` but function didn't exist.

**Fix**: Created new function that:
- Takes Position objects and mark prices as parameters
- Calculates delta drift (direction-aware notional values)
- Logs WARNING if delta drift exceeds 3% threshold

## External Research Findings

### Exchange Position API Differences
| Exchange | Field Name |
|----------|------------|
| Lighter | `symbol: "BTC-USD"` |
| X10/Extended | `market: "BTC-USD"` |

### Orderbook Depth Limit
- **X10/Extended REST**: No limit parameter for orderbook snapshots
- **X10/Extended WS**: `depth=1` for L1 only (10ms updates)
- Full snapshot + client-side slicing is correct fallback

### Ghost Fill Detection
- Working as designed! The bot correctly detects when Position API shows fills that Order API hasn't reported yet
- This is expected behavior due to async nature of exchange APIs

## Test Results
- ✅ 302 unit tests passed
- ✅ 83 position tests passed
- ⚠️ 6 AsyncMock warnings (pre-existing, unrelated)

## Files Modified
1. `src/funding_bot/services/positions/exit_eval.py` - Fixed leg1.symbol/leg2.symbol
2. `src/funding_bot/services/positions/imbalance.py` - Added log_delta_imbalance function + imports