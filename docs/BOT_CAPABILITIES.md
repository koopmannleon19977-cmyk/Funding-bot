# Funding Bot - Vollständige Feature-Dokumentation

> **Version:** 2.0.0+ | **Stand:** Januar 2026 | **Architektur:** Hexagonal + DDD

---

## Übersicht

**Funding Bot** ist ein **Delta-Neutral Funding Rate Arbitrage System**, das Funding-Rate-Differenzen zwischen **Lighter** und **X10** Perpetual-Futures-Börsen ausnutzt. Der Bot hält marktneutrale Positionen, indem er gleichzeitig LONG auf einer Börse und SHORT auf der anderen geht.

```
Beispiel:
  Lighter: +35% APY (erhält Funding)
  X10:     -20% APY (zahlt Funding)
  ═══════════════════════════════
  Spread:  +15% APY Gewinn (marktneutral)
```

---

## 1. Kernstrategie

### Delta-Neutral Arbitrage

| Phase | Aktion | Beschreibung |
|-------|--------|--------------|
| **Entry** | Leg1: LONG @ Lighter | Maker-Order, erhält Rebate |
| | Leg2: SHORT @ X10 | Hedge-Order, sofortige Ausführung |
| **Hold** | Funding sammeln | 48-240 Stunden halten |
| **Exit** | Koordinierter Close | Beide Legs parallel schließen |

### Unterstützte Assets
- ETH, BTC, SOL + 200+ Altcoins
- Dynamische Erkennung gemeinsamer Symbole beider Börsen

---

## 2. Unterstützte Börsen

### Lighter Protocol
**Pfad:** `src/funding_bot/adapters/exchanges/lighter/`

| Feature | Details |
|---------|---------|
| API | REST + WebSocket |
| Order-Typen | LIMIT (GTC, IOC, POST_ONLY), MARKET |
| Fees | 0.002% Maker-Rebate |
| Besonderheit | WebSocket-Order-Submission (ultra-low latency) |

### X10 Exchange
**Pfad:** `src/funding_bot/adapters/exchanges/x10/`

| Feature | Details |
|---------|---------|
| API | REST (kein WS für Orders) |
| Order-Typen | LIMIT (GTC, IOC), MARKET |
| Fees | ~0.0225% Taker (mit Referral) |
| Leverage | Bis 20x |

---

## 3. Trading-Fähigkeiten

### Order-Ausführung (Two-Leg System)

```
┌─ PREFLIGHT CHECKS
│  ├─ Depth Gate (Liquidität prüfen)
│  ├─ Spread Gate (<0.16%)
│  └─ Balance Gate (Margin verfügbar)
│
├─ LEG 1: LIGHTER (Maker)
│  ├─ POST_ONLY LIMIT @ Best Bid
│  ├─ Timeout: 30s → Escalate zu IOC
│  └─ WebSocket Fill-Detection
│
├─ LEG 2: X10 (Taker/Hedge)
│  ├─ IOC mit 0.1% Slippage
│  ├─ 3 Retries, +0.05% Slippage pro Retry
│  └─ Max Slippage: 0.3%
│
└─ ROLLBACK (bei Leg2-Failure)
   ├─ Leg1 canceln/schließen
   └─ 15 Min Cooldown
```

### Position Management

| Parameter | Wert | Beschreibung |
|-----------|------|--------------|
| Max Open Trades | 1 | Eine Position gleichzeitig |
| Target Notional | $350 | Positionsgröße |
| Leverage | 7x | Konservativ (6-8x typisch) |
| Hold Time | 48-240h | Min/Max Haltezeit |
| Delta Rebalance | 3% | Auto-Rebalance bei Drift |

---

## 4. Exit-Strategien (9 Mechanismen)

