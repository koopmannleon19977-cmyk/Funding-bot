# Funding Bot Code Review (Delta-Neutral Risk Audit)

[KRITISCH] - Decimal-Verletzung bei Exposure-Check und Sizing
Datei: src/core/trading.py, Zeile 279-323
Problem: `execute_trade_parallel` wandelt die Decimal-Notional (`trade_size`, `final_usd`) mehrfach in `float` um (z.B. Übergabe an `check_total_exposure`, Liquidity-Check, Logging). Dadurch entstehen Rundungsfehler und inkonsistente Hedge-Größen zwischen X10 und Lighter. Risiko: Delta-Neutralität bricht bei hohen Notionalen oder engen Step-Sizes; Exposure-Limit kann durch Float-Rundung überschritten werden.
Lösung: Alle Berechnungen und Parameter an Downstream-Calls in Decimal halten und nur für API-Endpunkte konvertieren, die explizit Float verlangen. Beispiel:
```python
# Vor dem Exposure-Check keine Float-Konvertierung
trade_size = safe_decimal(opp.get('size_usd') or getattr(config, 'DESIRED_NOTIONAL_USD', 500))
can_trade, exposure_pct, max_pct = await check_total_exposure(x10, lighter, trade_size)
...
# Liquidity-Check mit Decimal -> float nur direkt für API-Call
liquidity_usd = float(final_usd)  # nur hier, falls Adapter float erzwingt
if not await lighter.check_liquidity(symbol, lit_side_check, liquidity_usd, is_maker=True):
    ...
```

[HOCH] - Exposure-Berechnung fällt auf Float zurück und ignoriert Null-Saldo
Datei: src/core/state.py, Zeile 311-365
Problem: `check_total_exposure` konvertiert alle Werte am Ende in `float` (`current_leverage`, `max_leverage`) und ersetzt fehlende Balances pauschal durch `Decimal('100.0')`. Bei sehr kleinen oder Null-Salden wird so fälschlich Freigabe erteilt; Float-Rundungen können Limits unterlaufen.
Risiko: Bot öffnet neue Trades trotz unzureichender Margin, was Liquidations- oder Funding-Risiko erzeugt.
Lösung: Vollständig in Decimal rechnen, keinen magischen Fallback nutzen und harte Blockade bei fehlendem Kapital einbauen.
```python
# Ohne Float-Downcast und ohne 100-USD-Fallback
if total_balance <= 0:
    logger.error("No available balance on either exchange; block new trades")
    return False, Decimal('0'), safe_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 5))
current_leverage = (new_total_exposure / total_balance).quantize(Decimal('0.0001'))
max_leverage = safe_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 5))
can_trade = current_leverage <= max_leverage
```

[MITTEL] - Gemeinsame Stückzahlberechnung gibt Float zurück
Datei: src/application/parallel_execution.py, Zeile 48-77
Problem: `calculate_common_quantity` nutzt zwar Decimal intern, gibt aber `float` zurück. Bei kleinen Step-Sizes führt Float-Rundung zu inkonsistenter Coin-Menge zwischen den Börsen; Hedge kann um mehrere Ticks abweichen.
Risiko: Teil- oder Über-Hedging bei engen Lot-Sizes; erhöhtes Delta-Risiko bei schnellen Märkten.
Lösung: Decimal bis zum Ende halten und anrufende Stellen zwingen, nur bei API-Aufruf zu casten.
```python
def calculate_common_quantity(amount_usd, price, x10_step, lighter_step) -> Decimal:
    d_amount = safe_decimal(amount_usd)
    d_price = safe_decimal(price)
    d_x10_step = safe_decimal(x10_step)
    d_lighter_step = safe_decimal(lighter_step)
    if d_price <= 0:
        return Decimal('0')
    max_step = max(d_x10_step, d_lighter_step)
    if max_step <= 0:
        return d_amount / d_price
    steps = (d_amount / d_price) // max_step
    return steps * max_step  # Caller converts to float only for adapter if required
```
