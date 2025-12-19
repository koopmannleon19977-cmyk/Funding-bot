# BOT INSTRUCTION PLAN & OPERATIONAL RULES (MASTER CONSTITUTION)

> **Hinweis (VS Code Agents, Option A):** Diese Datei ist die Single Source of Truth.
> Um sie als globale VS Code Agent-Instructions zu aktivieren, wird sie nach
> `%APPDATA%\Code\User\prompts\funding-bot-constitution.instructions.md` synchronisiert.
> Sync-Befehl: `powershell -ExecutionPolicy Bypass -File scripts/sync_vscode_instructions.ps1`

Diese Datei definiert die unveränderlichen Gesetze ("Constitution"), nach denen der **Funding Arbitrage Bot** UND der **entwickelnde KI-Agent** handeln müssen.

---

## 🧠 TEIL 1: KI-AGENT VERHALTENS-PROTOKOLL

Diese Regeln gelten für **DICH** (den KI-Code-Assistenten), wenn du an diesem Projekt arbeitest.

### 1. Obligatorische Wissens-Basis (Source of Truth)

Bevor du komplexe Änderungen vornimmst oder Probleme löst, **MUSST** du die folgenden offiziellen Dokumente konsultieren. Rate niemals, wenn die Antwort in diesen Dateien steht.

- **SDK & Codebase**:
  - `C:\Users\koopm\funding-bot\Extended-TS-SDK-master` (X10 SDK)
  - `C:\Users\koopm\funding-bot\lighter-ts-main` (Lighter SDK)
- **Business Logic & Specs**:
  - `C:\Users\koopm\funding-bot\archive\cleanup_20251215\# Margin Schedule.txt` (Lighter Exchange Specs: Margin, Contracts, Funding, Liquidation Rules)
  - `C:\Users\koopm\funding-bot\archive\cleanup_20251215\# Vision and Roadmap.txt` (Extended/X10 Exchange Docs: Architecture, Trading Rules, Margin, Order Types)
  - `C:\Users\koopm\funding-bot\archive\cleanup_20251215\Jump to Content.txt` (Lighter API Documentation: SDK, WebSockets, Data Structures)
- **Externe Quellen**:
  - Suche im Internet nach aktuellen API-Änderungen oder Dokumentationen, falls lokal nicht ausreichend.

### 2. Der "Skeptical Expert" Modus

Für Aufgaben, die tiefes Nachdenken oder Architektur-Entscheidungen erfordern, befolge diesen strikten Ablauf:

1.  **Initiale Lösung**: Präsentiere deine beste Antwort/Lösung.
2.  **Skeptische Analyse**: Wechsele sofort die Perspektive. Handle als externer, kritischer Senior-Auditor, der deine Lösung "zerlegen" will.
3.  **Vulnerability Report**:
    - Identifiziere **exakt 3 verwundbarste Punkte** in deiner Lösung.
    - Erkläre spezifisch, warum diese scheitern könnten (Edge Cases, Race Conditions, API-Limits).
    - _Optional_: Schlage Mitigationen vor.

---

## 🤖 TEIL 2: TRADING BOT CORE RULES

### 0. Oberste Direktive: Delta-Neutralität

Der Bot ist ein **Arbitrage-System**, kein Directional-Trader.

- **Regel**: Es darf **NIEMALS** eine Position auf einer Börse gehalten werden, ohne eine exakt gegenläufige Position auf der anderen Börse zu haben (außer während der Millisekunden der Ausführung).
- **Notfall-Protokoll**: Sollte ein Hedge (Leg 2) fehlschlagen, muss der Bot **SOFORT** versuchen, Leg 1 zu schließen ("Atomic Rollback"). Ein "Warten auf bessere Preise" ist bei einem Unhedged-Event verboten.

### 1. Entry-Regeln (Gatekeping)

Der Bot darf einen Trade nur eingehen, wenn **ALLE** folgenden Bedingungen erfüllt sind:

#### A. Profitabilität (Positive Expectancy)

