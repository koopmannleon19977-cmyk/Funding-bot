# Fehlende Features - Detaillierte Analyse

**Erstellt:** 2025-01-20  
**Zweck:** Vollständige Übersicht aller fehlenden SDK-Features und deren Nutzen für den Funding-Bot

---

## 📊 Übersicht

| Exchange      | Fehlende Features | Priority 1 | Priority 2 | Priority 3 | Implementiert |
| ------------- | ----------------- | ---------- | ---------- | ---------- | ------------- |
| **X10**       | 7 Features        | 4          | 1          | 1          | 7          |
| **Lighter**   | 6 Features        | 2          | 2          | 2          | 4          |
| **Gemeinsam** | 2 Features        | 2          | 0          | 0          | 0             |

**Fortschritt:** 11/15 Features implementiert (73.3%)

## Operational Fixes (2025-12-21)

- Lighter REST 429 mitigation: WS-aware REST skip, position cache TTL, and standard rate limiter aligned to ~1 req/s.

---

## 🔴 X10 (Extended-TS-SDK) - Fehlende Features

### 1. Mass Cancel Orders ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
OrderManagementModule.massCancel({
  orderIds?: number[];
  externalOrderIds?: string[];
  markets?: string[];
  cancelAll?: boolean;
})
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `mass_cancel_orders()` Methode hinzugefügt
- ✅ `cancel_all_orders()` optimiert um Mass Cancel zu nutzen (10x schneller)
- ✅ Einzelne Cancel-Operationen vorhanden (Fallback)

**Zweck:**

- Schnelles Schließen mehrerer Orders in einem API-Call
- Atomare Operation (alle oder keine)
- Reduzierte Latenz bei Shutdown

**Warum wir es brauchen:**

1. **Shutdown-Performance:**

   - Aktuell: 10 Orders = 10 API-Calls = ~5-10 Sekunden
   - Mit Mass Cancel: 10 Orders = 1 API-Call = ~0.5-1 Sekunde
   - **10x schnelleres Shutdown**

2. **Emergency Cleanup:**

   - Bei Fehlern müssen alle Orders schnell geschlossen werden
   - Mass Cancel ist atomar → keine Race Conditions

3. **Rate Limiting:**
   - 1 Call statt 10 Calls = weniger Rate-Limit-Probleme

**Implementierung:**

```python
async def mass_cancel_orders(
    self,
    order_ids: Optional[List[int]] = None,
    external_order_ids: Optional[List[str]] = None,
    markets: Optional[List[str]] = None,
    cancel_all: bool = False
) -> bool:
    """
    Cancel multiple orders in one API call (Mass Cancel).

    This is 10x faster than canceling orders individually and is atomic
    (all orders are canceled or none).
    """
    client = await self._get_auth_client()
    result = await client.orders.massCancel({
        'orderIds': order_ids,
        'externalOrderIds': external_order_ids,
        'markets': markets,
        'cancelAll': cancel_all
    })
    return result.success
```

**Optimierungen:**

- ✅ `cancel_all_orders()` nutzt jetzt automatisch Mass Cancel wenn möglich
- ✅ Fallback auf individuelle Cancels wenn Mass Cancel fehlschlägt
- ✅ Unterstützt: `order_ids`, `external_order_ids`, `markets`, `cancel_all`

**Impact:** 🔥 **HOCH** - ✅ **IMPLEMENTIERT** - 10x schnellere Shutdowns

---

### 2. Position History API ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
AccountModule.getPositionsHistory({
  marketNames?: string[];
  positionSide?: string;
  cursor?: number;
  limit?: number;
})
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `get_positions_history()` Methode hinzugefügt
- ✅ Unterstützt SDK-Methode und REST-API-Fallback
- ✅ Vollständige PnL-Breakdown-Unterstützung
- ✅ Nur aktuelle Positionen (`getPositions()`) - bereits vorhanden

**Zweck:**

- Vollständige Historie aller geschlossenen Positionen
- Backtesting und Performance-Analyse
- PnL-Tracking über Zeit

**Warum wir es brauchen:**

1. **Vollständige PnL-Analyse:**

   - Aktuell: Nur aktuelle Trades werden getrackt
   - Mit History: Alle historischen Trades für vollständige Analyse
   - **Bessere Performance-Metriken**

2. **Backtesting:**

   - Historische Daten für Strategie-Optimierung
   - Vergleich verschiedener Strategien

3. **Compliance & Reporting:**

   - Vollständige Trade-Historie für Steuern/Reporting
   - Audit-Trail

4. **Debugging:**
   - Nachvollziehen, warum bestimmte Trades geschlossen wurden
   - Analyse von Exit-Bedingungen

**Implementierung:**

```python
async def get_positions_history(
    self,
    symbol: Optional[str] = None,
    position_side: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get historical positions (closed positions).

    Returns a list of all closed positions with their PnL, entry/exit prices,
    and other details. Useful for:
    - Performance analysis
    - Backtesting
    - Compliance & reporting
    - Debugging trade exits
    """
    # Tries SDK method first, falls back to REST API
    # Returns list of position dicts with full details
```

**Features:**

- ✅ SDK-Methode mit automatischem REST-API-Fallback
- ✅ Unterstützt Filterung nach Symbol und Position Side
- ✅ Pagination mit Cursor-Support
- ✅ Vollständige PnL-Breakdown-Unterstützung (tradePnl, fundingFees, etc.)
- ✅ Robuste Fehlerbehandlung

