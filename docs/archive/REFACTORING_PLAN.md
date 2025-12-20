# Bot Refactoring Plan

## Ziel

Strukturverbesserung des Funding Arbitrage Bots durch:

- Klare Trennung der Schichten (Domain, Application, Infrastructure)
- Konsolidierung von Duplikaten
- Verbesserte Wartbarkeit und Testbarkeit

## Aktuelle Probleme

1. **Root-Level Dateien**: Viele Dateien im `src/` Root sollten in Schichten organisiert sein
2. **Duplikate**: `database.py` existiert sowohl in `src/` als auch `src/data/`
3. **Unklare Struktur**: Mix aus alten und neuen Architekturen
4. **Import-Chaos**: Viele direkte Imports statt klarer Abhängigkeiten

## Refactoring-Strategie

### Phase 1: Infrastructure Layer

- [x] `adapters/` → `infrastructure/exchanges/` (bereits teilweise vorhanden)
- [ ] `database.py` → `infrastructure/persistence/database.py` (konsolidieren)
- [ ] `websocket_manager.py` → `infrastructure/messaging/websocket_manager.py`
- [ ] `rate_limiter.py` → `infrastructure/api/rate_limiter.py`
- [ ] `telegram_bot.py` → `infrastructure/messaging/telegram_bot.py`

### Phase 2: Application Layer

- [x] `use_cases/` bereits vorhanden
- [ ] `parallel_execution.py` → `application/execution/parallel_execution.py`
- [ ] `batch_manager.py` → `application/execution/batch_manager.py`
- [ ] `funding_tracker.py` → `application/services/funding_tracker.py`
- [ ] `reconciliation.py` → `application/services/reconciliation.py`

### Phase 3: Domain Layer

- [x] `domain/` bereits vorhanden
- [ ] `volatility_monitor.py` → `domain/services/volatility_monitor.py`
- [ ] `fee_manager.py` → `domain/services/fee_manager.py`
- [ ] `adaptive_threshold.py` → `domain/services/adaptive_threshold.py`

### Phase 4: Core Layer (Cleanup)

- [x] `core/` bereits vorhanden
- [ ] `state_manager.py` → `infrastructure/persistence/state_manager.py` (oder in core behalten)
- [ ] `shutdown.py` → `application/lifecycle/shutdown.py`
- [ ] `account_manager.py` → `application/services/account_manager.py`

### Phase 5: Utilities

- [ ] `utils/` konsolidieren und aufräumen
- [ ] `validation/` → `domain/validation/` oder `infrastructure/validation/`
- [ ] `risk/` → `domain/risk/` (bereits teilweise vorhanden)

### Phase 6: Cleanup

- [ ] Unbenutzte Dateien entfernen
- [ ] Alle Imports aktualisieren
- [ ] Tests aktualisieren

## Neue Struktur

```
src/
├── domain/                    # Business Logic
│   ├── entities/              # Domain Entities
│   ├── services/              # Domain Services
│   ├── rules/                 # Business Rules
│   └── value_objects/         # Value Objects
│
├── application/               # Application Layer
│   ├── use_cases/             # Use Cases
│   ├── execution/             # Trade Execution
│   ├── services/              # Application Services
│   └── lifecycle/             # Startup/Shutdown
│
├── infrastructure/            # Infrastructure Layer
│   ├── exchanges/              # Exchange Adapters
│   ├── persistence/            # Database & State
│   ├── messaging/              # WebSocket, Telegram
│   ├── api/                    # API Clients
│   └── monitoring/             # Monitoring
│
├── core/                      # Core Bot Logic (Legacy, wird schrittweise aufgelöst)
│
└── interfaces/                # External Interfaces
    ├── api/                    # REST API
    └── cli/                    # CLI Interface
```

## Implementierung

Die Refactoring-Schritte werden schrittweise durchgeführt, um die Funktionalität zu erhalten.
