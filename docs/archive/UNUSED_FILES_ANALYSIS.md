# Ungenutzte Dateien Analyse

## Analyse-Methode

Manuelle Prüfung der Imports in `main.py` und `core/startup.py` sowie Suche nach Verwendungen im Code.

## ❌ Definitiv ungenutzte Dateien

### 1. **`src/execution/order_manager.py`**

- **Status**: ❌ NICHT VERWENDET
- **Grund**: Verwendet `src.config.settings` (existiert nicht), `src.data.models`, `src.exchanges.base` - diese werden nicht verwendet
- **Alternative**: Die tatsächliche Execution-Logik ist in `application/execution/parallel_execution.py`
- **Empfehlung**: Kann gelöscht werden

### 2. **`src/data/database.py`**

- **Status**: ❌ NICHT VERWENDET
- **Grund**: Es gibt bereits `src/database.py` (jetzt `infrastructure/persistence/database.py`) die verwendet wird
- **Alternative**: Die verwendete Database ist `infrastructure/persistence/database.py`
- **Empfehlung**: Kann gelöscht werden (Duplikat)

### 3. **`src/data/models.py`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Wird nur in `execution/order_manager.py` verwendet (die selbst nicht verwendet wird)
- **Alternative**: Domain Entities sind in `domain/entities/`
- **Empfehlung**: Prüfen ob noch verwendet, sonst löschen

### 4. **`src/data/repositories.py`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Wird nur in `execution/order_manager.py` verwendet (die selbst nicht verwendet wird)
- **Alternative**: Repositories sind in `infrastructure/persistence/database.py`
- **Empfehlung**: Prüfen ob noch verwendet, sonst löschen

### 5. **`src/exchanges/base.py`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Wird nur in `execution/order_manager.py` und `core/opportunity_finder.py` verwendet
- **Alternative**: Adapters sind in `src/adapters/`
- **Empfehlung**: Prüfen ob `core/opportunity_finder.py` verwendet wird

### 6. **`src/exchanges/lighter/client.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Verwendet `src.config.settings` (existiert nicht)
- **Alternative**: Lighter Adapter ist in `src/adapters/lighter_adapter.py`
- **Empfehlung**: Kann gelöscht werden

### 7. **`src/exchanges/x10/client.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Verwendet `src.config.settings` (existiert nicht)
- **Alternative**: X10 Adapter ist in `src/adapters/x10_adapter.py`
- **Empfehlung**: Kann gelöscht werden

### 8. **`src/infrastructure/exchanges/base_gateway.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `infrastructure/exchanges/lighter/gateway.py` und `x10/gateway.py` verwendet (die selbst nicht verwendet werden)
- **Empfehlung**: Kann gelöscht werden (Placeholder)

### 9. **`src/infrastructure/exchanges/lighter/gateway.py`**

- **Status**: ❌ NICHT VERWENDET
- **Grund**: Placeholder mit `NotImplementedError`, wird nirgendwo verwendet
- **Empfehlung**: Kann gelöscht werden

### 10. **`src/infrastructure/exchanges/x10/gateway.py`**

- **Status**: ❌ NICHT VERWENDET
- **Grund**: Placeholder mit `NotImplementedError`, wird nirgendwo verwendet
- **Empfehlung**: Kann gelöscht werden

### 11. **`src/monitoring/logger.py`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Wird nur in `data/database.py`, `data/repositories.py`, `execution/order_manager.py`, `exchanges/lighter/client.py`, `exchanges/x10/client.py`, `core/opportunity_finder.py` verwendet - diese werden selbst nicht verwendet
- **Alternative**: Standard `logging` wird überall verwendet
- **Empfehlung**: Kann gelöscht werden

### 12. **`src/core/opportunity_finder.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Verwendet `src.config.settings`, `src.data.models`, `src.exchanges.base`, `src.risk.validators` - diese werden nicht verwendet
- **Alternative**: Opportunity-Finding ist in `core/opportunities.py`
- **Empfehlung**: Kann gelöscht werden

