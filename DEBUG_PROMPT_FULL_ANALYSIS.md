# 🔍 COMPREHENSIVE DEBUG & AUDIT PROMPT FOR CLAUDE

## 📋 TASK OVERVIEW

Du bist ein Senior Blockchain/DeFi Systems Engineer mit Expertise in:
- Arbitrage-Bots und Delta-Neutralität
- WebSocket-basierte Trading-Systeme
- Cross-Exchange Hedging
- Python async/await Patterns
- Risk Management & PnL-Tracking

**DEINE AUFGABE**: Führe ein vollständiges Code-Review und Debug-Audit des Funding Arbitrage Bots durch. Analysiere das bereitgestellte Log und identifiziere alle Probleme, Bugs, und Verbesserungsmöglichkeiten.

---

## 📁 PFLICHT-REFERENZ-DATEIEN

Diese Dateien MUSST du als primäre Wissensquelle nutzen:

### SDK & Codebase
| Pfad | Beschreibung |
|------|-------------|
| `C:\Users\koopm\funding-bot\Extended-TS-SDK-master` | X10 SDK (TypeScript) |
| `C:\Users\koopm\funding-bot\lighter-ts-main` | Lighter SDK (TypeScript) |

### Business Logic & Specs
| Pfad | Beschreibung |
|------|-------------|
| `C:\Users\koopm\funding-bot\archive\cleanup_20251215\# Margin Schedule.txt` | Lighter Exchange Specs: Margin, Contracts, Funding, Liquidation Rules |
| `C:\Users\koopm\funding-bot\archive\cleanup_20251215\# Vision and Roadmap.txt` | X10 Exchange Docs: Architecture, Trading Rules, Margin, Order Types |
| `C:\Users\koopm\funding-bot\archive\cleanup_20251215\Jump to Content.txt` | Lighter API Documentation: SDK, WebSockets, Data Structures |

### Bot Core Modules
| Pfad | Beschreibung |
|------|-------------|
| `C:\Users\koopm\funding-bot\config.py` | Zentrale Konfiguration |
| `C:\Users\koopm\funding-bot\src\parallel_execution.py` | Hedged Trade Execution Engine |
| `C:\Users\koopm\funding-bot\src\adapters\x10_adapter.py` | X10 Exchange Adapter |
| `C:\Users\koopm\funding-bot\src\adapters\lighter_adapter.py` | Lighter Exchange Adapter |
| `C:\Users\koopm\funding-bot\src\core\trading.py` | Trade Execution Logic |
| `C:\Users\koopm\funding-bot\src\shutdown.py` | Graceful Shutdown Logic |
| `C:\Users\koopm\funding-bot\src\funding_tracker.py` | Funding Fee Tracking |
| `C:\Users\koopm\funding-bot\src\state_manager.py` | Trade State Machine |
| `C:\Users\koopm\funding-bot\src\reconciliation.py` | Position Sync/Orphan Detection |
| `C:\Users\koopm\funding-bot\src\websocket_manager.py` | WebSocket Connections |

---

## 📊 LOG-ANALYSE (HAUPTFOKUS)

**LOG FILE**: `C:\Users\koopm\funding-bot\logs\funding_bot_LEON_20251219_175747_FULL.log`

### Session Overview (aus Log extrahiert)

| Metrik | Wert |
|--------|------|
| **Start** | 17:57:48 |
| **Ende** | 18:01:02 |
| **Laufzeit** | ~3 Minuten 14 Sekunden |
| **Startkapital** | X10=$146.48, Lighter=$220.29 |
| **Endkapital** | (Position geschlossen) |
| **Trades gestartet** | ~8+ Trade-Versuche |
| **Trades erfolgreich** | 1 (RESOLV-USD) |
| **Trades abgebrochen** | 7+ (RESOLV-USD 5x, FARTCOIN-USD 2x, VIRTUAL-USD) |
| **Finale PnL** | **-$1.32** (verlust) |

---

## 🚨 IDENTIFIZIERTE KRITISCHE ISSUES IM LOG

