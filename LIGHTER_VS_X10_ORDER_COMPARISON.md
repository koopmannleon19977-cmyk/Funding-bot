# Lighter vs X10 Order Creation Comparison

## Executive Summary

**Problem**: X10 orders work fine, Lighter orders fail with "invalid order base or quote amount"

**Root Cause**: Different decimal scaling approaches between exchanges

---

## 1. Order Size Calculation

### X10 Approach (WORKING) ✅

```python
# X10 uses SDK's built-in rounding functions
cfg = market.trading_config

# Get parameters from market config
step = Decimal(getattr(cfg, "min_order_size_change", "0"))
min_size = Decimal(getattr(cfg, "min_order_size", "0"))

# Calculate quantity
qty = Decimal(str(notional_usd)) / limit_price

# Round to step size
if step > 0:
    qty = (qty // step) * step
    if qty < min_size:
        qty = ((qty // step) + 1) * step

# Pass HUMAN-READABLE values to SDK
resp = await client.place_order(
    market_name=symbol,
    amount_of_synthetic=qty,        # e.g., 0.01 BTC
    price=limit_price,              # e.g., 61850.5 USD
    side=order_side,
    # ...
)
```

**Key Point**: X10 SDK handles all scaling internally. You pass human-readable decimal values.

---

### Lighter Approach (FAILING) ❌

```python
# Your current code - WRONG SCALING
def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
    data = self.market_info.get(symbol)

    size_decimals = safe_int(data.get('sd'), 8)
    price_decimals = safe_int(data.get('pd'), 6)

    base_scale = Decimal(10) ** size_decimals
    quote_scale = Decimal(10) ** price_decimals

    # THIS IS WHERE IT BREAKS - scaling is inconsistent
    scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
    scaled_price = int((price * quote_scale).quantize(...))

    return scaled_base, scaled_price
```

**Problem**:

- You're applying **wrong decimals** from API
- API's `size_decimals=8` for APT means display decimals, NOT scaling factor
- Your calculation: `0.01 * 10^8 = 1,000,000` (correct for BTC)
- But for APT: should be `0.01 * 10^4 = 100` (API uses 4 decimals internally)

---

## 2. Price Formatting & Precision

### X10: SDK Handles Everything

```python
# X10's SDK does price rounding automatically
if hasattr(cfg, "round_price") and callable(cfg.round_price):
    limit_price = cfg.round_price(raw_price)
else:
    tick_size = Decimal(getattr(cfg, "min_price_change", "0.01"))
    if side == "BUY":
        limit_price = ((raw_price + tick_size - Decimal('1e-12')) // tick_size) * tick_size
    else:
        limit_price = (raw_price // tick_size) * tick_size
```

### Lighter: Manual Scaling Required

```typescript
// From lighter-ts SDK (TypeScript)
const baseScale = Math.pow(10, size_decimals);
const quoteScale = Math.pow(10, price_decimals);

// CRITICAL: Use the EXACT values from API response
baseAmount = Math.round(amount * baseScale);
priceAmount = Math.round(price * quoteScale);
```

---

## 3. Market Info Fetching

### X10: All-In-One

```python
await client.markets_info.get_markets()
# Returns complete TradingConfig object with:
# - min_order_size
# - min_order_size_change (step size)
# - min_price_change (tick size)
# - round_price() helper function
```

### Lighter: Manual API Call

```python
# Endpoint: GET /api/v1/orderBookDetails?market_id={id}
response = {
    "order_book_details": [{
        "size_decimals": 8,          # Display decimals
        "price_decimals": 6,         # Display decimals
        "min_base_amount": "0.001",  # String (not scaled)
        "min_quote_amount": "10",    # String (not scaled)
        "supported_size_decimals": 4, # ACTUAL precision used
        "supported_price_decimals": 2 # ACTUAL precision used
    }]
}
```

**CRITICAL**: You need `supported_size_decimals`, not `size_decimals`!

---

## 4. The Fix for Lighter

### Step 1: Get Correct Decimals from API