**Impact:** 🔥 **HOCH** - ✅ **IMPLEMENTIERT** - Vollständige Analytics & Debugging

---

### 3. Orders History API ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
AccountModule.getOrdersHistory({
  marketNames?: string[];
  orderType?: OrderType;
  orderSide?: OrderSide;
  cursor?: number;
  limit?: number;
})
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `get_orders_history()` Methode hinzugefügt
- ✅ Unterstützt SDK-Methode und REST-API-Fallback
- ✅ Automatische Slippage- und Fill-Rate-Berechnung
- ✅ Nur aktuelle Orders (`getOpenOrders()`) - bereits vorhanden

**Zweck:**

- Vollständige Historie aller Orders (erfolgreich, fehlgeschlagen, gecancelt)
- Performance-Analyse (Fill-Rate, Slippage)
- Fee-Tracking

**Warum wir es brauchen:**

1. **Fill-Rate-Analyse:**

   - Wie viele Orders wurden erfolgreich gefüllt?
   - Welche Orders wurden gecancelt/abgelehnt?
   - **Optimierung der Order-Strategie**

2. **Fee-Tracking:**

   - Vollständige Fee-Historie für genaue PnL-Berechnung
   - Vergleich tatsächlicher vs. erwarteter Fees

3. **Order-Performance:**

   - Durchschnittliche Fill-Zeit
   - Slippage-Analyse
   - Maker vs. Taker Performance

4. **Debugging:**
   - Nachvollziehen, warum Orders fehlgeschlagen sind
   - Analyse von Reject-Reasons

**Implementierung:**

```python
async def get_orders_history(
    self,
    symbol: Optional[str] = None,
    order_type: Optional[str] = None,
    order_side: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get historical orders (all orders: filled, cancelled, rejected).

    Returns a list of all historical orders with their status, fill information,
    fees, and other details. Useful for:
    - Fill-rate analysis
    - Fee tracking
    - Order performance (fill time, slippage)
    - Debugging
    """
    # Tries SDK method first, falls back to REST API
    # Returns list of order dicts with full details including:
    # - Fill percentage, slippage, fees, timestamps
```

**Features:**

- ✅ SDK-Methode mit automatischem REST-API-Fallback
- ✅ Unterstützt Filterung nach Symbol, Order Type, Order Side
- ✅ Pagination mit Cursor-Support
- ✅ Automatische Berechnung von Fill-Rate, Slippage, Fill-Percentage
- ✅ Vollständige Order-Details (Status, Fees, Timestamps, Reject Reasons)
- ✅ Robuste Fehlerbehandlung

**Impact:** 🔥 **HOCH** - ✅ **IMPLEMENTIERT** - Performance-Optimierung & Analytics

---

### 4. Trades History API ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
AccountModule.getTrades({
  marketNames: string[];
  tradeSide?: OrderSide;
  tradeType?: string;
  cursor?: number;
  limit?: number;
})
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `get_trades_history()` Methode hinzugefügt
- ✅ Unterstützt SDK-Methode und REST-API-Fallback
- ✅ Automatische Notional-Berechnung
- ✅ Nur aktuelle Trades (via Positions) - bereits vorhanden

**Zweck:**

- Vollständige Historie aller Trades (Fills)
- Genauere PnL-Berechnung
- Trade-Analyse

**Warum wir es brauchen:**

1. **Genauere PnL-Berechnung:**

   - Aktuell: PnL basiert auf Entry/Exit-Preisen
   - Mit Trades: PnL basiert auf tatsächlichen Fill-Preisen
   - **Präzisere Accounting**

2. **Slippage-Tracking:**

   - Vergleich Limit-Preis vs. Fill-Preis
   - Durchschnittliche Slippage pro Trade
   - **Optimierung der Order-Preise**

3. **Trade-Analyse:**

   - Welche Trades waren profitabel?
   - Durchschnittliche Trade-Dauer
   - Best/Worst Trades

4. **Reconciliation:**
   - Vergleich Bot-Daten vs. Exchange-Daten
   - Fehlererkennung

**Implementierung:**

```python
async def get_trades_history(
    self,
    symbols: List[str],
    trade_side: Optional[str] = None,
    trade_type: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get historical trades (all fills/executions).

    Returns a list of all historical trades with their fill prices, sizes,
    fees, and other details. Useful for:
    - Precise PnL calculation (based on actual fill prices)
    - Slippage tracking (limit price vs fill price)
    - Trade analysis
    - Reconciliation
    """
    # Tries SDK method first, falls back to REST API
    # Returns list of trade dicts with full details including:
    # - Fill price, size, fee, timestamp, orderId, notional value
```

**Features:**

- ✅ SDK-Methode mit automatischem REST-API-Fallback
- ✅ Unterstützt Filterung nach Symbols (Array), Trade Side, Trade Type
- ✅ Pagination mit Cursor-Support
- ✅ Automatische Notional-Berechnung (price × size)
- ✅ Vollständige Trade-Details (Fill-Preis, Size, Fee, Timestamp, Order-ID)
- ✅ Robuste Fehlerbehandlung

**Impact:** 🔥 **HOCH** - ✅ **IMPLEMENTIERT** - Präzisere PnL-Berechnung & Slippage-Tracking

