# Liquidation API Research - 2026-01-15

## Investigation Summary

**User Request:** "prüfe wirklich ob die beiden börsen keine liquidations api bereitstellen, prüfe das in external_repos oder im internet"

## Key Findings

### ✅ BOTH EXCHANGES PROVIDE LIQUIDATION PRICE API

#### Lighter Exchange
- **Source:** `apidocs.lighter.xyz` → WebSocket Position JSON
- **Field:** `liquidation_price` (snake_case in WebSocket)
- **Example:** `"liquidation_price": "3024.66"`
- **Docs:** https://apidocs.lighter.xyz/docs/websocket-reference

```json
Position = {
  "market_id": INTEGER,
  "symbol": STRING,
  "position": STRING,
  "avg_entry_price": STRING,
  "mark_price": STRING,
  "liquidation_price": "3024.66",  // <-- Available!
  "unrealized_pnl": STRING,
  ...
}
```

**Adapter Mapping:** `src/funding_bot/adapters/exchanges/lighter/adapter.py:1524`
```python
liquidation_price=_safe_decimal(getattr(p, "liquidation_price", 0)) or None,
```

#### Extended/X10 Exchange
- **Source:** `api.docs.extended.exchange` → GET /api/v1/user/positions
- **Field:** `liquidationPrice` (camelCase in API)
- **Example:** `"liquidationPrice": "38200"`
- **Docs:** https://api.docs.extended.exchange/

```json
{
  "market": "BTC-USD",
  "side": "LONG",
  "leverage": "10",
  "size": "0.1",
  "value": "4000",
  "openPrice": "39000",
  "markPrice": "40000",
  "liquidationPrice": "38200",  // <-- Available!
  "margin": "20",
  "unrealisedPnl": "1000",
  ...
}
```

**API Documentation Table:**
| Field | Type | Description |
|-------|------|-------------|
| `data[].liquidationPrice` | string | Position's liquidation price |

**Adapter Mapping:** `src/funding_bot/adapters/exchanges/x10/adapter.py:1217`
```python
liquidation_price=_safe_decimal(getattr(p, "liquidation_price", 0)) or None,
```

## Problem Identified: SDK Mapping Issue

The warning "X10 liquidation_price missing for EDEN" is **NOT an API limitation** - it's an **SDK attribute mapping issue**.

### SDK Field Mapping Pattern

The X10/Extended SDK converts API camelCase → Python snake_case:
- `openPrice` → `open_price` ✅
- `markPrice` → `mark_price` ✅
- `unrealisedPnl` → `unrealised_pnl` ✅ (British spelling preserved)
- `liquidationPrice` → `liquidation_price` ❌ **Likely not mapped!**

### Possible Root Causes

1. **SDK Bug:** The `liquidationPrice` field is not exposed by the SDK at all
2. **Different Attribute Name:** SDK might use:
   - `liq_price`
   - `liquidation`
   - `liquidationprice`
3. **Optional Field:** SDK only includes it when non-null (but API docs say "yes"/required)

## Next Steps

### Option 1: Live Debug (Recommended)
Run a live debug session to inspect actual SDK Position object attributes:
```python
positions = await x10.list_positions()
for p in positions:
    print(dir(p))  # List all available attributes
```

### Option 2: Adapter Enhancement
Add fallback logic to try multiple attribute names:
```python
liq_price = (
    getattr(p, "liquidation_price", None) or
    getattr(p, "liq_price", None) or
    getattr(p, "liquidation", None) or
    None
)
```

### Option 3: Direct API Call
Bypass SDK and call REST API directly for position data to verify field presence.

## Settings Status

**Current:** `liquidation_distance_monitoring_enabled: bool = False` (line 196 in settings.py)

The liquidation distance monitoring feature is disabled because of this suspected API limitation - but **the limitation doesn't exist!** Once the SDK mapping is fixed, this feature can be enabled.

## References

- Lighter WebSocket Docs: `external_repos/lighter/apidocs.lighter.xyz_docs_websocket-reference.md`
- Extended API Docs: `external_repos/extended/api.docs.extended.exchange_#extended-api-documentation.md`
- X10 Adapter: `src/funding_bot/adapters/exchanges/x10/adapter.py:1217`
- Lighter Adapter: `src/funding_bot/adapters/exchanges/lighter/adapter.py:1524`
