# PnL Tracking Analysis Report

## Zusammenfassung

Die Analyse zeigt **erhebliche Probleme** beim PnL-Tracking des Bots:

### Hauptprobleme

1. **Nur Shutdown-Trades werden getrackt**: Der Bot erfasst nur PnL für Trades, die während des Shutdowns geschlossen werden. Alle anderen geschlossenen Trades werden nicht erfasst.

2. **Falsche PnL-Quelle**: Der Bot verwendet `unrealized_pnl` aus der Position vor dem Schließen, anstatt die tatsächliche `realized_pnl` aus den Close-Trades zu verwenden.

3. **Fehlgeschlagene Trade-History-Abfrage**: Der Code versucht, die tatsächliche PnL aus `fetch_my_trades` zu holen, aber dies schlägt fehl (keine Logs vorhanden).

### Vergleichsergebnisse

Für die 5 Trades, die während des Shutdowns geschlossen wurden:

| Symbol   | CSV Total PnL  | Bot Recorded PnL | Differenz | Status                     |
| -------- | -------------- | ---------------- | --------- | -------------------------- |
| EDEN-USD | **$-5.937355** | $+0.045000       | $5.98     | ❌ **KRITISCH**            |
| JUP-USD  | **$-1.243879** | $-0.014900       | $1.23     | ❌ **KRITISCH**            |
| ONDO-USD | **$+0.219205** | $-0.014000       | $0.23     | ❌ **Falsches Vorzeichen** |
| TAO-USD  | **$-0.084631** | $-0.012400       | $0.07     | ⚠️ **Nah, aber falsch**    |
| ZRO-USD  | **$-1.910596** | $-0.247200       | $1.66     | ❌ **KRITISCH**            |

### CSV-Daten

Die CSV-Datei enthält **109 EDEN-USD Close-Trades** mit einem kumulativen PnL von **$-5.937355**, aber der Bot hat nur **$+0.045000** erfasst (unrealized PnL zum Zeitpunkt des Shutdowns).

### Root Cause

1. **Shutdown-Logik** (`src/shutdown.py:663-732`):

   - Versucht `fetch_my_trades()` aufzurufen, um tatsächliche PnL zu holen
   - Fehler werden nur auf DEBUG-Level geloggt
   - Fallback auf `unrealized_pnl` aus Pre-Close-Daten

2. **PnL-Berechnung**:

   - Verwendet `unrealized_pnl + realized_pnl` aus Position-Daten
   - Diese Werte sind **Schätzungen**, nicht die tatsächlichen Close-Trade-PnL

3. **Fehlende Trade-History-Integration**:
   - Die CSV zeigt, dass Lighter für jeden Close-Trade einen "Closed PnL" Wert bereitstellt
   - Der Bot sollte diese Werte direkt aus den Trade-Daten holen, nicht berechnen

### Empfohlene Fixes

#### Fix 1: Verwende CSV "Closed PnL" direkt

Die CSV-Spalte "Closed PnL" enthält die tatsächliche realized PnL pro Trade. Der Bot sollte:

- Nach dem Schließen einer Position die Trade-History abrufen
- Die "Closed PnL" Werte aller Close-Trades summieren
- Diese Summe als `total_pnl` speichern

#### Fix 2: Verbessere Error-Handling

- Logge Fehler bei `fetch_my_trades` auf INFO-Level, nicht DEBUG
- Zeige klar an, wenn Fallback auf unrealized PnL verwendet wird

#### Fix 3: Tracke alle geschlossenen Trades

- Nicht nur Shutdown-Trades, sondern alle geschlossenen Trades tracken
- PnL sollte bei jedem Trade-Close erfasst werden, nicht nur beim Shutdown

#### Fix 4: Verwende Lighter API "Closed PnL"

- Die Lighter API liefert bereits "Closed PnL" pro Trade
- Diese Werte sollten direkt verwendet werden, anstatt PnL zu berechnen

### Code-Stellen zum Fixen

1. **`src/shutdown.py:663-732`**: `_close_lighter_with_tracking()`

   - Verbessere `fetch_my_trades` Error-Handling
   - Verwende "Closed PnL" aus Trade-Daten direkt

2. **`src/core/trade_management.py:102-160`**: `calculate_realized_pnl()`

   - Sollte tatsächliche Close-Trade-PnL verwenden, nicht berechnen

3. **`src/state_manager.py:414-444`**: `close_trade()`
   - Sollte sicherstellen, dass korrekte PnL-Werte übergeben werden

### Nächste Schritte

1. ✅ Analyse abgeschlossen
2. ⏳ Fix implementieren: Verwende "Closed PnL" aus Trade-History
3. ⏳ Error-Handling verbessern
4. ⏳ Testen mit neuen Trades
5. ⏳ Vergleich mit CSV nach Fix