### 13. **`src/application/use_cases/open_trade.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Neue Architektur, wird noch nicht verwendet
- **Empfehlung**: Behalten für zukünftige Migration

### 14. **`src/application/use_cases/close_trade.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Neue Architektur, wird noch nicht verwendet
- **Empfehlung**: Behalten für zukünftige Migration

### 15. **`src/application/use_cases/manage_position.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Neue Architektur, wird noch nicht verwendet
- **Empfehlung**: Behalten für zukünftige Migration

### 16. **`src/domain/entities/*`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Werden in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 17. **`src/domain/value_objects/*`**

- **Status**: ⚠️ TEILWEISE VERWENDET
- **Grund**: Werden in `application/use_cases/` und `domain/services/` verwendet
- **Empfehlung**: Behalten (werden in neuen Services verwendet)

### 18. **`src/domain/services/constitution_guard.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 19. **`src/domain/services/opportunity_scorer.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 20. **`src/domain/services/pnl_calculator.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 21. **`src/domain/services/position_sizer.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 22. **`src/domain/services/risk_evaluator.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 23. **`src/domain/services/trade_invariants.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `application/use_cases/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 24. **`src/domain/rules/constitution.py`**

- **Status**: ⚠️ NICHT VERWENDET
- **Grund**: Wird nur in `domain/services/` verwendet (die selbst noch nicht verwendet werden)
- **Empfehlung**: Behalten für zukünftige Migration

### 25. **Leere `__init__.py` Dateien**

- **Status**: ✅ OK
- **Grund**: Package-Initialisierung, werden implizit verwendet
- **Empfehlung**: Behalten

## ✅ Verwendete Dateien (wichtig)

- ✅ `src/main.py` - Entry Point
- ✅ `src/core/startup.py` - Bot Initialisierung
- ✅ `src/core/opportunities.py` - Opportunity Finding
- ✅ `src/core/trading.py` - Trade Execution
- ✅ `src/core/trade_management.py` - Position Management
- ✅ `src/core/monitoring.py` - Monitoring Loops
- ✅ `src/adapters/*` - Exchange Adapters (werden verwendet)
- ✅ `src/event_loop.py` - Wird verwendet (Zeile 149 in startup.py)
- ✅ `src/ws_order_client.py` - Wird in lighter_adapter verwendet
- ✅ `src/pnl_utils.py` - Wird verwendet
- ✅ `src/latency_arb.py` - Wird verwendet
- ✅ `src/open_interest_tracker.py` - Wird verwendet
- ✅ `src/api_server.py` - Wird verwendet

## 📋 Zusammenfassung

### Kann sicher gelöscht werden:

1. `src/execution/order_manager.py` - Nicht verwendet
2. `src/data/database.py` - Duplikat
3. `src/exchanges/lighter/client.py` - Nicht verwendet
4. `src/exchanges/x10/client.py` - Nicht verwendet
5. `src/infrastructure/exchanges/base_gateway.py` - Placeholder
6. `src/infrastructure/exchanges/lighter/gateway.py` - Placeholder
7. `src/infrastructure/exchanges/x10/gateway.py` - Placeholder
8. `src/monitoring/logger.py` - Nicht verwendet
9. `src/core/opportunity_finder.py` - Nicht verwendet

### Sollte geprüft werden:

- `src/data/models.py` - Nur in ungenutzten Dateien verwendet
- `src/data/repositories.py` - Nur in ungenutzten Dateien verwendet
- `src/exchanges/base.py` - Nur in ungenutzten Dateien verwendet

### Sollte behalten werden (zukünftige Migration):

- `src/application/use_cases/*` - Neue Architektur
- `src/domain/entities/*` - Neue Architektur
- `src/domain/value_objects/*` - Neue Architektur
- `src/domain/services/*` (constitution_guard, opportunity_scorer, etc.) - Neue Architektur
- `src/domain/rules/constitution.py` - Neue Architektur