### ISSUE #1: ORPHAN POSITION (VIRTUAL-USD)
**Zeilen 513-574**
```
18:00:37 [ERROR] ❌ [X10 FILL CHECK] VIRTUAL-USD: TIMEOUT and no position (order likely CANCELLED or EXPIRED)
...
18:00:11 [INFO] 💰💰💰 [x10_account] FILL/TRADE: VIRTUAL-USD BUY 213.0 @ $0.7031
18:00:11 [INFO] ✅ [x10_account] ORDER UPDATE: VIRTUAL-USD BUY FILLED qty=213 filled=213
...
17:59:23 [ERROR] 👻 ORPHAN POSITION: VIRTUAL-USD found (L=0, X=213.0) but NOT in DB!
17:59:23 [WARNING] 🚨 Closing orphan X10 position VIRTUAL-USD (size=213.0)...
```

**ANALYSE-FRAGEN**:
1. Warum wurde der Trade als TIMEOUT markiert, OBWOHL der X10 Order FILLED wurde (siehe 18:00:11)?
2. Die Reconciliation findet die Position erst bei 17:59:23 - das ist NACH dem Fill um 18:00:11 → Zeitstempel-Widerspruch? Prüfe die Reihenfolge im echten Log!
3. War das Problem ein Race Condition zwischen WebSocket Event und Timeout-Logik?
4. Warum wurde der Trade nicht in der DB recorded trotz Fill?

**PRÜFE DIESE DATEIEN**:
- `src/parallel_execution.py`: `_wait_for_x10_fill()` Funktion
- `src/adapters/x10_adapter.py`: WebSocket Order Update Handler
- `src/reconciliation.py`: Orphan Detection Logic

---

### ISSUE #2: REPEATED RESOLV-USD ABORTS (5x)
**Zeilen 197, 283, 394, 476, 553**
```
[WARNING] ❌ [PHASE 0] RESOLV-USD: Orderbook validation FAILED - Entry spread gate tripped (1.01% > 0.20%)
[WARNING] ❌ [PHASE 0] RESOLV-USD: Orderbook validation FAILED - Entry spread gate tripped (0.85% > 0.20%)
[WARNING] ❌ [PHASE 0] RESOLV-USD: Orderbook validation FAILED - Entry spread gate tripped (1.04% > 0.20%)
[WARNING] ❌ [PHASE 0] RESOLV-USD: Orderbook validation FAILED - Entry spread gate tripped (1.16% > 0.20%)
[WARNING] ❌ [PHASE 0] RESOLV-USD: Orderbook validation FAILED - Entry spread gate tripped (1.29% > 0.20%)
```

**ANALYSE-FRAGEN**:
1. Der Spread schwankt zwischen 0.85% und 1.29% - ist RESOLV-USD zu volatil für den aktuellen `MAX_SPREAD_FILTER_PERCENT = 0.002` (0.2%)?
2. Warum wird RESOLV-USD trotzdem immer wieder als Opportunity erkannt (APY: 225%)?
3. Sollte es einen dynamischen Spread-Filter basierend auf APY geben?
4. Die Config sagt `MAX_SPREAD_FILTER_PERCENT = 0.002` (0.2%) aber RESOLV-USD hat 1%+ Spread. Das ist **5x über dem Limit**!

**PRÜFE DIESE DATEIEN**:
- `src/validation/orderbook_validator.py`: Spread Validation Logic
- `config.py`: `MAX_SPREAD_FILTER_PERCENT`, `OB_LIGHTER_MAX_SPREAD_PCT`
- `src/core/trading.py`: Pre-Trade Validation

---

### ISSUE #3: X10 ORDER REJECTED (FARTCOIN-USD)
**Zeile 824**
```
18:00:44 [INFO] 📋 [x10_account] ORDER UPDATE: FARTCOIN-USD BUY REJECTED qty=533 filled=0 orderPrice=$0.28122
```

**ANALYSE-FRAGEN**:
1. Warum wurde die X10 Order REJECTED?
2. Der Log zeigt keinen `statusReason`! Das ist ein Logging-Bug.
3. Mögliche Gründe: Insufficient Margin, POST_ONLY crossed Market, Rate Limit
4. Ist der `orderPrice=$0.28122` zu aggressiv für POST_ONLY?

**PRÜFE DIESE DATEIEN**:
- `src/adapters/x10_adapter.py`: Order Rejection Parsing, `OrderStatusReason` Enum
- X10 SDK Docs: Order Rejection Reasons
- `src/parallel_execution.py`: POST_ONLY Pricing Logic

---

### ISSUE #4: ZOMBIE TRADE in DB (FARTCOIN-USD)
**Zeile 801**
```
18:00:42 [WARNING] ⚠️ ZOMBIE FOUND: FARTCOIN-USD is in DB but not on exchange. Closing...
```