| # | Strategie | Trigger | Beschreibung |
|---|-----------|---------|--------------|
| 1 | **Minimum Hold** | 48h erreicht | Zeit-basierter Exit |
| 2 | **Early Take-Profit** | Price PnL ≥ $4 | Preis-Dislokation nutzen |
| 3 | **Funding Flip** | Funding negativ | Sofort raus bei Verlust |
| 4 | **Z-Score Crash** | APY < mean - 2σ | Statistischer Crash |
| 5 | **Funding Velocity** | Negative Beschleunigung | Proaktive Crash-Erkennung |
| 6 | **Basis Convergence** | Spread -80% | Arbitrage erschöpft |
| 7 | **Net EV Exit** | Exit-Kosten > Future Funding | Wirtschaftlich sinnlos |
| 8 | **Opportunity Cost** | Bessere Opportunity (80%) | Kapital rotieren |
| 9 | **Delta Bound** | Delta Drift > 3% | Hedge-Schutz (Emergency) |

---

## 5. Services-Architektur

### Hauptservices

| Service | Pfad | Funktion |
|---------|------|----------|
| **MarketDataService** | `services/market_data/` | Preise, Funding-Rates, Orderbooks |
| **OpportunityEngine** | `services/opportunities/` | Opportunity-Scanning & Scoring |
| **ExecutionEngine** | `services/execution*.py` | Trade-Ausführung (20+ Module) |
| **PositionManager** | `services/positions/` | Position-Monitoring & Close |
| **FundingTracker** | `services/funding.py` | Funding-Payment-Tracking |
| **Reconciler** | `services/reconcile.py` | DB/Exchange-Synchronisation |
| **HistoricalIngestion** | `services/historical/` | 90-Tage Funding-History |

### Filter-Pipeline (Entry)

```
1. Blacklist Check      → Symbol nicht gesperrt
2. Spread Limit         → <0.16% Entry-Spread
3. Minimum APY          → ≥35% APY
4. Liquidity Gates      → L1-Depth ausreichend
5. Expected Value       → ≥$0.80 Profit nach Fees
6. Breakeven Time       → <24h bis Kostendeckung
7. Dynamic Spread       → Enger bei höherem APY
```

---

## 6. Konfiguration

### Wichtigste Parameter

**Pfad:** `src/funding_bot/config/config.yaml`

#### Trading
```yaml
live_trading: true
desired_notional_usd: 350
max_open_trades: 1
leverage_multiplier: 7.0
min_apy_filter: 0.35          # 35% APY Minimum
min_expected_profit_entry_usd: 0.80
min_hold_seconds: 172800      # 48 Stunden
max_hold_hours: 240           # 10 Tage
```

#### Spread & Liquidity
```yaml
max_spread_filter_percent: 0.0016  # 0.16%
depth_gate_enabled: true
depth_gate_mode: IMPACT
min_l1_notional_usd: 500
min_l1_notional_multiple: 1.2
```

#### Execution
```yaml
maker_order_timeout_seconds: 30
taker_order_slippage: 0.001       # 0.1%
leg1_escalate_to_taker_after_seconds: 15
hedge_ioc_max_attempts: 3
```

#### Risk Management
```yaml
max_consecutive_failures: 3
max_drawdown_pct: 0.20            # 20%
max_exposure_pct: 80.0
min_free_margin_pct: 0.05         # 5% Buffer
broken_hedge_cooldown_seconds: 900  # 15 Min
```

---

## 7. CLI-Befehle

```bash
# Standard-Betrieb
python main.py

# Diagnose
python -m funding_bot doctor

# Positionen synchronisieren
python -m funding_bot reconcile

# Alle Positionen schließen (Emergency)
python -m funding_bot close-all

# Historische Daten laden (90 Tage)
python -m funding_bot backfill
```

---

## 8. Monitoring & Observability

### Logging
**Pfad:** `src/funding_bot/observability/logging.py`

- Dual-Output: Console + JSON (strukturiert)
- Log-Tags: `[TRADE]`, `[PROFIT]`, `[HEALTH]`, `[SCAN]`
- Sensitive-Data-Masking (API-Keys)

### Prometheus Metrics
```
close_operations_total        # Close-Operationen nach Grund
close_duration_seconds        # Close-Dauer Histogram
close_pnl_usd                 # Realisierte PnL Distribution
rebalance_operations_total    # Rebalance-Counter
```