---

### 5. Asset Operations Tracking ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
AccountModule.assetOperations({
  operationsType?: string[];
  operationsStatus?: string[];
  startTime?: number;
  endTime?: number;
  cursor?: number;
  limit?: number;
  id?: number;
})
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `get_asset_operations()` Methode hinzugefügt
- ✅ Unterstützt SDK-Methode und REST-API-Fallback
- ✅ Vollständige Filterung nach Type, Status, Zeitraum
- ✅ Kein Tracking von Deposits/Withdrawals/Transfers - jetzt verfügbar

**Zweck:**

- Vollständiges Accounting (Deposits, Withdrawals, Transfers)
- Balance-Tracking über Zeit
- Compliance & Audit

**Warum wir es brauchen:**

1. **Vollständiges Accounting:**

   - Aktuell: Nur Trading-Aktivitäten werden getrackt
   - Mit Asset Operations: Alle Geldbewegungen
   - **Vollständige Bilanz**

2. **Balance-Tracking:**

   - Nachvollziehen, warum Balance sich ändert
   - Unterscheidung: Trading vs. Deposits/Withdrawals

3. **Compliance:**

   - Vollständiger Audit-Trail
   - Steuer-Reporting

4. **Debugging:**
   - Wenn Balance nicht stimmt → Asset Operations prüfen
   - Erkennen von unerwarteten Transfers

**Implementierung:**

```python
async def get_asset_operations(
    self,
    operation_type: Optional[List[str]] = None,
    operation_status: Optional[List[str]] = None,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    limit: int = 100,
    cursor: Optional[int] = None,
    operation_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get asset operations (deposits, withdrawals, transfers).

    Returns a list of all asset operations with their details. Useful for:
    - Complete accounting (all money movements)
    - Balance tracking over time
    - Compliance & audit trail
    - Debugging balance discrepancies
    """
    # Tries SDK method first, falls back to REST API
    # Returns list of operation dicts with full details
```

**Features:**

- ✅ SDK-Methode mit automatischem REST-API-Fallback
- ✅ Unterstützt Filterung nach Type, Status, Zeitraum, ID
- ✅ Pagination mit Cursor-Support
- ✅ Vollständige Operation-Details (Amount, Asset, Timestamp, Addresses, TX Hash)
- ✅ Robuste Fehlerbehandlung

**Impact:** ⚠️ **MITTEL** - ✅ **IMPLEMENTIERT** - Vollständiges Accounting & Compliance

---

### 6. Order by External ID ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
AccountModule.getOrderByExternalId(externalId: string)
OrderManagementModule.cancelOrderByExternalId(externalId: string)
```

**Aktueller Status:**

- ✅ **IMPLEMENTIERT** - `open_live_position()` erweitert um `external_id` Parameter
- ✅ **IMPLEMENTIERT** - `get_order_by_external_id()` Methode hinzugefügt
- ✅ **IMPLEMENTIERT** - `cancel_order_by_external_id()` Methode hinzugefügt
- ✅ SDK-Methoden mit REST-API Fallback implementiert
- ✅ Nur Order-ID (interne Exchange-ID) - weiterhin unterstützt

**Zweck:**

- Order-Tracking mit eigenen IDs
- Bessere Integration mit externen Systemen
- Idempotenz

**Warum wir es brauchen:**

1. **Order-Tracking:**

   - Aktuell: Nur Exchange-interne IDs
   - Mit External ID: Eigene IDs für besseres Tracking
   - **Bessere Integration**

2. **Idempotenz:**

   - Verhindert doppelte Orders bei Retries
   - Externe ID als Idempotency-Key

3. **Debugging:**
   - Einfacheres Tracking von Orders über Systeme hinweg
   - Korrelation mit eigenen Logs

**Implementierung:**

```python
# Order mit external_id platzieren
success, order_id = await x10_adapter.open_live_position(
    symbol="ETH-USD-PERP",
    side="BUY",
    notional_usd=100.0,
    external_id="my-custom-order-id-123"  # ✅ Neu: External ID Support
)

# Order per external_id abfragen
order = await x10_adapter.get_order_by_external_id("my-custom-order-id-123")
if order:
    print(f"Order Status: {order['status']}, Price: {order['price']}")