**ANALYSE-FRAGEN**:
1. Wie kam FARTCOIN-USD in die DB ohne Exchange Position?
2. War es ein vorheriger Trade-Versuch der im PENDING State stecken blieb?
3. Warum wurde es nicht bei Abort sauber aus der DB entfernt?
4. Ist `📝 Recorded PENDING trade FARTCOIN-USD in state/DB` das Problem?

**PRÜFE DIESE DATEIEN**:
- `src/state_manager.py`: PENDING State Recording
- `src/parallel_execution.py`: Cleanup nach Abort
- `src/database.py`: Trade State Transitions

---

### ISSUE #5: X10 FILL DETECTION TIMEOUT (FARTCOIN-USD, VIRTUAL-USD)
**Zeilen 303-309, 493-498**
```
17:58:37 [WARNING] X10: Timeout waiting for order 2002060883380088832 update
17:58:37 [ERROR] ❌ [X10 FILL CHECK] FARTCOIN-USD: TIMEOUT and no position
17:59:07 [WARNING] X10: Timeout waiting for order 2002061007510761472 update
17:59:07 [ERROR] ❌ [X10 FILL CHECK] VIRTUAL-USD: TIMEOUT and no position
```

**ABER** bei 17:59:06:
```
17:59:06 [INFO] 📋 [x10_account] ORDER UPDATE: FARTCOIN-USD BUY EXPIRED qty=533 filled=0
```

**ANALYSE-FRAGEN**:
1. Der Order EXPIRED bei 17:59:06, aber Timeout war 17:58:37 - das ist 29 Sekunden später. Das X10 POST_ONLY Order expiry scheint nicht mit dem Bot-Timeout synchronisiert zu sein!
2. Config sagt `X10_MAKER_FILL_WAIT_MAX_SECONDS = 15.0` - aber Order expired NACH 30+ Sekunden?
3. Gibt es eine Expiry-Zeit die bei X10 gesetzt wird?

**PRÜFE DIESE DATEIEN**:
- `src/parallel_execution.py`: `_wait_for_x10_fill()`
- `src/adapters/x10_adapter.py`: Order Expiry Calculation
- X10 SDK: Order Time-In-Force

---

### ISSUE #6: SHUTDOWN PNL BERECHNUNG
**Zeilen 892-924**
```
18:00:54 [INFO] ✅ RESOLV-USD Using Lighter accountTrades close price: $0.110206
18:00:54 [INFO] 💰 RESOLV-USD Lighter Closed PnL: $-3.5936 (entry=$0.107641, close=$0.110206)
...
18:00:58 [INFO] 💰 Shutdown PnL for RESOLV-USD: PnL=$-1.3199, Funding=$0.0320 
                 (lighter_pnl=$-3.5936, x10_pnl=$2.3100, fees=$0.0684)
```

**ANALYSE-FRAGEN**:
1. Entry Prices:
   - X10: $0.10769 (Log Zeile 669)
   - Lighter: $0.106663 (Log Zeile 676) oder $0.107641 (Zeile 868)?
   - **WIDERSPRUCH**: Zeile 676 sagt `Entry Lighter: $0.106663` aber Zeile 868 sagt `entry=$0.107641`
2. Ist die Lighter Entry Price korrekt aufgezeichnet worden?
3. Warum ist die Slippage so hoch? Entry @ $0.1076 → Exit @ $0.1102 = **2.4% Slippage**!
4. Der Trade lief nur ~1 Minute aber verlor $1.32. Ist das akzeptabel für einen Hedge?

**PRÜFE DIESE DATEIEN**:
- `src/shutdown.py`: PnL Calculation Logic
- `src/pnl_utils.py`: SpreadPnL Berechnung
- `src/state_manager.py`: Entry Price Recording

---

### ISSUE #7: LIGHTER FILL PRICE NOT FOUND
**Zeile 670**
```
17:59:47 [WARNING] ⚠️ Lighter fill price not found in position, using mark price $0.106663
```

**ANALYSE-FRAGEN**:
1. Warum wurde der echte Fill Price nicht aus der Lighter Position gelesen?
2. Welche Auswirkung hat das auf die PnL-Berechnung?
3. Sollte der Fill Price aus `accountTrades` API geholt werden?

**PRÜFE DIESE DATEIEN**:
- `src/adapters/lighter_adapter.py`: Fill Price Extraction
- `src/parallel_execution.py`: Trade Recording nach Hedge

