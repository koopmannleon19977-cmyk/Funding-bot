# TRADE LOGIK: Farm vs Normal Trades

## 📊 ÜBERSICHT

Der Bot hat **2 parallele Trading-Modi**, die je nach Config unterschiedlich agieren:

| Eigenschaft | 🚜 FARM TRADES | 💎 NORMAL TRADES |
|-------------|----------------|------------------|
| **Zweck** | Volume-Farming für Airdrops | Funding-Rate Arbitrage |
| **Loop** | `farm_loop()` + `logic_loop()` (wenn VOLUME_FARM_MODE=True) | `logic_loop()` (wenn VOLUME_FARM_MODE=False) |
| **Hold-Zeit** | Kurz (2 Min) | Lang (bis Exit-Kriterium) |
| **Exit-Logik** | Zeit-basiert | Profit/Loss-basiert |
| **Positionsgröße** | Klein ($12) | Variable ($16-20) |
| **APY Minimum** | Niedrig (3%) | Höher (5%) |

---

## 🚜 FARM TRADES (aktuell AKTIV)

### ✅ Eröffnungskriterien

**Config:** `VOLUME_FARM_MODE = True`

**Bedingungen (farm_loop + logic_loop):**
```python
# FARM LOOP (niedrige Standards)
✅ APY >= 3% (FARM_MIN_APY)
✅ Spread <= 15% (FARM_MAX_SPREAD_PCT)
✅ 24h Volatilität <= 4% (FARM_MAX_VOLATILITY_24H)
✅ Balance >= $12 (FARM_NOTIONAL_USD)
✅ Max 3 parallel (FARM_MAX_CONCURRENT)
✅ Symbol nicht in ACTIVE_TASKS/FAILED_COINS

# LOGIC LOOP (höhere Standards, aber auch Farm)
✅ APY >= 5% (MIN_APY_FILTER, adaptive)
✅ Spread <= 15% (MAX_SPREAD_FILTER_PERCENT)
✅ Alle anderen Standard-Checks
✅ is_farm_trade = True (weil VOLUME_FARM_MODE=True)
```

**Positionsgröße:**
```python
Farm Loop:  $12 (FARM_NOTIONAL_USD)
Logic Loop: $16-20 (Smart Sizing basierend auf Confidence)
```

### ❌ Schließungskriterien (PRIORITÄT)

**Exit-Checks in dieser Reihenfolge:**

1. **VOLATILITY_PANIC** (höchste Priorität)
   ```python
   if volatility_monitor.should_close_due_to_volatility(symbol):
       reason = "VOLATILITY_PANIC"
   ```

2. **FARM_COMPLETE** ⭐ (Zeit-basiert)
   ```python
   elif is_farm_trade and hold_time > 120 seconds:  # FARM_HOLD_SECONDS
       reason = "FARM_COMPLETE"
   ```
   **→ Farm Trades schließen nach 2 Minuten, egal ob Profit oder Loss!**

3. **STOP_LOSS** (-3%)
   ```python
   elif total_pnl < -notional * 0.03:
       reason = "STOP_LOSS"
   ```

4. **TAKE_PROFIT** (+5%)
   ```python
   elif total_pnl > notional * 0.05:
       reason = "TAKE_PROFIT"
   ```

5. **FUNDING_FLIP** (Rate dreht sich um)
   ```python
   elif funding_rate flipped for > 6 hours:  # FUNDING_FLIP_HOURS_THRESHOLD
       reason = "FUNDING_FLIP"
   ```

**⚠️ WICHTIG:** Farm Trades erreichen **fast nie** Stop-Loss/Take-Profit, weil sie nach 2 Min automatisch schließen!

---

## 💎 NORMAL TRADES (wenn VOLUME_FARM_MODE = False)

### ✅ Eröffnungskriterien

**Config:** `VOLUME_FARM_MODE = False`

**Bedingungen (nur logic_loop aktiv):**
```python
✅ APY >= 5% (MIN_APY_FILTER, adaptive basierend auf Markt)
✅ Spread <= 15% (MAX_SPREAD_FILTER_PERCENT)
✅ Nicht auf Blacklist
✅ Nicht TradFi/FX
✅ Volatility erlaubt Entry (volatility_monitor.can_enter_trade)
✅ Balance ausreichend
✅ Prediction Confidence hoch (predictor.predict_next_funding_rate)
✅ Max 40 parallele Trades (MAX_OPEN_TRADES)
```

**Positionsgröße:**
```python
Smart Sizing mit Kelly Criterion:
- Basis: $16 (DESIRED_NOTIONAL_USD)
- Range: $16-20 (MAX_TRADE_SIZE_USD)
- Abhängig von:
  * Prediction Confidence (0.5-1.0)
  * Verfügbare Balance
  * Volatility Adjustment
```

### ❌ Schließungskriterien (PRIORITÄT)

**Exit-Checks in dieser Reihenfolge:**