### Event Bus
```python
TradeStateChanged   # Trade wechselt Status
FundingCollected    # Funding-Payment erhalten
PositionReconciled  # Position synchronisiert
AlertEvent          # Kritische Alerts (→ Telegram)
TradeClosed         # Trade geschlossen mit PnL
```

---

## 9. Datenbank-Schema

**Pfad:** `src/funding_bot/adapters/store/sqlite/`

### Tabellen

| Tabelle | Beschreibung |
|---------|--------------|
| `trades` | Alle Trades mit Status, Legs, PnL |
| `trade_events` | Event-Log pro Trade (JSON) |
| `funding_events` | Funding-Payments pro Trade |
| `funding_history` | Historische Funding-Rates |
| `crash_detection` | APY-Crash-Events |

### Trade-Status
```
PENDING → OPENING → OPEN → CLOSING → CLOSED
                         ↘ REJECTED / FAILED
```

---

## 10. Sicherheit & Risikokontrolle

### Circuit Breakers

| Trigger | Aktion |
|---------|--------|
| 3 Failed Trades | 15 Min Pause |
| 20% Drawdown | Trading pausiert |
| <5% Free Margin | Trading pausiert |
| Broken Hedge | 15 Min Cooldown |
| 10 WS-Failures | 60s Open Circuit |

### Validation Gates

**Entry:**
- Depth Gate (Liquidität)
- Spread Gate (<0.16%)
- Balance Gate (Margin)
- Symbol Blacklist
- Cooldown Gate

**Execution:**
- Leg1 Fill (oder Escalate)
- Leg2 Fill (mit Retries)
- Microfill Grace Period
- Automatic Rollback

### Liquidation Monitoring
- 15% Safety Buffer zur Liquidation
- Check alle 15 Sekunden
- Emergency Close bei Gefahr

---

## 11. Performance

| Metrik | Ziel |
|--------|------|
| Opportunity Scan | <5s pro Zyklus |
| Order Submission | <100ms |
| Hedge Execution | <2s nach Leg1-Fill |
| Market Data Refresh | 15-30s |
| Memory | 200-500 MB |
| DB Growth | ~5 MB/Woche |

---

## 12. Verzeichnisstruktur

```
src/funding_bot/
├── adapters/
│   ├── exchanges/
│   │   ├── lighter/      # Lighter-Integration
│   │   └── x10/          # X10-Integration
│   ├── messaging/        # Event Bus
│   └── store/            # SQLite-Adapter
├── app/
│   └── supervisor/       # Main Loop, Guards
├── config/               # YAML-Config
├── domain/               # Business-Logik, Rules
├── observability/        # Logging, Metrics
├── ports/                # Interface-Definitionen
├── services/
│   ├── execution*.py     # Trade-Ausführung (20+ Files)
│   ├── historical/       # Backfill-Service
│   ├── market_data/      # Preise, Orderbooks
│   ├── metrics/          # KPI-Tracking
│   ├── opportunities/    # Opportunity-Engine
│   └── positions/        # Position-Management
└── utils/                # Helper-Funktionen
```

---

## 13. Schnellstart

```bash
# Setup
git clone <repo>
cd funding-bot
python -m venv venv
.\venv\Scripts\activate

# Dependencies
pip install -e .
pip install x10-python-trading-starknet>=0.0.17
pip install git+https://github.com/elliottech/lighter-python.git

# Konfiguration
cp .env.example .env
# .env mit API-Keys befüllen

# Starten
python main.py
```

---

## Zusammenfassung

| Aspekt | Details |
|--------|---------|
| **Strategie** | Delta-neutral Funding Arbitrage |
| **Börsen** | Lighter + X10 |
| **Assets** | 200+ Paare |
| **Entry** | 35% APY, <0.16% Spread, Depth Gates |
| **Exit** | 9 Strategien (Zeit, APY, Velocity, Z-Score, etc.) |
| **Risiko** | Circuit Breakers, Drawdown-Limits, Liquidation-Monitor |
| **Tests** | 273+ Unit Tests |
| **Architektur** | Hexagonal + DDD, async/await |