# Order per external_id canceln
success = await x10_adapter.cancel_order_by_external_id("my-custom-order-id-123")
```

**Implementierungsdetails:**

1. **`open_live_position()` erweitert:**

   - Neuer optionaler Parameter `external_id: Optional[str] = None`
   - Wird an `client.place_order(external_id=...)` weitergegeben
   - Unterstützt Idempotenz bei Retries

2. **`get_order_by_external_id()`:**

   - Verwendet `client.account.get_order_by_external_id()` wenn verfügbar
   - Fallback auf direkten REST-API-Call: `GET /api/v1/user/orders/external/{external_id}`
   - Gibt geparste Order-Daten zurück oder `None` wenn nicht gefunden

3. **`cancel_order_by_external_id()`:**

   - Verwendet `client.orders.cancel_order_by_external_id()` wenn verfügbar
   - Fallback auf direkten REST-API-Call: `DELETE /api/v1/user/order?externalId={external_id}`
   - Gibt `True` bei Erfolg, `False` bei Fehler zurück

4. **`_parse_order_data()` Helper:**
   - Parst Order-Daten von SDK-Response oder REST-API
   - Unterstützt sowohl dict- als auch Objekt-Format
   - Normalisiert Feldnamen (snake_case und camelCase)

**Impact:** ⚠️ **MITTEL** - ✅ **IMPLEMENTIERT** - Besseres Tracking & Idempotenz

---

### 7. Candles-Stream - IMPLEMENTIERT (2025-12-21)

**SDK-Methode:**

```typescript
PerpetualStreamClient.subscribeToCandles({
  marketName: string;
  candleType: string;
  interval: string;
})
```

**Aktueller Status:**

- IMPLEMENTIERT - X10StreamClient.subscribe_to_candles() hinzugefuegt
- Optional per Config (nur bei USE_ADAPTER_STREAM_CLIENTS=True):
  X10_CANDLE_STREAM_ENABLED, X10_CANDLE_STREAM_TYPE, X10_CANDLE_STREAM_INTERVAL

**Zweck:**

- Echtzeit-Chart-Daten
- Technische Analyse
- Volatility-Monitoring

**Warum wir es brauchen:**

1. **Volatility-Monitoring:**

   - Aktuell: Volatility basiert auf 24h-Statistiken
   - Mit Candles-Stream: Echtzeit-Volatility
   - **Bessere Risiko-Erkennung**

2. **Technische Analyse:**

   - RSI, MACD, etc. in Echtzeit
   - Bessere Entry/Exit-Signale

3. **Performance:**
   - Echtzeit-Daten statt REST-Polling
   - Weniger API-Calls

**Implementierung:**

```python
async def subscribe_to_candles(
    self,
    symbol: str,
    candle_type: str = "trade",
    interval: str = "1m"
) -> None:
    """Subscribe to candle stream"""
    await self._stream_client.subscribe_to_candles(
        message_handler=lambda data: self._handle_candle_stream_message(data, symbol),
        market_name=symbol,
        candle_type=candle_type,
        interval=interval
    )
```

**Impact:** NIEDRIG - Jetzt verfuegbar (opt-in)

---

## 🟢 Lighter (lighter-ts-main) - Fehlende Features

### 1. Unified Orders mit SL/TP ✅ **IMPLEMENTIERT** (2025-01-20)

**SDK-Methode:**

```typescript
SignerClient.createUnifiedOrder({
  marketIndex: number;
  clientOrderIndex: number;
  baseAmount: number;
  isAsk: boolean;
  orderType: OrderType;
  stopLoss?: { triggerPrice: number; isLimit?: boolean };
  takeProfit?: { triggerPrice: number; isLimit?: boolean };
})
```

**Aktueller Status:**

- IMPLEMENTIERT - place_order_with_sl_tp() Methode hinzugefuegt
- createUnifiedOrder genutzt wenn verfuegbar
- Fallback: grouped orders (OTO/OTOCO) fuer SL/TP, atomar
- Automatische Quantisierung und Preis-Skalierung

**Zweck:**

- Automatisches Risikomanagement
- Stop-Loss und Take-Profit in einem API-Call
- Reduzierte Latenz

**Warum wir es brauchen:**

1. **Automatisches Risikomanagement:**

   - Aktuell: Manuelle Überwachung von Positionen
   - Mit SL/TP: Automatisches Schließen bei Limits
   - **Weniger Risiko, weniger Überwachung**

2. **Performance:**

   - 1 API-Call statt 3 (Order + SL + TP)
   - Atomare Operation
   - **3x weniger API-Calls**

3. **Zuverlässigkeit:**

   - SL/TP werden garantiert erstellt
   - Keine Race Conditions zwischen Order und SL/TP

4. **Use Case für Funding-Bot:**
   - Bei hoher Volatility: Automatischer Stop-Loss
   - Bei Profit-Target: Automatischer Take-Profit
   - **Besseres Risikomanagement**

**Implementierung:**

```python
async def place_order_with_sl_tp(
    self,
    symbol: str,
    side: str,
    size: Decimal,
    price: Decimal,
    stop_loss_price: Optional[Decimal] = None,
    take_profit_price: Optional[Decimal] = None,
    reduce_only: bool = False,
    post_only: bool = False,
    time_in_force: Optional[str] = None
) -> Dict[str, Any]:
    """
    Place order with automatic Stop-Loss and Take-Profit (Unified Order).

    This is 3x faster than placing orders individually (1 API call instead of 3)
    and is atomic (all orders are created or none).
    """
    # Uses SDK createUnifiedOrder if available
    # Falls back to grouped orders if SDK method not available
    # Returns dict with mainOrder, stopLoss, takeProfit results