1. **Minimum APY**: Die annualisierte Funding-Rate muss `> MIN_APY_FILTER` sein (Standard: **35%**).
2. **Breakeven-Time**: Die erwarteten Gebühren (Entry + Exit + Slippage) müssen durch Funding-Einnahmen innerhalb von `MAX_BREAKEVEN_HOURS` (Standard: **8h**) gedeckt sein.
3. **Net Profit Check**: `(Est. Funding * HoldTime) - (All Fees) > MIN_PROFIT_EXIT_USD` (Standard: **$0.10**).

#### B. Markt-Qualität

1. **Spread-Filter**: Der Bid-Ask Spread darf `MAX_SPREAD_FILTER_PERCENT` (Standard: **0.2%**) nicht überschreiten.
2. **Liqudität**: Das Orderbuch muss auf beiden Seiten genug Tiefe haben, um `DESIRED_NOTIONAL_USD` ohne übermäßige Slippage auszuführen.

#### C. System-Status

1. **Keine offenen Fehler**: Circuit Breaker `CB_MAX_CONSECUTIVE_FAILURES` darf nicht ausgelöst sein.
2. **Exposure Limits**: `MAX_OPEN_TRADES` Limit noch nicht erreicht.

### 2. Exekutions-Regeln (Execution Flow)

Der Bot nutzt eine asynchrone, parallele Ausführungslogik ("Optimistic Execution").

#### Schritt 1: Leg 1 (Lighter - Maker)

- **Typ**: Immer `LIMIT` Order mit `POST_ONLY=True`.
- **Ziel**: Verdienen des Spreads oder Minimierung der Fees.
- **Timeout**: Dynamisch basierend auf Volatilität. Bei Timeout -> Cancel.
- **Escalation**: Nur bei extrem profitablen Opportunities (`> 80% APY` & Stable Spread) darf auf `MARKET/IOC` gewechselt werden (falls in Config erlaubt).

#### Schritt 2: Leg 2 (X10 - Maker-First mit Taker-Fallback)

**NEU (2025-12-18): X10 Maker Engine - Speed-First Strategy**

Da X10 weniger liquid ist als Lighter, nutzen wir eine aggressive Maker-First Strategie:

- **Typ**: Erst `POST_ONLY` (0% Fees) → dann Eskalation zu `TAKER/IOC` (0.0225%)
- **Trigger**: Wird SOFORT nach bestätigtem Fill von Leg 1 ausgelöst.
- **Spread-Protection (NEU 2025-12-19)**: VOR dem Hedge-Start prüfen, ob der Spread stabil geblieben ist.
  - Wenn Spread sich signifikant verschlechtert (z.B. < 80% des erwarteten Spreads) -> **ABORT HEDGE & ROLLBACK LEG 1**.
  - Verhindert negative Spread-PnL durch Slippage/Latency während des Lighter-Fills.
- **Size**: Muss EXAKT der gefillten Quantity von Leg 1 entsprechen (angepasst an Lot-Size).
- **Self-Trade Protection (STP)**: Muss aktiviert sein, um Eigenhandel zu verhindern.

**X10 Maker Engine Flow:**
1. **POST_ONLY Order** platzieren (spart 0.0225% Taker-Fees!)
2. **Warten auf Fill** (3s Timeout - schnell wegen geringer X10 Liquidität)
3. **Ghost-Fill-Schutz**: Position-Delta-Check vor jedem Cancel
4. **Requote** (max 1x): Cancel + neue Order mit 0.1% aggressiverem Preis
5. **Taker-Eskalation**: Nach Timeout → IOC Order (garantiert Hedge-Completion)

**Config-Parameter:**
- `X10_MAKER_ENABLED = True` (Master-Switch)
- `X10_MAKER_TIMEOUT_SECONDS = 3.0` (schnell!)
- `X10_MAKER_MAX_REQUOTES = 1` (nur 1 Retry)
- `X10_MAKER_ESCALATION_ENABLED = True` (immer eskalieren)

**Priorität:** Hedge-Completion > Fee-Savings. Lieber Taker-Fees als unhedged!

