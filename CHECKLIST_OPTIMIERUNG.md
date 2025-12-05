# Optimierungs-Checkliste: Funding Rate Arbitrage Bot

Diese Checkliste basiert auf der Analyse des Logs vom 05.12.2025 und dem aktuellen Code-Stand.

## 🚨 Priorität 1: Kritische Fixes (Sofort erledigen)

### 1. Kelly Criterion Feedback Loop reparieren
*   **Problem:** Der Bot handelt mit einer `win_rate` von **0.0%** (siehe Log), obwohl er profitabel ist. Das bedeutet, er lernt nicht aus Gewinnen und erhöht die Positionsgröße nicht.
*   **Lösung:** Sicherstellen, dass `KellyPositionSizer.record_trade()` nach jedem erfolgreichen Trade-Abschluss (in `trade_management_loop` oder `ParallelExecutionManager`) aufgerufen wird.
- [ ] `record_trade` Aufruf implementieren/überprüfen.

### 2. Desyncs & "Orphaned Positions" bekämpfen
*   **Problem:** Trotz Rollback-Mechanismus treten Desyncs auf (`DESYNC DETECTED`), bei denen eine Seite offen bleibt.
*   **Lösung:**
    - [ ] `EXECUTION_TIMEOUT` in `parallel_execution.py` leicht erhöhen (z.B. von 15s auf 20s), um Timeouts bei hoher Last zu vermeiden.
    - [ ] Prüfen, ob API-Fehler (Rate Limits) die Ursache für das Scheitern eines Legs sind.

### 3. "Ghost Positions" (Dust) filtern
*   **Problem:** Lighter meldet Kleinstpositionen (Dust), die der Bot als "offen" interpretiert und schließen will.
*   **Lösung:**
    - [ ] In `lighter_adapter.py` -> `fetch_open_positions`: Filter einbauen, der Positionen mit `notional_value < 1.0 USD` ignoriert.

---

## 🚀 Priorität 2: Strategie-Aktivierung (Profitabilität steigern)

### 4. Adaptive Thresholds aktivieren
*   **Problem:** Das Modul `src/adaptive_threshold.py` existiert, wird aber laut Log nicht genutzt (keine "REGIME" Logs).
*   **Lösung:**
    - [ ] `AdaptiveThresholdManager` in den `logic_loop` integrieren.
    - [ ] Bei Markt-Status "COLD" den `min_apy` Filter automatisch erhöhen.

### 5. Latency Arbitrage "scharf schalten"
*   **Problem:** Modul ist da (`20%` Status), aber es wurden keine Trades ausgeführt.
*   **Lösung:**
    - [ ] Schwellenwerte (`lag_threshold`) prüfen – sind sie zu konservativ?
    - [ ] Polling-Frequenz für Preis-Updates erhöhen oder Websocket-Latenz prüfen.

---

## 📊 Detaillierter Status nach Roadmap

### PHASE 1: KERN-ARCHITEKTUR
- [x] **Parallel Execution & Rollback (90%)** - *Funktioniert, aber Desync-Ursachen beheben.*
- [x] **Non-blocking Main Loop (95%)** - *Sehr stabil.*
- [x] **Rate Limiter (100%)** - *Keine 429er Fehler mehr.*
- [x] **DB Migration (100%)** - *Erfolgreich auf aiosqlite.*
- [x] **State Management (100%)** - *In-Memory + Write-Behind läuft.*

### PHASE 2: INTELLIGENCE
- [ ] **Prediction V2 (80%)** - *Läuft, aber ohne Kelly-Lerneffekt (siehe Prio 1).*
- [x] **Orderbook Fetching (90%)** - *Imbalance-Daten vorhanden.*
- [x] **Open Interest Tracking (100%)** - *Liefert Daten für 68 Symbole.*
- [x] **Websockets Refactor (95%)** - *Auto-Reconnect funktioniert.*
- [x] **Event-Loop Umbau (100%)** - *Zentrale Steuerung etabliert.*

### PHASE 3: STRATEGIES
- [ ] **Latency Arbitrage (20%)** - *Inaktiv / Keine Trades im Log.*
- [ ] **Adaptive Threshold (0%)** - *Code inaktiv / Nicht eingebunden.*
- [ ] **Maker Rebates (10%)** - *Gebühren werden getrackt, aber keine aktive Maker-Strategie.*
- [x] **Fee Management (100%)** - *Dynamische Gebühren aktiv.*
- [ ] **Kelly Criterion Sizing (80%)** - *Feedback-Loop fehlt (Kritisch).*
- [x] **BTC Correlation (100%)** - *In Prediction integriert.*

### PHASE 4: ROBUSTNESS
- [x] **Volume Farm Mode (90%)** - *Aktiv, öffnet Trades.*
- [ ] **Regime Detection (80%)** - *Monitor läuft, greift aber scheinbar nicht ins Trading ein.*