```python
async def _fetch_single_market(self, order_api, market_id: int):
    details = await order_api.order_book_details(market_id=market_id)
    m = details.order_book_details[0]

    # ⚠️ WRONG - These are display decimals
    # size_decimals = int(m.size_decimals)
    # price_decimals = int(m.price_decimals)

    # ✅ CORRECT - Use supported_* fields
    size_decimals = int(m.supported_size_decimals)    # e.g., 4 for APT
    price_decimals = int(m.supported_price_decimals)  # e.g., 2 for APT

    # These are already in human format (not scaled)
    min_base = Decimal(str(m.min_base_amount))       # e.g., "0.001"
    min_quote = Decimal(str(m.min_quote_amount))     # e.g., "10"
```

### Step 2: Scale Correctly (EXACT SDK Logic)

```python
def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
    """
    EXACT REPLICATION of lighter-ts SDK scaling logic:
    https://github.com/Bvvvp009/lighter-ts/blob/main/src/utils/price-utils.ts
    """
    data = self.market_info.get(symbol)

    # ✅ Use supported_* fields (actual precision)
    size_decimals = int(data.get('size_decimals_actual', 4))
    price_decimals = int(data.get('price_decimals_actual', 2))

    # Calculate scales
    base_scale = Decimal(10) ** size_decimals
    quote_scale = Decimal(10) ** price_decimals

    # Get minimums (already in human format)
    min_base_amount = data.get('min_base_amount')  # Decimal("0.001")

    # Ensure qty meets minimum
    if qty < min_base_amount:
        qty = min_base_amount * Decimal("1.05")  # 5% buffer

    # Scale to integers (EXACT SDK formula)
    scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))

    if side == 'SELL':
        scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_DOWN))
    else:
        scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_UP))

    # Validation
    if scaled_base == 0 or scaled_price == 0:
        raise ValueError(f"Scaled values are zero! base={scaled_base}, price={scaled_price}")

    return scaled_base, scaled_price
```

### Step 3: Example Calculation

```python
# APT-USD Market
# API Response:
# - size_decimals = 8 (display only)
# - supported_size_decimals = 4 (actual)
# - min_base_amount = "0.001"

# Order: 0.01 APT @ $10.50
qty = Decimal("0.01")
price = Decimal("10.50")

# ❌ WRONG (your current code)
scaled_base = int(0.01 * 10^8) = 1,000,000  # WAY TOO LARGE!
scaled_price = int(10.50 * 10^6) = 10,500,000

# ✅ CORRECT (using supported_* fields)
scaled_base = int(0.01 * 10^4) = 100
scaled_price = int(10.50 * 10^2) = 1050

# Result:
{
    "BaseAmount": 100,         # 0.01 APT (correctly scaled)
    "Price": 1050             # $10.50 (correctly scaled)
}
```

---

## 5. Complete Working Solution

