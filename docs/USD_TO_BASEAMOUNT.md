# USD to BaseAmount Conversion

## Overview

Die `usd_to_base_amount()` Funktion konvertiert USD-Beträge in die korrekt formatierten `baseAmount` Einheiten für die Lighter API.

## Problem

Lighter verwendet **ganzzahlige Einheiten** mit marktspezifischen Skalierungen:

- **APT-USD**: 1 APT = 10,000 units (4 Dezimalstellen)
- **ETH-USD**: 1 ETH = 100,000,000 units (8 Dezimalstellen)
- **BTC-USD**: 1 BTC = 100,000,000 units (8 Dezimalstellen)

## Lösung

### Funktion Signatur

```python
def usd_to_base_amount(
    self,
    symbol: str,
    usd_amount: float,
    current_price: Optional[float] = None
) -> int:
```

### Parameter

| Parameter       | Typ     | Beschreibung               | Beispiel    |
| --------------- | ------- | -------------------------- | ----------- |
| `symbol`        | `str`   | Market Symbol              | `"APT-USD"` |
| `usd_amount`    | `float` | USD Betrag                 | `14.36`     |
| `current_price` | `float` | Preis pro Token (optional) | `10.20`     |

### Return Value

`int` - Skalierter `baseAmount` für die Lighter API

### Beispiele

#### Beispiel 1: APT-USD Order ($14.36)

```python
from src.adapters.lighter_adapter import LighterAdapter

adapter = LighterAdapter()
await adapter.load_market_cache()

# Order $14.36 worth of APT at $10.20 per APT
base_amount = adapter.usd_to_base_amount("APT-USD", 14.36, 10.20)
# Returns: 14080 units

# Verification:
# - Quantity: 14.36 / 10.20 = 1.408 APT
# - APT uses 4 decimals: 1 APT = 10,000 units
# - Scaled: 1.408 * 10,000 = 14,080 units ✅
```

#### Beispiel 2: ETH-USD Order ($100)

```python
# Order $100 worth of ETH at $3,500 per ETH
base_amount = adapter.usd_to_base_amount("ETH-USD", 100.00, 3500.00)
# Returns: 2857143 units

# Verification:
# - Quantity: 100 / 3500 = 0.02857143 ETH
# - ETH uses 8 decimals: 1 ETH = 100,000,000 units
# - Scaled: 0.02857143 * 100,000,000 = 2,857,143 units ✅
```

#### Beispiel 3: Automatischer Preis-Fetch

```python
# Price wird automatisch aus Cache geholt
base_amount = adapter.usd_to_base_amount("APT-USD", 20.00)
# Verwendet cached price von adapter.price_cache
```

## Funktionsweise

### 1. Market Configuration Laden

```python
market_data = self.market_info.get(symbol)
# Enthält: size_decimals, price_decimals, min_base_amount
```

### 2. Preis Ermitteln

```python
if current_price is None:
    current_price = self.get_price(symbol)
```

### 3. Quantity Berechnen

```python
quantity = usd_amount / current_price
# Beispiel: $14.36 / $10.20 = 1.408 APT
```

### 4. Scaling Parameter Extrahieren

```python
size_decimals = market_data.get('sd')  # z.B. 4 für APT
base_scale = 10 ** size_decimals        # z.B. 10,000 für APT
```

### 5. Minimum Order Size Validierung

```python
if quantity < min_base_amount:
    quantity = min_base_amount * 1.05  # 5% Buffer
```

### 6. Integer Scaling

```python
scaled_base = int((quantity * base_scale).quantize(Decimal('1'), ROUND_UP))
# Beispiel: 1.408 * 10,000 = 14,080 units
```

## API Integration

### Komplettes Order Beispiel

```python
import asyncio
from decimal import Decimal
from src.adapters.lighter_adapter import LighterAdapter

async def place_order():
    adapter = LighterAdapter()
    await adapter.load_market_cache()

    symbol = "APT-USD"
    usd_size = 14.36

    # 1. Convert USD to baseAmount
    current_price = adapter.get_price(symbol)
    base_amount = adapter.usd_to_base_amount(symbol, usd_size, current_price)

    # 2. Calculate limit price with slippage
    slippage = Decimal("0.005")  # 0.5%
    limit_price = Decimal(str(current_price)) * (Decimal("1") + slippage)

    # 3. Scale price to API units
    market_data = adapter.market_info[symbol]
    price_decimals = market_data.get('pd')
    price_scaled = int(float(limit_price) * (10 ** price_decimals))

    # 4. Create order
    success, order_id = await adapter.open_live_position(
        symbol=symbol,
        side="BUY",
        notional_usd=usd_size,
        price=float(limit_price)
    )

    print(f"Order placed: {order_id}")

asyncio.run(place_order())
```

