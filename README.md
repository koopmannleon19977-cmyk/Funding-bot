# 🚀 Funding Arbitrage Bot - Komplette Architektur-Dokumentation

> **Zweck dieser Dokumentation:** Vollständige Übersicht über den Bot-Aufbau, Funktionalität und Architektur als Basis für einen verbesserten Neubau.

---

## 📋 Inhaltsverzeichnis

1. [Bot-Übersicht](#bot-übersicht)
2. [Kernfunktionalität](#kernfunktionalität)
3. [Architektur-Übersicht](#architektur-übersicht)
4. [Modul-Struktur](#modul-struktur)
5. [Trading-Strategie](#trading-strategie)
6. [Technische Details](#technische-details)
7. [Verbesserungspotenziale](#verbesserungspotenziale)

---

## 🎯 Bot-Übersicht

### Was macht der Bot?

Der **Funding Arbitrage Bot** ist ein automatisiertes Trading-System, das **Funding Rate Arbitrage** zwischen zwei Kryptobörsen betreibt:

- **Lighter Protocol** (DEX - Decentralized Exchange)
- **X10 Exchange** (CEX - Centralized Exchange)

### Kernprinzip

Der Bot eröffnet **delta-neutrale Positionen** (Long auf einer Börse, Short auf der anderen) und verdient durch die **Differenz der Funding Rates** zwischen beiden Börsen.

**Beispiel:**
```
Lighter: LONG ETH  (+0.02% stündlich = Funding erhalten)
X10:     SHORT ETH (-0.01% stündlich = Funding zahlen)
─────────────────────────────────────────────────────
Net Funding: +0.01% pro Stunde

Bei $150 Position auf jeder Seite:
• Stündlicher Profit: $300 × 0.01% = $0.03/Stunde
• Täglicher Profit: $0.72
• Annualisierte APY: ~88%
```

---

## ⚙️ Kernfunktionalität

### 1. Opportunity Detection (Opportunitäts-Erkennung)

**Modul:** `src/core/opportunities.py`

**Funktionen:**
- Scannt beide Börsen nach profitablen Funding-Rate-Differenzen
- Berechnet APY (Annualized Percentage Yield)
- Filtert nach:
  - Minimum APY (Standard: 20-35%)
  - Spread-Limits (max 0.2%)
  - Liquidität (Orderbook-Tiefe)
  - Volatilität (24h Volatility)
  - Breakeven-Zeit (max 8h)
  - Blacklist (ausgeschlossene Coins)

**Besonderheiten:**
- **Latency Arbitrage:** Nutzt Verzögerungen bei Funding-Rate-Updates (X10 ist 3-10s langsamer)
- **Price Impact Simulation:** Berechnet echte Slippage über Orderbook-Levels
- **Dynamic Spread Limits:** Volatilitätsbasierte Anpassung der Spread-Filter

### 2. Trade Execution (Trade-Ausführung)

**Modul:** `src/core/trading.py`, `src/application/execution/parallel_execution.py`

**Strategie:**
1. **Leg 1 (Lighter):** Maker Order (POST_ONLY) - 0% Fees
2. **Leg 2 (X10):** Taker Order (IOC) - 0.0225% Fees

**Ausführungs-Flow:**
```
PENDING → LEG1_SENT → LEG1_FILLED → LEG2_SENT → COMPLETE
    │         │            │             │
    └─────────┴────────────┴─────────────┘
         Rollback bei Fehler
```

**Features:**
- **Parallel Execution:** Beide Legs werden gleichzeitig gestartet
- **Optimistic Rollback:** Bei Hedge-Fehler wird Leg 1 sofort geschlossen
- **Spread Protection:** Prüft Spread-Stabilität vor Hedge-Start
- **Ghost Fill Detection:** Erkennt teilweise gefüllte Orders

### 3. Position Management (Positions-Verwaltung)

**Modul:** `src/core/trade_management.py`

**Funktionen:**
- Überwacht offene Positionen
- Trackt Funding-Zahlungen (stündlich)
- Berechnet PnL (Profit & Loss)
- Entscheidet über Exit-Bedingungen

**Exit-Bedingungen:**
- ✅ **Profit erreicht:** Realisierter PnL + Funding > Target ($0.10)
- ⏰ **Zeit abgelaufen:** Max Hold Time (72h) erreicht
- 📉 **APY Crash:** APY fällt unter 20%
- 💰 **Funding Flip:** Net-Funding negativ für > 4h
- 🚨 **Volatility Panic:** 24h Volatilität > 8%

### 4. Risk Management (Risiko-Management)

**Module:** `src/domain/risk/`, `src/risk/`

**Circuit Breakers:**
- Max 5 aufeinanderfolgende Fehler → Bot stoppt
- Max 20% Drawdown → Bot stoppt
- Volatility Hard Cap (50%) → Keine neuen Entries

**Position Limits:**
- Max 2 gleichzeitige Positionen
- Max Exposure: 10% des Kapitals
- Min Free Margin: 5%

### 5. State Management (Zustands-Verwaltung)

**Module:** `src/state_manager.py`, `src/core/state.py`, `src/infrastructure/persistence/`

**Features:**
- **In-Memory State:** Schneller Zugriff auf offene Trades
- **SQLite Persistence:** Crash-sichere Datenbank
- **Write-Behind Pattern:** Memory-first, async DB writes
- **State Machine:** PENDING → OPEN → CLOSED

### 6. Monitoring & Logging

**Module:** `src/core/monitoring.py`, `src/utils/json_logger.py`

**Features:**
- **Structured JSON Logging:** JSONL Format für Grafana/ELK
- **Telegram Alerts:** Real-time Benachrichtigungen
- **Health Reports:** Regelmäßige Status-Updates
- **Connection Watchdog:** Auto-Reconnect bei WebSocket-Fehlern

---

## 🏗️ Architektur-Übersicht

### High-Level Architektur

```
┌─────────────────────────────────────────────────────────────────┐
│                    FUNDING ARBITRAGE BOT                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐  │
│  │   Lighter    │◄──►│   Opportunity    │◄──►│     X10      │  │
│  │   Adapter    │    │     Finder       │    │   Adapter    │  │
│  └──────┬───────┘    └────────┬─────────┘    └──────┬───────┘  │
│         │                     │                       │          │
│         ▼                     ▼                       ▼          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │         PARALLEL EXECUTION MANAGER                        │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │  │
│  │  │  Leg 1      │  │  Leg 2      │  │  Rollback       │  │  │
│  │  │  (Lighter)  │  │  (X10)      │  │  Processor      │  │  │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
│                              │                                   │
│         ┌────────────────────┼────────────────────┐            │
│         ▼                    ▼                    ▼            │
│  ┌────────────┐    ┌─────────────────┐    ┌──────────────┐   │
│  │   State    │    │     Trade       │    │   Funding    │   │
│  │  Manager   │    │   Management    │    │   Tracker    │   │
│  └─────┬──────┘    └─────────────────┘    └──────────────┘   │
│        │                                                       │
│        ▼                                                       │
│  ┌─────────────────┐    ┌────────────────┐    ┌──────────┐  │
│  │    SQLite DB     │    │ Volatility     │    │ Shutdown │  │
│  │  (Persistence)   │    │ Monitor        │    │ Manager  │  │
│  └─────────────────┘    └────────────────┘    └──────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Datenfluss

```
1. Opportunity Detection     2. Trade Execution          3. Position Management
┌──────────────────┐        ┌───────────────────┐       ┌───────────────────┐
│ Scan both         │        │ Send Lighter Leg  │       │ Monitor PnL       │
│ exchanges for     ├───────►│ (Maker/Limit)     ├──────►│ Track Funding     │
│ funding spread    │        │ + X10 Leg (Taker) │       │ Check Exit Cond.  │
└──────────────────┘        └───────────────────┘       └───────────────────┘
        │                           │                           │
        │  Filters:                 │  State Machine:           │  Exit when:
        │  • APY > 20%              │  PENDING → LEG1_SENT      │  • Min profit hit
        │  • Spread < 0.2%          │  → LEG1_FILLED            │  • Max hold time
        │  • Breakeven < 8h         │  → LEG2_SENT              │  • APY flips
        │  • Liquidity OK           │  → COMPLETE               │  • Volatility spike
        └───────────────────────────┴───────────────────────────┘
```

---

## 📁 Modul-Struktur

### Core Modules (`src/core/`)

#### `opportunities.py`
- **Zweck:** Opportunity Detection
- **Hauptfunktionen:**
  - `find_opportunities()` - Scannt beide Börsen
  - `calculate_expected_profit()` - Berechnet erwarteten Profit
  - `is_tradfi_or_fx()` - Filtert TradFi/FX Coins

#### `trading.py`
- **Zweck:** Trade Execution
- **Hauptfunktionen:**
  - `execute_trade_parallel()` - Führt Trade aus
  - `close_trade()` - Schließt Trade
  - `launch_trade_task()` - Startet Trade-Task

#### `trade_management.py`
- **Zweck:** Position Management
- **Hauptfunktionen:**
  - `manage_open_trades()` - Überwacht offene Trades
  - `calculate_realized_pnl()` - Berechnet PnL
  - `cleanup_zombie_positions()` - Bereinigt Zombie-Positionen

#### `state.py`
- **Zweck:** State Management
- **Hauptfunktionen:**
  - `get_open_trades()` - Holt offene Trades
  - `add_trade_to_state()` - Fügt Trade hinzu
  - `close_trade_in_state()` - Schließt Trade

#### `monitoring.py`
- **Zweck:** Background Monitoring
- **Hauptfunktionen:**
  - `trade_management_loop()` - Trade-Überwachungs-Loop
  - `farm_loop()` - Farm-Mode Loop
  - `health_reporter()` - Health Reports

#### `startup.py`
- **Zweck:** Bot Initialization
- **Hauptfunktionen:**
  - `run_bot_v5()` - Main Entry Point
  - `setup_database()` - DB Setup
  - `FundingBot` - Bot-Klasse

### Adapters (`src/adapters/`)

#### `lighter_adapter.py` (292KB)
- **Zweck:** Lighter Protocol Integration
- **Features:**
  - REST API Client
  - WebSocket Order Submission
  - Nonce Management
  - Batch Order Support
  - Maker/Taker Orders

#### `x10_adapter.py` (139KB)
- **Zweck:** X10 Exchange Integration
- **Features:**
  - StarkNet Signing
  - WebSocket Data Streams
  - Self-Trade Protection (STP)
  - Market/Limit/IOC Orders

### Infrastructure (`src/infrastructure/`)

#### `persistence/database.py`
- **Zweck:** SQLite Database Layer
- **Features:**
  - Async Database Operations
  - Trade History Persistence
  - Funding Payment Records
  - PnL Snapshots

#### `persistence/state_manager.py`
- **Zweck:** In-Memory State Manager
- **Features:**
  - Write-Behind Pattern
  - Trade State Caching
  - Async DB Writes

#### `messaging/websocket_manager.py`
- **Zweck:** WebSocket Connection Manager
- **Features:**
  - Auto-Reconnect
  - Exponential Backoff
  - Ping/Pong Handling
  - Error 1006 Handling

#### `messaging/telegram_bot.py`
- **Zweck:** Telegram Notifications
- **Features:**
  - Trade Alerts
  - Error Notifications
  - Health Reports

#### `api/rate_limiter.py`
- **Zweck:** API Rate Limiting
- **Features:**
  - Per-Endpoint Limits
  - Shutdown-Safe
  - Token Bucket Algorithm

### Domain (`src/domain/`)

#### `risk/circuit_breaker.py`
- **Zweck:** Circuit Breaker Pattern
- **Features:**
  - Consecutive Failure Tracking
  - Drawdown Monitoring
  - Kill Switch

#### `risk/validators.py`
- **Zweck:** Risk Validators
- **Features:**
  - Exposure Limits
  - Margin Checks
  - Position Size Validation

#### `services/fee_manager.py`
- **Zweck:** Dynamic Fee Management
- **Features:**
  - Real-Time Fee Fetching
  - Tier-Based Calculation
  - Fee Estimation

#### `services/volatility_monitor.py`
- **Zweck:** Volatility Monitoring
- **Features:**
  - 24h Volatility Tracking
  - Regime Detection (LOW/NORMAL/HIGH/EXTREME)
  - Dynamic Spread Limits

#### `validation/orderbook_validator.py`
- **Zweck:** Orderbook Validation
- **Features:**
  - Price Impact Simulation
  - Liquidity Checks
  - Spread Validation

### Application (`src/application/`)

#### `execution/parallel_execution.py`
- **Zweck:** Parallel Trade Execution
- **Features:**
  - State Machine (PENDING → COMPLETE)
  - Rollback Processor
  - Ghost Fill Detection
  - Maker-to-Taker Escalation

#### `services/funding_tracker.py`
- **Zweck:** Funding Payment Tracking
- **Features:**
  - Hourly Funding Fetch
  - PnL Updates
  - Database Persistence

#### `services/reconciliation.py`
- **Zweck:** Position Reconciliation
- **Features:**
  - Exchange ↔ DB Sync
  - Orphan Detection
  - Position Fixing

#### `lifecycle/shutdown.py`
- **Zweck:** Graceful Shutdown
- **Features:**
  - Position Verification
  - Clean Close
  - State Persistence

### Utilities (`src/utils/`)

#### `json_logger.py`
- **Zweck:** Structured JSON Logging
- **Features:**
  - JSONL Format
  - Grafana/ELK Compatible
  - Event Categorization

#### `helpers.py`
- **Zweck:** Helper Functions
- **Features:**
  - `safe_float()` - Safe Float Conversion
  - `safe_decimal()` - Safe Decimal Conversion
  - `quantize_usd()` - USD Quantization

---

## 📊 Trading-Strategie

### Entry-Strategie

**Bedingungen (ALLE müssen erfüllt sein):**

1. **Profitabilität:**
   - APY > 20% (dynamisch anpassbar)
   - Breakeven < 8 Stunden
   - Erwarteter Profit > $0.10

2. **Markt-Qualität:**
   - Spread < 0.2% (volatilitätsbasiert angepasst)
   - Genug Liquidität im Orderbook
   - Price Impact < 0.5%

3. **System-Status:**
   - Keine Circuit Breaker aktiv
   - Max Open Trades nicht erreicht
   - Genug Margin verfügbar

### Execution-Strategie

**Leg 1 (Lighter):**
- **Typ:** Maker Order (POST_ONLY)
- **Ziel:** 0% Fees
- **Timeout:** 45s (dynamisch basierend auf Liquidität)
- **Escalation:** Bei Timeout → Taker Order

**Leg 2 (X10):**
- **Typ:** Taker Order (IOC)
- **Ziel:** Garantierter Hedge
- **Fees:** 0.0225%
- **Self-Trade Protection:** Aktiviert

**Rollback:**
- Bei Hedge-Fehler → Sofortiger Close von Leg 1
- Market Order für schnellen Exit

### Exit-Strategie

**Exit-Bedingungen (ODER-Verknüpfung):**

1. **Profit Target:** Net PnL > $0.10
2. **Max Hold Time:** 72 Stunden
3. **APY Crash:** APY < 20%
4. **Funding Flip:** Net-Funding negativ > 4h
5. **Volatility Panic:** 24h Vol > 8%

**Exit-Execution:**
- Lighter: Taker Order (0% Fee)
- X10: Taker Order (0.0225% Fee)
- Beide gleichzeitig für Delta-Neutralität

---

## 🔧 Technische Details

### Datenbank-Schema

**Tabelle: `trades`**
```sql
- id (INTEGER PRIMARY KEY)
- symbol (TEXT)
- entry_time (TIMESTAMP)
- exit_time (TIMESTAMP)
- status (TEXT: pending/open/closed/rollback)
- notional_usd (REAL)
- entry_price_x10 (REAL)
- entry_price_lighter (REAL)
- side_x10 (TEXT)
- side_lighter (TEXT)
- x10_order_id (TEXT)
- lighter_order_id (TEXT)
- final_pnl_usd (REAL)
- funding_pnl_usd (REAL)
- spread_pnl_usd (REAL)
- fees_usd (REAL)
```

**Tabelle: `funding_history`**
```sql
- id (INTEGER PRIMARY KEY)
- symbol (TEXT)
- exchange (TEXT)
- timestamp (TIMESTAMP)
- funding_rate (REAL)
- funding_amount_usd (REAL)
- trade_id (INTEGER)
```

### State Machine

```
PENDING
  │
  ├─► LEG1_SENT
  │     │
  │     ├─► LEG1_FILLED
  │     │     │
  │     │     ├─► LEG2_SENT
  │     │     │     │
  │     │     │     ├─► COMPLETE
  │     │     │     │
  │     │     │     └─► ROLLBACK_QUEUED
  │     │     │           │
  │     │     │           └─► ROLLBACK_IN_PROGRESS
  │     │     │
  │     │     └─► FAILED
  │     │
  │     └─► FAILED
  │
  └─► FAILED
```

### Decimal Precision

**Wichtig:** Alle finanziellen Berechnungen verwenden `decimal.Decimal` statt `float`!

**Beispiel:**
```python
from decimal import Decimal

price = Decimal("4000.50")
quantity = Decimal("0.0375")
notional = price * quantity  # Decimal("150.01875")
```

### WebSocket Management

**Lighter WebSocket:**
- Server sendet Pings alle ~60s
- Bot antwortet mit Pong
- Keine eigenen Pings nötig

**X10 WebSocket:**
- Client sendet Pings alle 15s
- Server antwortet mit Pong
- Auto-Reconnect bei Error 1006

### Rate Limiting

**Lighter:**
- Standard Tier: 1 req/s
- Premium Tier: 50 req/s

**X10:**
- REST: ~10 req/s
- WebSocket: Unlimited

---

## 🚀 Verbesserungspotenziale

### Architektur-Verbesserungen

1. **Clean Architecture:**
   - Klare Trennung: Domain / Application / Infrastructure
   - Dependency Injection
   - Interface-basierte Adapter

2. **Event-Driven Architecture:**
   - Event Bus für lose Kopplung
   - Domain Events (TradeOpened, TradeClosed, etc.)
   - Event Sourcing für Audit-Trail

3. **Microservices:**
   - Separate Services für:
     - Opportunity Detection
     - Trade Execution
     - Position Management
     - Risk Management

### Code-Qualität

1. **Type Safety:**
   - Vollständige Type Hints
   - mypy für Type Checking
   - Pydantic für Data Validation

2. **Testing:**
   - Unit Tests für alle Module
   - Integration Tests für Adapter
   - E2E Tests für Trading-Flow

3. **Documentation:**
   - Docstrings für alle Funktionen
   - API Documentation
   - Architecture Decision Records (ADRs)

### Performance

1. **Caching:**
   - Redis für State Caching
   - Market Data Caching
   - Position Cache mit TTL

2. **Async Optimization:**
   - Connection Pooling
   - Batch Operations
   - Parallel Processing

3. **Database:**
   - Connection Pooling
   - Query Optimization
   - Indexes für häufige Queries

### Features

1. **Multi-Exchange Support:**
   - Plugin-System für neue Exchanges
   - Unified Adapter Interface
   - Cross-Exchange Arbitrage

2. **Advanced Strategies:**
   - Triangular Arbitrage
   - Statistical Arbitrage
   - Market Making

3. **Risk Management:**
   - VaR (Value at Risk) Calculation
   - Stress Testing
   - Backtesting Framework

4. **Monitoring:**
   - Prometheus Metrics
   - Grafana Dashboards
   - Alerting Rules

### DevOps

1. **Containerization:**
   - Docker für Deployment
   - Docker Compose für Development
   - Kubernetes für Production

2. **CI/CD:**
   - Automated Testing
   - Code Quality Checks
   - Automated Deployment

3. **Observability:**
   - Distributed Tracing
   - Log Aggregation
   - Performance Monitoring

---

## 📝 Konfiguration

### Wichtige Config-Parameter

**Position Settings:**
```python
DESIRED_NOTIONAL_USD = 150.0      # Trade-Größe in USD
MAX_OPEN_TRADES = 2                # Max gleichzeitige Positionen
LEVERAGE_MULTIPLIER = 5.0         # Max Leverage
```

**Profitability Filters:**
```python
MIN_APY_FILTER = 0.20             # 20% Minimum APY
MAX_BREAKEVEN_HOURS = 8.0         # Max 8h bis Breakeven
MIN_PROFIT_EXIT_USD = 0.10        # Min Profit zum Schließen
```

**Safety Settings:**
```python
MAX_HOLD_HOURS = 72.0             # Max 72h Haltezeit
MAX_SPREAD_FILTER_PERCENT = 0.002 # 0.2% Max Spread
CB_MAX_DRAWDOWN_PCT = 0.20        # 20% Max Drawdown
VOLATILITY_PANIC_THRESHOLD = 8.0  # 8% = Panic Close
```

**Fees:**
```python
TAKER_FEE_X10 = 0.000225          # 0.0225% X10 Taker
MAKER_FEE_X10 = 0.0               # 0.00% X10 Maker
MAKER_FEE_LIGHTER = 0.0           # 0.00% Lighter Maker
TAKER_FEE_LIGHTER = 0.0           # 0.00% Lighter Taker
```

---

## 🎓 Lessons Learned

### Was gut funktioniert:

1. **Delta-Neutralität:** Der Hedge schützt vor Preis-Risiko
2. **Maker-First:** 0% Fees auf Lighter sparen Geld
3. **State Persistence:** SQLite macht Bot crash-sicher
4. **Structured Logging:** JSONL macht Debugging einfach

### Was verbessert werden sollte:

1. **Code-Duplikation:** Viele ähnliche Funktionen in verschiedenen Modulen
2. **Error Handling:** Nicht alle Edge Cases abgedeckt
3. **Testing:** Zu wenig automatische Tests
4. **Documentation:** Code-Kommentare teilweise veraltet

### Kritische Punkte:

1. **Ghost Fills:** Lighter meldet manchmal "cancelled" obwohl gefüllt
2. **API Latency:** X10 Updates sind langsamer als Lighter
3. **Spread Protection:** Wichtig für profitable Trades
4. **Rollback Logic:** Muss schnell und zuverlässig sein

---

## 📚 Weitere Ressourcen

### Externe Dokumentation:

- **Lighter Protocol:** https://apidocs.lighter.xyz
- **X10 Exchange:** https://docs.extended.exchange
- **StarkNet:** https://docs.starknet.io

### Interne Dokumentation:

- `config.py` - Alle Konfigurations-Parameter
- `START_BOT2.bat` - Startup-Script
- `requirements.txt` - Python Dependencies

---

## 🔄 Migration zu neuem Bot

### Empfohlene Vorgehensweise:

1. **Phase 1: Clean Architecture**
   - Neue Ordnerstruktur aufbauen
   - Domain Models definieren
   - Interfaces für Adapter erstellen

2. **Phase 2: Core Features**
   - Opportunity Detection portieren
   - Trade Execution neu implementieren
   - State Management migrieren

3. **Phase 3: Infrastructure**
   - Adapter refactoren
   - Database Layer neu aufbauen
   - WebSocket Manager verbessern

4. **Phase 4: Testing & Deployment**
   - Tests schreiben
   - CI/CD Pipeline aufsetzen
   - Production Deployment

---

**Version:** 1.0  
**Datum:** 2025-12-21  
**Autor:** Bot Architecture Documentation