```

**Features:**

- ✅ Prüft SDK `createUnifiedOrder` Verfügbarkeit automatisch
- ✅ Automatische Quantisierung und Preis-Skalierung (Lighter-spezifisch)
- ✅ Unterstützt alle Order-Parameter (reduce_only, post_only, time_in_force)
- ✅ Atomare Operation wenn SDK-Methode verfügbar
- Fallback auf grouped orders (OTO/OTOCO) wenn SDK-Methode nicht verfuegbar
- ✅ Vollständige Fehlerbehandlung und Nonce-Management

**Impact:** 🔥 **HOCH** - ✅ **IMPLEMENTIERT** - Automatisches Risikomanagement & 3x weniger API-Calls

---

### 2. TWAP Orders - IMPLEMENTIERT (2025-12-21)

**SDK-Methode:**

```typescript
SignerClient.createOrder({
  orderType: OrderType.TWAP,
  // ... other params
});
```

**Aktueller Status:**

- IMPLEMENTIERT - place_twap_order() hinzugefuegt
- TWAP nutzt ORDER_TYPE_TWAP und GTT mit Default 1h Expiry
- SL/TP werden nicht im selben Batch erstellt

**Zweck:**

- Time-Weighted Average Price Orders
- Reduzierter Price Impact bei großen Orders
- Graduelle Execution über Zeit

**Warum wir es brauchen:**

1. **Reduzierter Price Impact:**

   - Aktuell: Große Orders = hoher Slippage
   - Mit TWAP: Order wird über Zeit verteilt
   - **Bessere Fill-Preise bei großen Orders**

2. **Use Case:**

   - Bei sehr großen Positionen (>$500)
   - Bei niedriger Liquidität
   - **Optimierung für große Trades**

3. **Strategie-Option:**
   - Alternative zu Market Orders
   - Bessere Kontrolle über Execution

**Implementierung:**

```python
result = await lighter_adapter.place_twap_order(
    symbol="ETH-USD",
    side="BUY",
    size=Decimal("0.01"),
    price=Decimal("4000")
)
print(result)
```

**Impact:** MITTEL - Implementiert und bereit

---

### 3. Grouped Orders - IMPLEMENTIERT (2025-12-21)

**SDK-Methode:**

```typescript
TransactionType.CREATE_GROUPED_ORDERS = 28;
// Multiple orders in one transaction
```

**Aktueller Status:**

- IMPLEMENTIERT - place_grouped_orders() hinzugefuegt
- OTO/OTOCO via create_grouped_orders (GroupingType 1/3)
- SL/TP nutzt BaseAmount=0 und ClientOrderIndex=0 fuer OTOCO

**Zweck:**

- Atomare Multi-Leg-Execution
- Mehrere Orders in einer Transaktion
- Garantierte All-or-Nothing Execution

**Warum wir es brauchen:**

1. **Atomare Execution:**

   - Aktuell: Zwei Orders = zwei separate Transaktionen
   - Mit Grouped: Beide Orders oder keine
   - **Keine Race Conditions**

2. **Use Case:**

   - Komplexe Strategien mit mehreren Legs
   - Hedge + SL/TP in einem Call
   - **Bessere Konsistenz**

3. **Performance:**
   - 1 Transaktion statt N Transaktionen
   - Weniger Nonce-Management

**Implementierung:**

```python
orders = [req_main, req_tp, req_sl]
result = await lighter_adapter.place_grouped_orders(3, orders)
if result.get("success"):
    print("Grouped orders tx hash:", result.get("hash"))
```

**Impact:** MITTEL - Implementiert und nutzbar

---

### 4. Request Batching - TEILWEISE IMPLEMENTIERT

**SDK-Feature:**

```typescript
RequestBatcher - Automatisches Batching von Requests
```

**Aktueller Status:**

- TEILWEISE - LighterBatchManager vorhanden (send_tx_batch)
- Nutzung ist opt-in und nicht global verdrahtet

**Zweck:**

- Automatisches Batching von API-Requests
- Reduzierte Latenz
- Bessere Performance

**Warum wir es brauchen:**

1. **Performance:**

   - Aktuell: Jeder Request einzeln
   - Mit Batching: Mehrere Requests gebündelt
   - **Weniger Latenz**

2. **Rate Limiting:**

   - Weniger API-Calls
   - Bessere Rate-Limit-Nutzung

3. **Use Case:**
   - Beim Start: Viele Market-Daten abrufen
   - Beim Shutdown: Viele Orders canceln
   - **Optimierung für Bulk-Operationen**

**Implementierung:**

```python
class LighterRequestBatcher:
    """Batch multiple requests together"""
    def __init__(self, max_batch_size: int = 10, max_wait_ms: int = 50):
        self.max_batch_size = max_batch_size
        self.max_wait_ms = max_wait_ms
        self._pending_requests = []
        self._batch_task = None

    async def add_request(self, request: Callable) -> Any:
        """Add request to batch"""
        # Collect requests and execute in batch
        pass
