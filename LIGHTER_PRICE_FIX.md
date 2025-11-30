# Lighter Price Loading Fix

## Problem

Bot was showing "Lighter REST: Loaded 0 prices" and many markets had None prices (ARB-USD, MON-USD, ETH-USD, etc.)

## Root Causes Found

### 1. Wrong REST Endpoint

- **Old Code**: Tried to use `/api/v1/ticker` endpoint
- **Problem**: This endpoint doesn't exist in the Lighter SDK
- **Fix**: Use `/api/v1/orderBooks` endpoint instead

### 2. Wrong Field Name

- **Old Code**: Tried multiple field names: `lastTradePrice`, `markPrice`, etc.
- **Problem**: API uses snake_case, not camelCase
- **Fix**: Use `last_trade_price` (confirmed from lighter-ts SDK)

### 3. WebSocket Handler

- **Old Code**: Used `mark_price` as primary field
- **Problem**: Should prioritize `last_trade_price` field
- **Fix**: Check `last_trade_price` first, then fallback to `mark_price`

## Changes Made

### 1. Fixed `_rest_price_poller()` (lighter_adapter.py)

```python
# BEFORE: Wrong field names
for field in ["last_trade_price", "lastTradePrice", "mark_price", "markPrice"]:
    if field in m and m[field]:
        try:
            price = float(m[field])
            break
        except (ValueError, TypeError):
            continue

# AFTER: Correct field name only
price = m.get("last_trade_price")
if price and price > 0:
    try:
        self.price_cache[symbol] = float(price)
        updated += 1
    except (ValueError, TypeError):
        continue
```

### 2. Fixed `load_market_cache()` (lighter_adapter.py)

```python
# AFTER: Extract last_trade_price during market loading
if last_price:
    try:
        price_float = float(last_price)
        if price_float > 0:
            self.price_cache[symbol] = price_float
            logger.debug(f"{symbol}: Cached price ${price_float:.2f} from last_trade_price")
    except (ValueError, TypeError):
        pass
```

### 3. Removed `/ticker` Endpoint (lighter_adapter.py)

```python
# BEFORE: Used non-existent /ticker endpoint
async with session.get(f"{base_url}/api/v1/ticker", timeout=...) as resp:
    # ... 40+ lines of code trying to parse ticker data

# AFTER: Only use _rest_price_poller for prices
async def load_funding_rates_and_prices(self):
    """Load Funding Rates from Lighter REST API (prices handled by _rest_price_poller)"""
    await self._load_funding_rates()
```

### 4. Fixed WebSocket Handler (websocket_manager.py)

```python
# BEFORE: Only checked mark_price
mark_price = stats.get("mark_price") or stats.get("markPrice")
if mark_price:
    self.lighter_adapter.price_cache[symbol] = float(mark_price)

# AFTER: Prioritize last_trade_price
price = stats.get("last_trade_price") or stats.get("mark_price") or stats.get("markPrice")
if price:
    self.lighter_adapter.price_cache[symbol] = float(price)
```

## API Response Structure (Confirmed from lighter-ts SDK)

### `/api/v1/orderBooks` Response

```json
{
  "code": 200,
  "order_books": [
    {
      "symbol": "ETH-USD",
      "market_id": 0,
      "last_trade_price": 3000.50,  // ← NUMBER type, extract this!
      "daily_price_high": 3100,
      "daily_price_low": 2900,
      "size_decimals": 8,
      "price_decimals": 6,
      ...
    }
  ]
}
```

### WebSocket `market_stats` Message

```json
{
  "type": "update/market_stats",
  "channel": "market_stats:0",
  "market_stats": {
    "market_id": 0,
    "last_trade_price": 3000.50,  // ← Primary price field
    "mark_price": "3000.55",
    "index_price": "3000.45",
    "current_funding_rate": "0.0001",
    "open_interest": 1000000,
    ...
  }
}
```

## Expected Results

After these fixes:

1. ✅ **REST API**: Should load all 70 prices from `/orderBooks` endpoint
2. ✅ **WebSocket**: Should receive price updates via `market_stats/{market_id}` subscriptions
3. ✅ **Cache**: `price_cache` should show 70/70 markets with valid prices
4. ✅ **Logs**: Should see "Lighter REST: Loaded 70 prices" (not 0)

## Testing

Run the bot and check logs:

```bash
# Should see:
✅ Lighter: 70 markets loaded (REST API)
✅ Lighter REST: Loaded 70 prices
✅ ETH-USD: Cached price $3000.50 from last_trade_price

# Should NOT see:
❌ Lighter REST: Loaded 0 prices
❌ ARB-USD: None prices
```

## References

- lighter-ts SDK: https://github.com/Bvvvp009/lighter-ts
- OrderBookDetail interface: `src/api/order-api.ts`
- MarketHelper price field: `src/utils/market-helper.ts` (line 99: `get lastPrice()`)
- WebSocket docs: `docs/WsClient.md` (line 252: market_stats message format)
