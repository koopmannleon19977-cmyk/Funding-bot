# FIX: Farm Trade Marking Race Condition

## 🐛 PROBLEM

**Symptom:** Farm Trades wurden inkonsistent markiert
- ✅ Einige Trades: `is_farm_trade=True` (korrekt)
- ❌ Viele Trades: `is_farm_trade=False` (falsch)

**Root Cause:** Race Condition zwischen `farm_loop()` und `logic_loop()`
- Beide Loops laufen parallel
- `farm_loop()` setzt `is_farm_trade=True` explizit
- `logic_loop()` ruft `find_opportunities()` auf
- `find_opportunities()` hatte **HARDCODED** `is_farm_trade=False` (Zeile 356)

## ✅ LÖSUNG

### Änderung 1: Dynamic Flag Setting in `find_opportunities()`

**Vorher:**
```python
async def find_opportunities(lighter, x10, open_syms) -> List[Dict]:
    ...
    opps.append({
        'symbol': s,
        'apy': apy * 100,
        'net_funding_hourly': net,
        'leg1_exchange': 'Lighter' if rl > rx else 'X10',
        'leg1_side': 'SELL' if rl > rx else 'BUY',
        'is_farm_trade': False  # ❌ HARDCODED!
    })
```

**Nachher:**
```python
async def find_opportunities(lighter, x10, open_syms, is_farm_mode: bool = None) -> List[Dict]:
    """
    Find trading opportunities

    Args:
        lighter: Lighter adapter
        x10: X10 adapter
        open_syms: Set of already open symbols
        is_farm_mode: If True, mark all trades as farm trades. If None, auto-detect from config.
    """
    # Auto-detect farm mode if not specified
    if is_farm_mode is None:
        is_farm_mode = config.VOLUME_FARM_MODE

    opps: List[Dict] = []
    ...
    mode_indicator = "🚜 FARM" if is_farm_mode else "💎 ARB"
    logger.info(f"🔍 {mode_indicator} Scanning {len(common)} pairs...")
    ...
    opps.append({
        'symbol': s,
        'apy': apy * 100,
        'net_funding_hourly': net,
        'leg1_exchange': 'Lighter' if rl > rx else 'X10',
        'leg1_side': 'SELL' if rl > rx else 'BUY',
        'is_farm_trade': is_farm_mode  # ✅ DYNAMIC: Set based on mode
    })
```

### Änderung 2: Improved Logging

**Vorher:**
```python
logger.info(f"✅ {symbol} opened successfully")
```

**Nachher:**
```python
is_farm = opp.get('is_farm_trade', False)
farm_indicator = "🚜 FARM" if is_farm else "💎 ARB"
logger.info(f"✅ {farm_indicator} {symbol} opened successfully")
```

## 🎯 VERHALTEN

### Wenn `VOLUME_FARM_MODE = True`:
- ✅ `farm_loop()` → Öffnet Farm-Trades (niedrige APY, kurze Hold-Time)
- ✅ `logic_loop()` → Öffnet AUCH Farm-Trades (höhere APY, aber trotzdem als Farm markiert)
- ✅ **ALLE Trades** werden als `is_farm_trade=True` markiert
- ✅ **ALLE Trades** werden nach `FARM_HOLD_SECONDS` geschlossen (aktuell: 120s)

### Wenn `VOLUME_FARM_MODE = False`:
- ✅ `farm_loop()` → Läuft NICHT
- ✅ `logic_loop()` → Öffnet normale Arbitrage Trades (`is_farm_trade=False`)
- ✅ **Normale Exit-Logik:** Stop-Loss, Take-Profit, Funding-Flip, etc.

## 📊 ERWARTETES ERGEBNIS

**Logs werden jetzt zeigen:**
```
🔍 🚜 FARM Scanning 234 pairs...
🚜 Opening FARM: WLD-USD APY=4.2%
✅ 🚜 FARM WLD-USD opened successfully
💸 EXIT WLD-USD: FARM_COMPLETE | PnL $0.23 (Fees: $0.05)
```

**Statt vorher:**
```
🔍 Scanning 234 pairs...  # ❌ Kein Indikator
Opening WLD-USD            # ❌ Kein Farm-Indikator
✅ WLD-USD opened successfully  # ❌ Kein Flag sichtbar
```

## 🧪 TESTING

Prüfe nach Deployment:
1. ✅ Logs zeigen `🚜 FARM` Indikator bei allen Trades
2. ✅ Alle offenen Trades haben `is_farm_trade=True` in DB
3. ✅ Alle Trades werden nach ~2 Minuten geschlossen (FARM_COMPLETE)
4. ✅ Keine Trades mit `is_farm_trade=False` mehr

## 🔧 FILES CHANGED

- `scripts/monitor_funding_final.py`:
  - `find_opportunities()` (Zeile 192-211, 371)
  - `execute_trade_parallel()` (Zeile 629-645)

---

**Status:** ✅ FIXED
**Date:** 2025-11-26
**Priority:** P0 (Critical Bug Fix)