```

**Impact:** NIEDRIG - Optionaler Performance-Hebel

---

### 5. Erweiterte Order Status Checker - TEILWEISE IMPLEMENTIERT

**SDK-Feature:**

```typescript
checkOrderStatus() - Intelligente Order-Status-Prüfung
formatOrderResult() - Formatierte Order-Ergebnisse
getCancelReason() - Detaillierte Cancel-Reasons
```

**Aktueller Status:**

- TEILWEISE - get_order_status nutzt active/inactive Orders pro Market
- Cancel-Reason wird aus Status-String extrahiert
- Formatierung/zus. Felder noch ausbaubar

**Zweck:**

- Intelligente Order-Status-Erkennung
- Bessere Error-Handling
- Detaillierte Cancel-Reasons

**Warum wir es brauchen:**

1. **Besseres Error-Handling:**

   - Aktuell: Generische Fehler
   - Mit erweiterten Checks: Spezifische Reasons
   - **Besseres Debugging**

2. **Order-Status-Erkennung:**
   - Automatische Erkennung von Fills
   - Bessere Retry-Logik

**Impact:** NIEDRIG - Debugging verbessert

---

### 6. Funding-Rate-Stream - IMPLEMENTIERT (2025-12-21)

**Status:**

- IMPLEMENTIERT - market_stats/all via WebSocketManager liefert Funding Rates
- REST bleibt als Fallback (wenn WS stale)

**Zweck:**

- Echtzeit-Funding-Rate-Updates
- Sofortige Erkennung von Funding-Änderungen

**Warum wir es brauchen:**

1. **Sofortige Opportunity-Erkennung:**

   - Aktuell: Polling alle 30s
   - Mit Stream: Sofortige Updates
   - **Schnellere Trade-Execution**

2. **Performance:**
   - Weniger API-Calls
   - Echtzeit-Daten

**Impact:** NIEDRIG - Echtzeit-Updates aktiv

---

## 🔵 Gemeinsame Fehlende Features

### 1. Erweiterte Metriken & Dashboard-Integration

**Zweck:**

- Grafana/ELK-Integration für Metriken
- Real-time Dashboards
- Alerting

**Warum wir es brauchen:**

1. **Monitoring:**

   - Visualisierung von Performance
   - Erkennung von Problemen
   - **Proaktives Monitoring**

2. **Analytics:**
   - Trade-Performance über Zeit
   - APY-Tracking
   - **Datengetriebene Optimierung**

**Impact:** ⚠️ **MITTEL** - Wichtig für Production-Monitoring

---

### 2. Backtesting-Framework

**Zweck:**

- Historische Daten für Backtesting
- Strategie-Optimierung
- Performance-Simulation

**Warum wir es brauchen:**

1. **Strategie-Optimierung:**

   - Testen von Parametern auf historischen Daten
   - Vergleich verschiedener Strategien
   - **Bessere Performance**

2. **Risiko-Analyse:**
   - Worst-Case-Szenarien
   - Drawdown-Analyse

**Impact:** ⚠️ **MITTEL** - Wichtig für Strategie-Entwicklung

---

## 📋 Priorisierte To-Do-Liste

### 🔥 Priority 1 (Kritisch - Sofort implementieren)

1. ✅ **X10: Mass Cancel** - Shutdown-Performance (**IMPLEMENTIERT 2025-01-20**)
2. ✅ **X10: Position History** - Analytics & Debugging (**IMPLEMENTIERT 2025-01-20**)
3. ✅ **X10: Orders History** - Performance-Analyse (**IMPLEMENTIERT 2025-01-20**)
4. ✅ **X10: Trades History** - Genauere PnL-Berechnung (**IMPLEMENTIERT 2025-01-20**)
5. ✅ **Lighter: Unified Orders mit SL/TP** - Risikomanagement (**IMPLEMENTIERT 2025-01-20**)

### ⚠️ Priority 2 (Wichtig - Nächste Iteration)

6. ✅ **X10: Asset Operations** - Vollständiges Accounting (**IMPLEMENTIERT 2025-01-20**)
7. ✅ **X10: Order by External ID** - Besseres Tracking (**IMPLEMENTIERT 2025-01-20**)
8. **Lighter: TWAP Orders (IMPLEMENTIERT)** - Große Orders
9. **Lighter: Grouped Orders (IMPLEMENTIERT)** - Atomare Execution

### 💡 Priority 3 (Nice-to-have - Später)

10. **X10: Candles-Stream (IMPLEMENTIERT)** - Technische Analyse
11. **Lighter: Request Batching** - Performance
12. **Lighter: Erweiterte Order Status** - Debugging
13. **Gemeinsam: Dashboard-Integration** - Monitoring
14. **Gemeinsam: Backtesting-Framework** - Strategie-Optimierung

---

## 💰 Geschätzter Impact

### Performance-Verbesserungen

| Feature                         | Zeitersparnis    | API-Call-Reduktion |
| ------------------------------- | ---------------- | ------------------ |
| Mass Cancel                     | 10x schneller    | 90% Reduktion      |
| Unified Orders                  | 3x schneller     | 67% Reduktion      |
| Streams (bereits implementiert) | 20-50x schneller | 100% Reduktion     |

### Risiko-Reduktion

| Feature                  | Risiko-Reduktion                         |
| ------------------------ | ---------------------------------------- |
| Unified Orders mit SL/TP | 🔥 Hoch - Automatisches Risikomanagement |
| Position History         | ⚠️ Mittel - Bessere Analyse              |
| Asset Operations         | ⚠️ Mittel - Vollständiges Accounting     |

---

## 🎯 Empfohlene Implementierungs-Reihenfolge

1. **Woche 1:** Mass Cancel (X10) + Unified Orders (Lighter)
2. **Woche 2:** Position/Orders/Trades History (X10)
3. **Woche 3:** Asset Operations (X10) + TWAP (Lighter)
4. **Woche 4:** Grouped Orders (Lighter) + External ID (X10)
5. **Später:** Candles-Stream, Batching, Dashboard

---

## 📊 Detaillierte Feature-Beschreibungen

### X10: Mass Cancel - Technische Details

**API-Endpoint:**

```
POST /api/v1/user/order/massCancel
```

**Request Body:**

```json
{
  "orderIds": [123, 456, 789],
  "markets": ["ETH-USD", "BTC-USD"],
  "cancelAll": false
}
```

**Response:**

```json
{
  "success": true,
  "data": {},
  "errors": []
}
```

**Verwendung im Bot:**

- Shutdown: `massCancel(cancelAll=True)` → Alle Orders sofort
- Emergency: `massCancel(markets=["ETH-USD"])` → Alle Orders für Symbol
- Cleanup: `massCancel(orderIds=[...])` → Spezifische Orders

**Vorteile:**

- Atomare Operation (alle oder keine)
- Keine Race Conditions
- 10x schneller als einzelne Cancels

---

### X10: Position History - Technische Details

**API-Endpoint:**

```
GET /api/v1/user/positions/history?market=BTC-USD&limit=100&cursor=123
```

**Response:**

```json
{
  "data": [
    {
      "market": "BTC-USD",
      "side": "LONG",
      "size": "0.1",
      "entryPrice": "40000",
      "exitPrice": "41000",
      "pnl": "100",
      "fundingCollected": "5.2",
      "openedAt": "2025-01-01T00:00:00Z",
      "closedAt": "2025-01-01T12:00:00Z",
      "closeReason": "PROFIT_TARGET"
    }
  ],
  "cursor": 456
}
```

**Verwendung im Bot:**

- Performance-Analyse: Alle geschlossenen Trades analysieren
- Backtesting: Historische Daten für Strategie-Tests
- Reporting: Vollständige Trade-Historie exportieren

**Vorteile:**

- Vollständige Trade-Historie
- Genauere Performance-Metriken
- Besseres Debugging

---

### X10: Orders History - Technische Details

**API-Endpoint:**

```
GET /api/v1/user/orders/history?market=BTC-USD&limit=100&cursor=123
```

**Response:**

```json
{
  "data": [
    {
      "id": 123,
      "market": "BTC-USD",
      "side": "BUY",
      "type": "LIMIT",
      "price": "40000",
      "size": "0.1",
      "filledSize": "0.1",
      "avgFillPrice": "40001.5",
      "status": "FILLED",
      "createdAt": "2025-01-01T00:00:00Z",
      "filledAt": "2025-01-01T00:00:05Z",
      "fee": "0.9"
    }
  ],
  "cursor": 456
}
```

**Verwendung im Bot:**

- Fill-Rate-Analyse: Wie viele Orders wurden gefüllt?
- Slippage-Analyse: Limit-Preis vs. Fill-Preis
- Fee-Tracking: Tatsächliche Fees vs. erwartete Fees

**Vorteile:**

- Vollständige Order-Historie
- Performance-Optimierung
- Besseres Fee-Tracking

---

### X10: Trades History - Technische Details

**API-Endpoint:**

```
GET /api/v1/user/trades?market=BTC-USD&limit=100&cursor=123
```

**Response:**

```json
{
  "data": [
    {
      "id": 789,
      "market": "BTC-USD",
      "side": "BUY",
      "price": "40001.5",
      "size": "0.05",
      "fee": "0.45",
      "timestamp": "2025-01-01T00:00:05Z",
      "orderId": 123
    }
  ],
  "cursor": 456
}
```

**Verwendung im Bot:**

- Genauere PnL: Basierend auf tatsächlichen Fill-Preisen
- Slippage-Tracking: Vergleich Limit vs. Fill
- Trade-Analyse: Welche Trades waren profitabel?

**Vorteile:**

- Präzisere PnL-Berechnung
- Bessere Slippage-Analyse
- Vollständige Trade-Historie

---

### Lighter: Unified Orders - Technische Details

**Transaction Type:**

```
CREATE_ORDER (14) mit optionalen SL/TP Orders
```

**Request:**

```json
{
  "marketIndex": 0,
  "clientOrderIndex": 1234567890,
  "baseAmount": 10000,
  "price": 400000,
  "isAsk": false,
  "orderType": 0, // LIMIT
  "stopLoss": {
    "triggerPrice": 380000,
    "isLimit": false
  },
  "takeProfit": {
    "triggerPrice": 420000,
    "isLimit": false
  }
}
```

**Response:**

```json
{
  "mainOrder": {
    "hash": "0x...",
    "success": true
  },
  "stopLoss": {
    "hash": "0x...",
    "success": true
  },
  "takeProfit": {
    "hash": "0x...",
    "success": true
  }
}
```

**Verwendung im Bot:**

- Risikomanagement: Automatischer SL bei hoher Volatility
- Profit-Taking: Automatischer TP bei Profit-Target
- Reduzierte Latenz: 1 Call statt 3 Calls

**Vorteile:**

- Automatisches Risikomanagement
- 3x weniger API-Calls
- Atomare Operation

---

### Lighter: TWAP Orders (IMPLEMENTIERT) - Technische Details

**Transaction Type:**

```
CREATE_ORDER (14) mit orderType = 6 (TWAP)
```

**Request:**

```json
{
  "marketIndex": 0,
  "clientOrderIndex": 1234567890,
  "baseAmount": 100000, // 0.1 ETH
  "isAsk": false,
  "orderType": 6, // TWAP
  "duration": 300 // 5 minutes
}
```

**Verhalten:**

- Order wird über 5 Minuten verteilt ausgeführt
- Reduzierter Price Impact
- Automatische Execution

**Verwendung im Bot:**

- Große Orders (>$500): TWAP statt Market
- Niedrige Liquidität: Graduelle Execution
- Optimierung: Bessere Fill-Preise

**Vorteile:**

- Reduzierter Price Impact
- Bessere Fill-Preise bei großen Orders
- Automatische Execution

---

## 🔍 Vergleich: Vorher vs. Nachher

### Shutdown-Performance

**Vorher (ohne Mass Cancel):**

```
10 Orders zu canceln:
- 10 API-Calls × 0.5s = 5 Sekunden
- Rate Limiting kann zusätzliche Verzögerung verursachen
- Race Conditions möglich
```

**Nachher (mit Mass Cancel):**

```
10 Orders zu canceln:
- 1 API-Call × 0.5s = 0.5 Sekunden
- Atomare Operation
- Keine Race Conditions
```

**Verbesserung:** 10x schneller

---

### PnL-Berechnung

**Vorher (ohne Trades History):**

```
PnL = (Exit-Preis - Entry-Preis) × Size
Problem: Exit-Preis könnte vom Limit-Preis abweichen
```

**Nachher (mit Trades History):**

```
PnL = Summe aller Fill-Preise × Fill-Sizes
Genau: Basierend auf tatsächlichen Fills
```

**Verbesserung:** Präzisere Accounting

---

### Risikomanagement

**Vorher (ohne Unified Orders):**

```
1. Place Order
2. Wait for Fill
3. Place Stop-Loss
4. Place Take-Profit
Problem: Race Conditions, 3 separate Calls
```

**Nachher (mit Unified Orders):**

```
1. Place Order with SL/TP
→ Alles atomar in einem Call
```

**Verbesserung:** Automatisches Risikomanagement, 3x weniger Calls

---

## 📈 Geschätzter ROI (Return on Investment)

### Zeit-Investment vs. Nutzen

| Feature              | Implementierungs-Zeit | Nutzen                         | ROI          |
| -------------------- | --------------------- | ------------------------------ | ------------ |
| Mass Cancel          | 2-3 Stunden           | 10x schnellere Shutdowns       | 🔥 Sehr hoch |
| Unified Orders       | 4-6 Stunden           | Automatisches Risikomanagement | 🔥 Sehr hoch |
| Position History     | 3-4 Stunden           | Vollständige Analytics         | ⚠️ Hoch      |
| Orders History       | 3-4 Stunden           | Performance-Optimierung        | ⚠️ Hoch      |
| Trades History       | 3-4 Stunden           | Präzisere PnL                  | ⚠️ Hoch      |
| Asset Operations     | 2-3 Stunden           | Vollständiges Accounting       | ⚠️ Mittel    |
| Order by External ID | 2-3 Stunden           | Besseres Tracking & Idempotenz | ⚠️ Mittel    |
| TWAP Orders          | 4-5 Stunden           | Große Orders optimieren        | ⚠️ Mittel    |
| Grouped Orders       | 5-6 Stunden           | Atomare Execution              | ⚠️ Mittel    |

**Gesamt-Investment:** ~30-40 Stunden  
**Gesamt-Nutzen:** Signifikante Performance- und Risiko-Verbesserungen

---

## 🚀 Quick Wins (Schnelle Implementierungen mit hohem Impact)

1. Mass Cancel (X10) - 2-3h, 10x schneller Shutdown (IMPLEMENTIERT 2025-01-20)
2. Unified Orders (Lighter) - 4-6h, Risk Management (IMPLEMENTIERT 2025-12-21)
3. Trades History (X10) - 3-4h, PnL/Slippage (IMPLEMENTIERT 2025-01-20)

**Gesamt:** ~10-13 Stunden fuer 3 kritische Features
**Status:** 3/3 implementiert

---

**Letzte Aktualisierung:** 2025-12-21

---

## Implementierungs-Status

### Abgeschlossen

| Feature              | Exchange | Implementiert | Datum      | Impact                                   |
| -------------------- | -------- | ------------- | ---------- | ---------------------------------------- |
| Mass Cancel          | X10      | Yes           | 2025-01-20 | High - schneller Shutdown                |
| Position History     | X10      | Yes           | 2025-01-20 | High - Analytics & Debugging             |
| Orders History       | X10      | Yes           | 2025-01-20 | High - Performance & Analytics           |
| Trades History       | X10      | Yes           | 2025-01-20 | High - PnL & Slippage                     |
| Asset Operations     | X10      | Yes           | 2025-01-20 | Medium - Accounting                       |
| Order by External ID | X10      | Yes           | 2025-01-20 | Medium - Tracking & Idempotenz           |
| Candles Stream       | X10      | Yes           | 2025-12-21 | Low - Realtime candles (opt-in)          |
| Unified Orders SL/TP | Lighter  | Yes           | 2025-01-20 | High - Risk Management                   |
| TWAP Orders          | Lighter  | Yes           | 2025-12-21 | Medium - Large order execution           |
| Grouped Orders       | Lighter  | Yes           | 2025-12-21 | Medium - Atomic multi-leg                |
| Funding Rate Stream  | Lighter  | Yes           | 2025-12-21 | Low - WS market_stats/all                |

### Teilweise

- Request Batching (Lighter) - BatchManager vorhanden, opt-in
- Erweiterte Order Status Checker (Lighter) - active/inactive + cancel reason

### Geplant

1. Dashboard-Integration
2. Backtesting-Framework

**Letzte Aktualisierung:** 2025-12-21