---

### ISSUE #8: WASTED OPPORTUNITIES
**Log Pattern Analysis**

| Zeit | Symbol | APY | Ergebnis | Grund |
|------|--------|-----|----------|-------|
| 17:58:03 | FARTCOIN-USD | 48.2% | ABORTED | Spread 0.2% OK aber X10 Timeout |
| 17:58:10 | RESOLV-USD | 226% | ABORTED | Spread 1.01% > 0.20% |
| 17:58:26 | RESOLV-USD | 225.1% | ABORTED | Spread 0.85% > 0.20% |
| 17:58:41 | RESOLV-USD | 225.1% | ABORTED | Spread 1.04% > 0.20% |
| 17:59:00 | RESOLV-USD | 225.1% | ABORTED | Spread 1.16% > 0.20% |
| 17:59:22 | RESOLV-USD | 220.8% | ABORTED | Spread 1.29% > 0.20% |
| 17:59:40 | RESOLV-USD | 220.8% | **SUCCESS** | Spread OK! |
| 18:00:44 | FARTCOIN-USD | 49.1% | ABORTED | X10 REJECTED |

**ANALYSE-FRAGEN**:
1. 6 RESOLV-USD Versuche → 1 Erfolg → 17% Erfolgsrate. Zu viel API Spam!
2. FARTCOIN-USD: 2 Timeouts + 1 Reject = 0 Erfolge. Sollte es auf Blacklist?
3. Gibt es einen Cooldown für wiederholte Attempts desselben Symbols?

---

## 🔬 DETAILLIERTE CODE-REVIEW AUFGABEN

### AUFGABE 1: FILL DETECTION RACE CONDITION
```
Untersuche parallel_execution.py:
1. Wie funktioniert _wait_for_x10_fill()?
2. Gibt es eine Race Condition zwischen:
   - WebSocket Fill Event
   - Timeout Timer
   - Position API Fallback Check
3. Warum wurde VIRTUAL-USD als TIMEOUT markiert obwohl es FILLED wurde?
```

### AUFGABE 2: SPREAD VALIDATION KONSISTENZ
```
Untersuche:
1. config.py: MAX_SPREAD_FILTER_PERCENT = 0.002 (0.2%)
2. config.py: OB_LIGHTER_MAX_SPREAD_PCT = 2.0 (2.0%)
3. WIDERSPRUCH! Welcher Wert wird wann verwendet?
4. src/validation/orderbook_validator.py - welcher Filter greift?
```

### AUFGABE 3: ENTRY PRICE RECORDING
```
Untersuche den Flow:
1. parallel_execution.py: Nach X10 Fill → Lighter Hedge
2. Wo wird entry_price_lighter gesetzt?
3. Warum gibt es den Warning "fill price not found in position"?
4. Ist safe_decimal/safe_float korrekt bei Price Parsing?
```

### AUFGABE 4: POST_ONLY REJECTION HANDLING
```
Untersuche:
1. x10_adapter.py: Order Placement mit POST_ONLY
2. Was passiert bei POST_ONLY + Price crosses Market?
3. Wird statusReason ausgewertet?
4. Sollte der Bot auf MARKET/IOC eskalieren nach Rejection?
```

### AUFGABE 5: ZOMBIE/ORPHAN PREVENTION
```
Untersuche:
1. state_manager.py: Wann wird Trade als PENDING gespeichert?
2. parallel_execution.py: Cleanup bei Abort/Timeout
3. reconciliation.py: Ist die Orphan Detection schnell genug?
4. Gibt es ein rollback_trade_to_db() bei Exception?
```

### AUFGABE 6: SHUTDOWN PNL ACCURACY
```
Untersuche:
1. shutdown.py: close_live_position() Flow
2. pnl_utils.py: calculate_spread_pnl()
3. Warum ist Lighter PnL = -$3.59 aber X10 PnL = +$2.31?
4. Sollte Net PnL ≈ 0 sein bei perfektem Hedge?
5. 2.4% Slippage bei Shutdown - ist das normal?
```

### AUFGABE 7: FUNDING TRACKING VALIDATION
```
Untersuche:
1. funding_tracker.py: Wurden alle Funding Payments erfasst?
2. Log zeigt: X10=$0.0320, Lighter=$0.0000
3. Warum hat Lighter $0 Funding? Trade lief 1+ Stunde, sollte Funding haben!
4. Funding Interval bei Lighter = 1h, Trade lief 17:59-18:00 = 1 Minute → kein Funding erwartet?
```

