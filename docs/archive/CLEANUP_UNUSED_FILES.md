# Ungenutzte Dateien - Cleanup Plan

## ❌ Definitiv ungenutzte Dateien (können gelöscht werden)

Diese Dateien werden nirgendwo importiert oder verwendet:

1. **`src/execution/order_manager.py`** - Verwendet nicht-existierende `src.config.settings`
2. **`src/data/database.py`** - Duplikat (verwendete Version ist `infrastructure/persistence/database.py`)
3. **`src/data/models.py`** - Wird nur in ungenutzten Dateien verwendet
4. **`src/data/repositories.py`** - Wird nur in ungenutzten Dateien verwendet
5. **`src/exchanges/base.py`** - Wird nur in ungenutzten Dateien verwendet
6. **`src/exchanges/lighter/client.py`** - Verwendet nicht-existierende `src.config.settings`
7. **`src/exchanges/x10/client.py`** - Verwendet nicht-existierende `src.config.settings`
8. **`src/infrastructure/exchanges/base_gateway.py`** - Placeholder, nicht verwendet
9. **`src/infrastructure/exchanges/lighter/gateway.py`** - Placeholder mit NotImplementedError
10. **`src/infrastructure/exchanges/x10/gateway.py`** - Placeholder mit NotImplementedError
11. **`src/monitoring/logger.py`** - Wird nur in ungenutzten Dateien verwendet
12. **`src/core/opportunity_finder.py`** - Verwendet nicht-existierende Module

## ⚠️ Neue Architektur (sollten behalten werden)

Diese Dateien sind Teil der neuen Architektur und werden in Zukunft verwendet:

- `src/application/use_cases/*` - Neue Use Cases
- `src/domain/entities/*` - Domain Entities
- `src/domain/value_objects/*` - Value Objects
- `src/domain/services/*` (constitution_guard, opportunity_scorer, etc.) - Domain Services
- `src/domain/rules/constitution.py` - Business Rules

## ✅ Verwendete Dateien (wichtig - NICHT löschen)

- `src/event_loop.py` - Wird verwendet (Zeile 149 in startup.py)
- `src/ws_order_client.py` - Wird in lighter_adapter verwendet
- `src/pnl_utils.py` - Wird verwendet
- `src/latency_arb.py` - Wird verwendet
- `src/open_interest_tracker.py` - Wird verwendet
- `src/api_server.py` - Wird verwendet
- `src/adapters/lighter_client_fix.py` - Wird verwendet
- `src/infrastructure/messaging/event_bus.py` - Wird in use_cases verwendet