#### Schritt 3: Fehlerbehandlung (Rollback)

- Wenn Leg 2 fehlschlägt (Reject, Timeout, Partial Fill):
  - **Aktion**: Sofortiges Schließen (Market Close) der offenen Menge auf Leg 1.
  - **Prio**: Sicherheit > Kosten. Market Orders sind hier akzeptabel.

### 3. Position Management (Holding)

Während eine Position offen ist:

1. **Funding Tracking**: Jede Stunde prüfen, ob Funding-Zahlungen eingegangen sind. Datenbank `funding_history` updaten.
2. **Exit-Bedingungen (ODER-Verknüpfung)**:
   - ✅ **Profit erreicht**: Realisierter PnL + Funding > Target.
   - ⏰ **Zeit abgelaufen**: `MAX_HOLD_HOURS` (Standard: **72h**) erreicht -> Force Close.
   - 📉 **APY Crash**: APY fällt unter `MIN_MAINTENANCE_APY` (Standard: **20%**).
   - � **Funding Flip**: Net-Funding negativ (wir zahlen) für > `FUNDING_FLIP_HOURS_THRESHOLD` (Standard: **4h**).
   - �🚨 **Volatility Panic**: 24h Volatilität > `VOLATILITY_PANIC_THRESHOLD` (Standard: **8%**) -> Sofortiger Exit zur Sicherung des Kapitals.

### 4. Technische Prinzipien (Code-Ebene)

#### A. Präzision & Daten

- **Decimal Only**: Alle Preise, Quantities und PnL-Berechnungen MÜSSEN `decimal.Decimal` nutzen. `float` ist für finanzielle Berechnungen verboten.
- **Rounding**: Immer `ROUND_DOWN` für Bids/Buys und `ROUND_UP` für Asks/Sells (konservatives Rounding).

#### B. State Persistence

- **Single Source of Truth**: Die SQLite-Datenbank (`data/funding.db`) ist die Wahrheit.
- **Write-Ahead/Through**: Status-Änderungen (z.B. "Trade Opened") müssen persistiert werden, _bevor_ oder _während_ der nächste Schritt eingeleitet wird, um bei Crashs wiederherstellbar zu sein.

#### C. Logging & Monitoring

- **JSONL**: Alle Events müssen strukturiert in `funding_bot_json.jsonl` geloggt werden.
- **Fehler**: Jeder Fehler (Exception) muss getrackt werden.

### 5. Shutdown-Protokoll

Sollte der Bot beendet werden (User-Signal oder Crash):

1. **Cancel All**: Alle offenen Orders auf beiden Exchanges löschen.
2. **Inventory Check**: Prüfen, ob Positionen offen sind.
   - Wenn JA: Versuchen, sauber in DB zu speichern (bei Graceful Shutdown).
   - Wenn Kritisch (Crash): Wenn möglich Panic-Close (konfigurierbar), sonst Alarmierung.
3. **Database Checkpoint**: `WAL Checkpoint` durchführen, um DB-Konsistenz zu sichern.

---

## 🧩 TEIL 3: AGENT OPERATIONS PLAYBOOK (konkret & umsetzbar)

> Ziel: Sofort produktiv arbeiten, ohne Architekturfehler zu riskieren.

1. **Kontext laden (immer zuerst)**:

   - Lies: `README.md` (Architektur), `config.py` (Policy), `src/parallel_execution.py` (Execution).
   - Prüfe `logs/funding_bot_json.jsonl`.

2. **Änderung planen (klein & atomar)**:

   - Definiere exakt: betroffene Module, erwarteter Effekt, Messkriterien.
   - Prüfe Nebenwirkungen: Delta-Neutralität, Persistenz, JSONL-Logging.

3. **Implementieren (Patterns beachten)**:

   - **Defensive Coding**: `try/except` um externe Calls. `None`-Checks für API-Outputs.
   - **Decimal Math**: Keine Floats für Geld.
   - **Async Safety**: Keine `time.sleep` Blockaden.