### AUFGABE 8: X10 ORDER EXPIRY MISMATCH
```
Untersuche:
1. x10_adapter.py: place_order() - wird expiry/TTL gesetzt?
2. timeout = 15s im Bot aber Order expired nach 30s+
3. SDKs prüfen: Wie funktioniert X10 Order TTL?
4. Sollte der Bot kürzere TTL setzen um Sync zu behalten?
```

---

## 📈 PERFORMANCE METRIKEN ZUM PRÜFEN

| Metrik | Aktuell | Ziel | Status |
|--------|---------|------|--------|
| Trade Success Rate | 1/8 (12.5%) | >60% | ❌ KRITISCH |
| Avg Trade Duration | ~6.5s | <10s | ✅ OK |
| Orphan Rate | 1/8 (12.5%) | 0% | ❌ KRITISCH |
| Spread Rejection Rate | 5/8 (62.5%) | <20% | ❌ KRITISCH |
| Net PnL | -$1.32 | >$0 | ❌ VERLUST |
| Funding Collected | $0.032 | >$0.10 | ⚠️ GERING |

---

## ✅ ERWARTETE DELIVERABLES

Nach deiner Analyse, liefere:

### 1. EXECUTIVE SUMMARY
- Top 3 kritischste Bugs
- Geschätzte Auswirkung auf PnL
- Prioritätsreihenfolge für Fixes

### 2. DETAILLIERTE BUG-REPORTS
Für jeden Bug:
```
BUG ID: #X
SEVERITY: CRITICAL / HIGH / MEDIUM / LOW
COMPONENT: [Dateiname]
SYMPTOM: [Was passiert falsch?]
ROOT CAUSE: [Warum?]
FIX: [Code-Änderung oder Config-Änderung]
VALIDATION: [Wie testen wir den Fix?]
```

### 3. CONFIG-OPTIMIERUNGEN
```
Aktuelle Config → Empfohlene Config → Begründung
```

### 4. CODE-FIXES
Für jeden identifizierten Bug, liefere:
- Exakter Dateipfad
- Exakte Zeilennummern wo geändert werden muss
- Vorher/Nachher Code-Snippets

### 5. MONITORING-EMPFEHLUNGEN
- Welche Metriken sollten geloggt werden?
- Welche Alerts sollten eingerichtet werden?

---

## 🧪 SKEPTICAL EXPERT MODE (OBLIGATORISCH)

Nach deiner initialen Analyse, wechsle in den "Skeptical Expert" Modus:

1. **Präsentiere** deine Top 3 identifizierten Bugs
2. **Analysiere kritisch** deine eigene Analyse:
   - Welche Annahmen könnten falsch sein?
   - Gibt es alternative Erklärungen für die Symptome?
   - Welche Edge Cases hast du möglicherweise übersehen?
3. **Identifiziere 3 verwundbarste Punkte** in deinen vorgeschlagenen Fixes:
   - Warum könnte der Fix nicht funktionieren?
   - Welche Nebenwirkungen sind möglich?
   - Was wenn die Root Cause anders ist?

---

## 📚 ZUSÄTZLICHE KONTEXT-DATEIEN

Falls du mehr Kontext brauchst, prüfe auch:

| Datei | Inhalt |
|-------|--------|
| `logs/funding_bot_json.jsonl` | Strukturierte JSON Logs |
| `data/funding.db` | SQLite Datenbank mit Trade History |
| `src/utils/__init__.py` | safe_float, safe_decimal Helpers |
| `src/data/orderbook_provider.py` | Orderbook Daten |
| `tests/` | Unit Tests für Referenz |

---

## ⚠️ WICHTIGE CONSTRAINTS

1. **Delta-Neutralität > Profit**: Niemals eine ungehedgte Position halten!
2. **Decimal für Geld**: Keine float-Arithmetik für Preise/Quantities
3. **Atomic State**: DB muss immer konsistent mit Exchange-Positionen sein
4. **Rate Limits**: Lighter = 1 req/s (Standard), X10 = höher
5. **Funding Schedule**: Beide Exchanges zahlen stündlich Funding

---

*Dieses Prompt wurde am 2025-12-19 um 18:25 UTC+1 erstellt für Log-Analyse der Session 17:57-18:01*
