# Lighter Order Fix - Applied Changes

## Problem

Lighter orders failed with "invalid order base or quote amount" error while X10 orders worked fine.

## Root Cause

**Wrong decimal fields used for scaling**:

- Used `size_decimals` (display decimals = 8) instead of `supported_size_decimals` (actual = 4 for APT)
- Used `price_decimals` (display decimals = 6) instead of `supported_price_decimals` (actual = 2 for APT)

This caused orders to be scaled by 10,000x too large!

## Solution Applied

### 1. Updated `_fetch_single_market()` Method

**Before (WRONG)**:

```python
size_decimals = safe_int(getattr(m, 'size_decimals', None), 8)  # ❌ Display only
price_decimals = safe_int(getattr(m, 'price_decimals', None), 6)  # ❌ Display only

market_data = {
    'sd': size_decimals,  # ❌ Wrong field!
    'pd': price_decimals,  # ❌ Wrong field!
}
```

**After (CORRECT)**:

```python
# ✅ Use supported_* fields (actual scaling decimals)
size_decimals_actual = safe_int(getattr(m, 'supported_size_decimals', None), 4)
price_decimals_actual = safe_int(getattr(m, 'supported_price_decimals', None), 2)

# Display decimals (for logging/UI only)
size_decimals_display = safe_int(getattr(m, 'size_decimals', None), 8)
price_decimals_display = safe_int(getattr(m, 'price_decimals', None), 6)

market_data = {
    'sd': size_decimals_actual,  # ✅ Correct for scaling
    'pd': price_decimals_actual,  # ✅ Correct for scaling
    'display_sd': size_decimals_display,  # For reference
    'display_pd': price_decimals_display,  # For reference
}
```

### 2. Updated `load_market_cache()` REST API Loading

Applied same fix to REST API market loading to ensure consistency.

## Example: APT-USD Order

### Before Fix (FAILED)

```python
# Input
qty = 0.01 APT
price = $10.50

# Wrong calculation (using size_decimals=8)
scaled_base = int(0.01 * 10^8) = 1,000,000  # ❌ 10,000x too large!
scaled_price = int(10.50 * 10^6) = 10,500,000

# API rejected: "invalid order base or quote amount"
```

### After Fix (SUCCESS)

```python
# Input
qty = 0.01 APT
price = $10.50

# Correct calculation (using supported_size_decimals=4)
scaled_base = int(0.01 * 10^4) = 100  # ✅ Correct!
scaled_price = int(10.50 * 10^2) = 1050  # ✅ Correct!

# API accepts order
```

## Why X10 Worked

X10 SDK handles all scaling internally:

```python
# X10: Just pass human-readable values
resp = await client.place_order(
    amount_of_synthetic=qty,  # 0.01 (human format)
    price=limit_price,        # 10.50 (human format)
    # SDK scales automatically
)
```

Lighter requires manual scaling with EXACT decimals from API.

## Verification Checklist

- [x] Updated `_fetch_single_market()` to use `supported_size_decimals`
- [x] Updated `_fetch_single_market()` to use `supported_price_decimals`
- [x] Updated `load_market_cache()` REST endpoint parsing
- [x] Added display decimals for reference
- [x] Added debug logging for decimal validation
- [x] Preserved existing `_scale_amounts()` logic (it uses `sd` and `pd` from market_info)

## Testing

Test with small orders:

```python
# Test APT-USD
symbol = "APT-USD"
notional_usd = 15.0  # $15 order

# Should now work with correct scaling:
# - supported_size_decimals=4 → base_scale=10,000
# - supported_price_decimals=2 → quote_scale=100
```

## Next Steps

1. **Test on Testnet** with various markets (BTC, ETH, APT, etc.)
2. **Monitor logs** for decimal validation messages
3. **Verify filled orders** match expected sizes
4. **Compare fees** with X10 to ensure correct execution

## Files Modified

1. `src/adapters/lighter_adapter.py`:
   - Line ~476: `_fetch_single_market()` method
   - Line ~735: `load_market_cache()` method

## Reference Documents

- `LIGHTER_VS_X10_ORDER_COMPARISON.md`: Full comparison analysis
- [Lighter TypeScript SDK](https://github.com/Bvvvp009/lighter-ts): Reference implementation

## Key Takeaway

**Always use `supported_size_decimals` and `supported_price_decimals` for Lighter API scaling, NOT `size_decimals` and `price_decimals`.**

The latter are display-only fields and will cause orders to be scaled incorrectly, leading to rejections.