1. **VOLATILITY_PANIC**
   ```python
   if volatility_monitor.should_close_due_to_volatility(symbol):
       reason = "VOLATILITY_PANIC"
   ```

2. **~~FARM_COMPLETE~~** (wird übersprungen bei is_farm_trade=False)

3. **STOP_LOSS** (-3%)
   ```python
   elif total_pnl < -notional * 0.03:
       reason = "STOP_LOSS"
   ```

4. **TAKE_PROFIT** (+5%)
   ```python
   elif total_pnl > notional * 0.05:
       reason = "TAKE_PROFIT"
   ```

5. **FUNDING_FLIP** (Rate dreht sich um für >6h)
   ```python
   elif funding_rate flipped for > 6 hours:
       reason = "FUNDING_FLIP"
   ```

**Hold-Zeit:** Unbegrenzt (bis Exit-Kriterium erfüllt)

---

## 🔄 AKTUELLE KONFIGURATION (Stand: config.py)

```python
# FARM MODE
VOLUME_FARM_MODE = True           # ✅ Farm Mode AKTIV!
FARM_HOLD_SECONDS = 120           # 2 Minuten Hold
FARM_NOTIONAL_USD = 12            # $12 Positionsgröße
FARM_MAX_CONCURRENT = 3           # Max 3 Farm Trades parallel
FARM_MIN_APY = 0.03               # 3% APY Minimum

# NORMAL MODE (gilt auch für logic_loop im Farm Mode)
MIN_APY_FILTER = 0.05             # 5% APY Minimum
MAX_OPEN_TRADES = 40              # Max 40 Trades total
DESIRED_NOTIONAL_USD = 16.0       # $16 Basis-Größe
MAX_TRADE_SIZE_USD = 20.0         # $20 Maximum
```

---

## 🎯 AKTUELLES VERHALTEN (VOLUME_FARM_MODE = True)

### Beide Loops laufen parallel:

**1. farm_loop() - Niedrige Standards**
```
🔍 🚜 FARM Scanning 234 pairs...
💎 SOL-USD | APY: 3.5%
🚜 Opening FARM: SOL-USD APY=3.5%
✅ 🚜 FARM SOL-USD opened successfully ($12)
⏰ Hold for 2 minutes...
💸 EXIT SOL-USD: FARM_COMPLETE | PnL $0.15 (Fees: $0.06)
```

**2. logic_loop() - Höhere Standards, aber auch als Farm markiert**
```
🔍 🚜 FARM Scanning 234 pairs...
💎 ETH-USD | APY: 8.2%
🚀 EXECUTING ETH-USD: APY=8.2%
✅ 🚜 FARM ETH-USD opened successfully ($18)
⏰ Hold for 2 minutes...
💸 EXIT ETH-USD: FARM_COMPLETE | PnL $0.25 (Fees: $0.09)
```

### Warum beide als Farm?
✅ `find_opportunities()` prüft: `if is_farm_mode is None: is_farm_mode = config.VOLUME_FARM_MODE`
✅ Weil `VOLUME_FARM_MODE = True` → **ALLE Trades werden als Farm-Trades markiert**
✅ **ALLE schließen nach 2 Minuten**, egal aus welchem Loop

---

## 📈 PnL KALKULATION (für beide Trade-Types)

```python
# Funding PnL
funding_pnl = net_funding_rate * hold_hours * notional_usd

# Spread PnL
spread_pnl = (entry_spread - current_spread) / price * notional_usd

# Fees (dynamic, EMA-basiert)
fees = notional_usd * (fee_x10 + fee_lighter) * 2.0

# Total PnL
total_pnl = funding_pnl + spread_pnl - fees
```

**Beispiel Farm Trade ($12, 2 Min Hold, 5% APY):**
```
Funding: 0.0002 * 0.033h * $12 = $0.00008
Spread:  Variable (±$0.01 - $0.10)
Fees:    $12 * 0.00025 * 2 = $0.006
Total:   ~$0.00 - $0.10 (Break-even bis kleiner Profit)
```

**Zweck:** Nicht maximaler Profit, sondern **Volume für Airdrops!**

---

## 🚀 ZUSAMMENFASSUNG

| Szenario | farm_loop() | logic_loop() | is_farm_trade | Exit nach |
|----------|-------------|--------------|---------------|-----------|
| VOLUME_FARM_MODE=True | ✅ Läuft (niedrige APY) | ✅ Läuft (hohe APY) | ✅ TRUE | 2 Min |
| VOLUME_FARM_MODE=False | ❌ Nicht aktiv | ✅ Läuft | ❌ FALSE | Profit/Loss |

**Aktuell (VOLUME_FARM_MODE=True):**
- ✅ Beide Loops öffnen Trades
- ✅ Alle Trades als `is_farm_trade=True` markiert
- ✅ Alle schließen nach 120 Sekunden
- 🎯 Ziel: **Maximales Trading-Volume für Airdrops**

---

**Generiert:** 2025-11-26
