# Quick Reference: Lighter API Decimal Fields

## API Response Fields (from `/api/v1/orderBookDetails`)

```json
{
  "order_book_details": [
    {
      "symbol": "APT",
      "market_id": 31,

      // ❌ DISPLAY DECIMALS (for UI only, DON'T use for scaling)
      "size_decimals": 8,
      "price_decimals": 6,

      // ✅ ACTUAL DECIMALS (use these for API scaling)
      "supported_size_decimals": 4,
      "supported_price_decimals": 2,

      // Human-readable minimums (NOT scaled)
      "min_base_amount": "0.001",
      "min_quote_amount": "10"
    }
  ]
}
```

## Scaling Formula

```python
# Get ACTUAL decimals from API
size_decimals = supported_size_decimals  # e.g., 4 for APT
price_decimals = supported_price_decimals  # e.g., 2 for APT

# Calculate scales
base_scale = 10 ** size_decimals    # e.g., 10^4 = 10,000
quote_scale = 10 ** price_decimals  # e.g., 10^2 = 100

# Scale human-readable values to integers
scaled_base = int(qty * base_scale)      # 0.01 * 10,000 = 100
scaled_price = int(price * quote_scale)  # 10.50 * 100 = 1050
```

## Common Markets

| Market  | Supported Size Decimals | Supported Price Decimals | Base Scale  | Quote Scale |
| ------- | ----------------------- | ------------------------ | ----------- | ----------- |
| BTC-USD | 8                       | 2                        | 100,000,000 | 100         |
| ETH-USD | 4                       | 2                        | 10,000      | 100         |
| APT-USD | 4                       | 2                        | 10,000      | 100         |
| SOL-USD | 4                       | 2                        | 10,000      | 100         |

**Note**: Always fetch from API - don't hardcode these values!

## Code Template

```python
# 1. Fetch market info from API
details = await order_api.order_book_details(market_id=market_id)
m = details.order_book_details[0]

# 2. Extract ACTUAL decimals (not display decimals)
size_decimals_actual = int(m.supported_size_decimals)  # ✅
price_decimals_actual = int(m.supported_price_decimals)  # ✅

# 3. Calculate scales
base_scale = Decimal(10) ** size_decimals_actual
quote_scale = Decimal(10) ** price_decimals_actual

# 4. Scale your order
qty = Decimal("0.01")  # 0.01 APT
price = Decimal("10.50")  # $10.50

scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_UP))

# 5. Send order
await signer.create_order(
    market_index=market_id,
    base_amount=scaled_base,  # 100
    price=scaled_price,       # 1050
    # ...
)
```

## Validation Checklist

Before sending order:

- [ ] Used `supported_size_decimals` (NOT `size_decimals`)
- [ ] Used `supported_price_decimals` (NOT `price_decimals`)
- [ ] Checked `scaled_base > 0`
- [ ] Checked `scaled_price > 0`
- [ ] Verified minimum notional (~$10 USD)
- [ ] Added 5% buffer to minimums

## Common Mistakes

### ❌ WRONG

```python
# Using display decimals
size_decimals = m.size_decimals  # ❌ Display only!
price_decimals = m.price_decimals  # ❌ Display only!

# Result: 10,000x too large for APT
scaled_base = int(0.01 * 10^8) = 1,000,000  # ❌ REJECTED!
```

### ✅ CORRECT

```python
# Using supported decimals
size_decimals = m.supported_size_decimals  # ✅ Actual scaling
price_decimals = m.supported_price_decimals  # ✅ Actual scaling

# Result: Correct scaling for APT
scaled_base = int(0.01 * 10^4) = 100  # ✅ ACCEPTED!
```

## Debug Logging

Add this to verify correct decimals:

```python
logger.info(
    f"{symbol} Decimals:\n"
    f"  Display: size={m.size_decimals}, price={m.price_decimals}\n"
    f"  Actual: size={m.supported_size_decimals}, price={m.supported_price_decimals}\n"
    f"  Scales: base={10**m.supported_size_decimals}, quote={10**m.supported_price_decimals}\n"
    f"  Example: 0.01 @ $10.50 → base={int(0.01 * 10**m.supported_size_decimals)}, "
    f"price={int(10.50 * 10**m.supported_price_decimals)}"
)
```

## References

- [Lighter TypeScript SDK](https://github.com/Bvvvp009/lighter-ts)
- API Docs: `/api/v1/orderBookDetails`
- Your adapter: `src/adapters/lighter_adapter.py`