## Fehlerbehandlung

### ValueError: Market Not Found

```python
try:
    base_amount = adapter.usd_to_base_amount("UNKNOWN-USD", 10.00)
except ValueError as e:
    print(f"Error: {e}")
    # Output: "❌ Market UNKNOWN-USD not found. Call load_market_cache() first."
```

### ValueError: No Price Available

```python
try:
    base_amount = adapter.usd_to_base_amount("APT-USD", 10.00, None)
    # Falls kein Cache-Preis verfügbar
except ValueError as e:
    print(f"Error: {e}")
    # Output: "❌ No valid price for APT-USD"
```

### ValueError: Amount Too Small

```python
# Automatische Anpassung bei zu kleinen Beträgen
base_amount = adapter.usd_to_base_amount("APT-USD", 0.01, 10.00)
# Warnung: "⚠️ APT-USD: USD amount $0.01 too small. Increased to min..."
```

## Validierung

### Pre-Order Validation

```python
# Validiere Parameter VOR dem Order
is_valid, error = await adapter.validate_order_params(symbol, usd_size)
if not is_valid:
    print(f"Order validation failed: {error}")
else:
    base_amount = adapter.usd_to_base_amount(symbol, usd_size)
    # Proceed with order...
```

## Logging

Die Funktion loggt detaillierte Informationen:

```
💱 APT-USD USD → baseAmount:
   Input: $14.36 @ $10.200000
   Calculated: 1.40784314 tokens
   Scaled: 14080 units (size_decimals=4)
   Actual: 1.40800000 tokens = $14.36
```

## Testing

Führe das Test-Script aus:

```powershell
python test_usd_conversion.py
```

Output zeigt:

1. USD → baseAmount Konvertierung für verschiedene Markets
2. Validierung der Berechnungen
3. Kompletten Order-Flow

## Vergleich: TypeScript SDK

Die Python-Implementierung folgt exakt der TypeScript SDK Logik:

### TypeScript (offizielles SDK)

```typescript
// From lighter-ts/src/utils/price-utils.ts
export async function amountToUnits(
  amount: number,
  marketIndex: number,
  orderApi?: OrderApi
): Promise<number> {
  const market = await getMarketConfig(marketIndex, orderApi);
  return Math.round(amount * market.baseScale);
}
```

### Python (unsere Implementierung)

```python
# Equivalent in lighter_adapter.py
def usd_to_base_amount(self, symbol: str, usd_amount: float,
                       current_price: Optional[float] = None) -> int:
    quantity = Decimal(str(usd_amount)) / Decimal(str(current_price))
    base_scale = Decimal(10) ** size_decimals
    return int((quantity * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
```

## Best Practices

### ✅ DO

- **Immer `load_market_cache()` aufrufen** vor der Nutzung
- **USD-Beträge > $10** verwenden (Lighter Minimum)
- **Slippage hinzufügen** bei Limit Orders (0.3-1%)
- **Validierung vor Order** mit `validate_order_params()`

### ❌ DON'T

- **Nicht manuell skalieren** - nutze die Funktion
- **Nicht Price = None** ohne Cache-Preis
- **Nicht USD < $5** - zu klein für meiste Markets
- **Nicht hardcoded decimals** - nutze API-Werte

## Troubleshooting

### Problem: "Scaled baseAmount is 0"

**Ursache:** USD-Betrag zu klein oder Preis zu hoch

**Lösung:**

```python
# Erhöhe USD-Betrag oder prüfe Preis
min_notional = adapter.min_notional_usd(symbol)
if usd_amount < min_notional:
    usd_amount = min_notional * 1.1
```

### Problem: "Market not found"

**Ursache:** Market Cache nicht geladen

**Lösung:**

```python
await adapter.load_market_cache(force=True)
```

### Problem: "No valid price"

**Ursache:** Kein cached Price, API nicht erreichbar

**Lösung:**

```python
# Manuell Preis übergeben
base_amount = adapter.usd_to_base_amount(symbol, usd_amount, 10.20)
```

## Weiterführende Dokumentation

- [Lighter API Docs](https://docs.lighter.xyz)
- [lighter-ts SDK](https://github.com/Bvvvp009/lighter-ts)
- [MarketHelper Documentation](https://github.com/Bvvvp009/lighter-ts/blob/main/docs/MarketHelper.md)
