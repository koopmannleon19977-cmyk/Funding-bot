# Performance-Audit: Funding Bot (Delta-Neutral Trading System)

### [KRITISCH] - Opportunity Scanning - 50ms Sleep & niedrige Parallelität

**Aktuell:** Jede Symbolabfrage wartet künstlich `await asyncio.sleep(0.05)` und ist zusätzlich durch `asyncio.Semaphore(10)` limitiert, bevor überhaupt Funding/Price-Caches gelesen werden.
**Problem:** Bei 100 Paaren ergibt allein der Sleep >500ms Grundrauschen (50ms × 100 / 10 parallel) noch bevor irgendeine Bewertung startet; Opportunitäten werden dadurch systematisch zu spät erkannt.
**Optimierung:** Entferne den künstlichen Sleep und nutze ein ratelimiter-freundliches Burst-Modell (z.B. Token-Bucket) oder direkte WebSocket-Pushes, damit alle Cache-Lookups sofort erfolgen können. Falls Rate-Limits nötig sind, reduziere den Wait auf ein dynamisches, latenzbasiertes Backoff statt fester 50ms.
**Erwartete Verbesserung:** ~500ms → <50ms pro Scan-Zyklus (≈90% schneller bei 100 Paaren).

```python
# Vorher (vereinfacht)
async def fetch_symbol_data(s: str):
    async with semaphore:
        await asyncio.sleep(0.05)
        lr = await lighter.fetch_funding_rate(s)
        xr = await x10.fetch_funding_rate(s)
        px = await x10.fetch_mark_price(s)
        pl = await lighter.fetch_mark_price(s)
        return (s, lr, xr, px, pl)

# Nachher (burst, ohne künstliche Pause)
async def fetch_symbol_data(s: str):
    async with rate_limiter:  # z.B. aiolimiter/token bucket
        lr, xr, px, pl = await asyncio.gather(
            lighter.fetch_funding_rate(s),
            x10.fetch_funding_rate(s),
            x10.fetch_mark_price(s),
            lighter.fetch_mark_price(s),
        )
        return (s, lr, xr, px, pl)
```

### [HOCH] - Order-Path - Serielle Positions- & Balance-Queries unter Lock

**Aktuell:** Im Trade-Entry-Pfad werden `fetch_open_positions()` (X10 + Lighter), Exposure-Check, Min-Notional-Queries und Balance-Abfragen hintereinander im selben Execution-Lock ausgeführt.
**Problem:** Jede REST/WS-Latenz summiert sich; bei 50–100ms pro Call entstehen leicht 300–500ms, bevor Orders überhaupt signiert werden. Unter hohem Traffic blockiert der Lock weitere Trades.
**Optimierung:** Parallelisiere unabhängige Requests mit `asyncio.gather` (Positionen, Min-Notional, Balances) und verschiebe reine Cache-Lookups (z.B. open_trades-State) vor den Lock. Halte den kritischen Abschnitt minimal (nur Reserve/Book-Kritik und Task-Launch).
**Erwartete Verbesserung:** 300–500ms → <120ms bis Order-Senden (≈60–75% schneller).

```python
# Vorher (verkürzt)
async with lock:
    x10_positions = await x10.fetch_open_positions()
    lighter_positions = await lighter.fetch_open_positions()
    min_req_x10 = await x10.min_notional_usd(symbol)
    min_req_lit = await lighter.min_notional_usd(symbol)
    raw_x10 = await x10.get_available_balance()
    raw_lit = await lighter.get_available_balance()

# Nachher
async with lock:
    (x10_positions, lighter_positions), (min_req_x10, min_req_lit), (raw_x10, raw_lit) = await asyncio.gather(
        asyncio.gather(x10.fetch_open_positions(), lighter.fetch_open_positions()),
        asyncio.gather(x10.min_notional_usd(symbol), lighter.min_notional_usd(symbol)),
        asyncio.gather(x10.get_available_balance(), lighter.get_available_balance()),
    )
    # weiterer Code unverändert
```

### [MITTEL] - Datenpfad - Unnötige Float-Konvertierungen im Hot Path

**Aktuell:** Für Latency-Arb und Spread-Berechnungen werden Funding- und Preiswerte in `find_opportunities` wiederholt zu `float` gewandelt und zurückverpackt, obwohl die Adapter bereits `Decimal` liefern.
**Problem:** Zusätzliche Boxing/Unboxing kostet CPU, triggert Garbage-Collection und untergräbt die geforderte Decimal-Präzision; die Schleife läuft pro Symbol und Scan-Zyklus.
**Optimierung:** Rechne end-to-end mit `Decimal` und konvertiere nur für Logging/JSON-Serialisierung. Entferne doppelte `safe_float`/`float(...)` Aufrufe im Schleifen-Hotpath.
**Erwartete Verbesserung:** ~5–10% weniger CPU pro Scan bei vielen Paaren (Millisekunden-Reduktion, stabilere Präzision).

```python
# Vorher
latency_opp['price_x10'] = safe_float(px)
latency_opp['spread_pct'] = abs(safe_float(px) - safe_float(pl)) / safe_float(px) if px else 0
apy = float(total_apy * 100)

# Nachher
latency_opp['price_x10'] = px
latency_opp['spread_pct'] = (abs(px - pl) / px) if px else Decimal('0')
apy = (total_apy * Decimal('100'))
```