4. **Testen (eng am Code)**:

   - Führe gezielte Tests aus: `pytest -q tests/test_parallel_execution.py`.
   - Bei neuen Funktionen: Unit-Tests ergänzen.

5. **Verifizieren**:
   - Bot im Dry-Run prüfen.
   - Shutdown auslösen und Sauberkeit prüfen.

---

## 🧪 TEIL 4: BUILD / TEST / RUN WORKFLOWS

### Abhängigkeiten

- Python 3.10+, `pip install -r requirements.txt`
- Lighter TS-SDK bauen: `cd lighter-ts-main && npm run build`

### Bot starten

- Windows: `START_BOT2.bat` oder `python src/main.py`

### Tests

- Schnell: `pytest -q`
- Zielgerichtet: `pytest -q tests/test_parallel_execution.py`

---

## 📏 TEIL 5: CODING STANDARDS & QUALITÄT

**1. Robustness First (Defensive Coding)**

- **Null Safety**: Gehe niemals davon aus, dass eine API-Antwort Daten enthält. Nutze Helper wie `safe_float()`.
- **Error Handling**: Fange Fehler dort, wo sie passieren. Logge den Stacktrace (`exc_info=True`) nur bei echten Fehlern.

**2. "Der Log ist dein Auge"**

- **Entscheidungs-Transparenz**: Logge _warum_ eine Entscheidung getroffen wurde (z.B. "Rejected Strategy A because Spread 0.5% > 0.2%").
- **Struktur**: Nutze `logger.info` für normale Flows, `logger.warning` für Retries.

**3. Keine "Magic Numbers"**

- Konfigurationswerte gehören NACH `config.py`, nicht in den Code.

**DO**

- Decimal für alle Beträge.
- Maker (Lighter) → Taker (X10) strikt gemäß Policy.
- Compliance-Check vor Entry.

**DON'T**

- Keine globalen Refactors ohne Backup.
- Circuit Breaker nicht umgehen.

---

## 🔌 TEIL 6: INTEGRATIONSPUNKTE

- Lighter WebSocket: `src/ws_order_client.py`
- Validation: `src/validation/orderbook_validator.py`
- Database: `src/database.py` (WAL Mode)

---

## 🗂️ TEIL 7: TROUBLESHOOTING

- `logs/funding_bot_json.jsonl` auf "State: FAILED" filtern.
- Maker-Timeouts in `ParallelExecutionManager` prüfen.
- DB-Konsistenz mit `scripts/check_db.py` sichern.

---

## � TEIL 8: PERFORMANCE-OPTIMIERUNG (THE LOOP)

Perfektion ist kein Zustand, sondern ein Prozess. Befolge diesen "Kaizen"-Zyklus:

### 1. The 'Missed Opportunity' Audit (Wöchentlich)

- Analysiere Logs auf `REJECTED`:
  - `REJECT_REASON="Spread too high"`: Wenn oft der Fall, prüfe ob `MAX_SPREAD_FILTER` leicht erhöht werden kann (bei hohem APY).
  - `REJECT_REASON="Liquidity"`: `DESIRED_NOTIONAL_USD` eventuell temporär senken?

### 2. Execution-Speed Audit

- Prüfe in Logs die Zeit zwischen `LEG1_FILLED` und `LEG2_SENT`. Ziel: < 100ms.
- Wenn langsamer: Prüfe Server-Latency oder Code-Overhead (Logging im Critical Path?).

### 3. Tuning-Regel

- Ändere beim Optimieren nur **einen** Parameter gleichzeitig (z.B. Leverage ODER Spread), um die Kausalität der Ergebnisverbesserung zu beweisen.

---

## 🔒 TEIL 9: SICHERHEIT & COMPLIANCE

- Secrets maskieren (SensitiveDataFilter).
- Self-Trade-Protection beachten.
- **Delta-Neutralität > Profit**.

---

*Version 2.4 - Aktualisiert am 19.12.2025 - Spread Protection (Feature #1) & Funding-Flip (Feature #2) implementiert*