```python
class LighterAdapter(BaseAdapter):

    async def _fetch_single_market(self, order_api, market_id: int):
        """Fetch market with CORRECT decimal fields"""
        details = await order_api.order_book_details(market_id=market_id)
        m = details.order_book_details[0]

        symbol = m.symbol
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"

        # ✅ Use supported_* fields for scaling
        market_data = {
            'i': m.market_id,
            'symbol': symbol,

            # Display decimals (for UI only)
            'display_size_decimals': int(m.size_decimals),
            'display_price_decimals': int(m.price_decimals),

            # ACTUAL scaling decimals (for API)
            'size_decimals': int(m.supported_size_decimals),
            'price_decimals': int(m.supported_price_decimals),

            # Min amounts (already in human format)
            'min_base_amount': Decimal(str(m.min_base_amount)),
            'min_quote_amount': Decimal(str(m.min_quote_amount)),

            # For validation
            'min_notional': float(m.min_quote_amount) if m.min_quote_amount else 10.0,
        }

        self.market_info[symbol] = market_data

        # Cache price
        if m.last_trade_price:
            self.price_cache[symbol] = float(m.last_trade_price)

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        """
        Scale amounts using SUPPORTED decimals (not display decimals)
        Matches lighter-ts SDK exactly.
        """
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"Market {symbol} not found")

        # ✅ Critical: Use 'size_decimals' (mapped from supported_*)
        size_decimals = int(data.get('size_decimals', 4))
        price_decimals = int(data.get('price_decimals', 2))

        base_scale = Decimal(10) ** size_decimals
        quote_scale = Decimal(10) ** price_decimals

        # Get minimum base amount
        min_base = data.get('min_base_amount', Decimal("0.001"))

        # Ensure minimum notional (~$10)
        notional = qty * price
        if notional < Decimal("10.5"):
            qty = Decimal("10.5") / price

        # Ensure minimum quantity
        if qty < min_base:
            qty = min_base * Decimal("1.05")

        # Scale to integers
        scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))

        if side == 'SELL':
            scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_DOWN))
        else:
            scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_UP))

        # Validation
        if scaled_base == 0:
            raise ValueError(
                f"❌ {symbol}: Base amount is 0!\n"
                f"   qty={qty}, size_decimals={size_decimals}, base_scale={base_scale}"
            )

        if scaled_price == 0:
            raise ValueError(
                f"❌ {symbol}: Price is 0!\n"
                f"   price={price}, price_decimals={price_decimals}, quote_scale={quote_scale}"
            )

        logger.debug(
            f"📐 {symbol} Scaling:\n"
            f"   Input: {qty} @ ${price}\n"
            f"   Decimals: size={size_decimals}, price={price_decimals}\n"
            f"   Scales: base={base_scale}, quote={quote_scale}\n"
            f"   Output: base={scaled_base}, price={scaled_price}"
        )

        return scaled_base, scaled_price
```

---

## 6. Key Differences Summary

| Aspect              | X10 (Working)              | Lighter (Broken → Fixed)                      |
| ------------------- | -------------------------- | --------------------------------------------- |
| **SDK Handling**    | ✅ All automatic           | ❌ Manual scaling required                    |
| **Decimals Source** | `trading_config` object    | ⚠️ `supported_*` fields (not `size_decimals`) |
| **Value Format**    | Human-readable (0.01 BTC)  | Scaled integers (100 for APT)                 |
| **Min Amount**      | `min_order_size` (Decimal) | `min_base_amount` (String)                    |
| **Price Rounding**  | `round_price()` helper     | Manual with `quantize()`                      |
| **Step Size**       | `min_order_size_change`    | Implied by decimals                           |

---

## 7. Testing Your Fix

```python
# Test with APT-USD
symbol = "APT-USD"
notional_usd = 15.0  # $15 order
price = 10.50        # $10.50 per APT

# Expected:
qty = 15.0 / 10.50 = 1.4285714 APT

# With supported_size_decimals=4:
scaled_base = int(1.4285714 * 10000) = 14286
scaled_price = int(10.50 * 100) = 1050

# API should accept this order
```

---

## 8. Migration Checklist

- [ ] Update `_fetch_single_market()` to use `supported_size_decimals` and `supported_price_decimals`
- [ ] Rename fields in `market_info` dict to distinguish display vs actual decimals
- [ ] Update `_scale_amounts()` to use correct decimal fields
- [ ] Add validation for zero-scaled values
- [ ] Add debug logging for all scaling operations
- [ ] Test with small orders on multiple markets (BTC, ETH, APT, etc.)
- [ ] Monitor for "invalid order base or quote amount" errors
- [ ] Compare filled orders vs X10 to verify correctness

---

## References

- [Lighter TypeScript SDK - price-utils.ts](https://github.com/Bvvvp009/lighter-ts/blob/main/src/utils/price-utils.ts)
- [Lighter TypeScript SDK - market-helper.ts](https://github.com/Bvvvp009/lighter-ts/blob/main/src/utils/market-helper.ts)
- [Lighter TypeScript SDK - order-api.ts](https://github.com/Bvvvp009/lighter-ts/blob/main/src/api/order-api.ts)
- X10 Extended SDK (your working adapter)
