# Funding Bot - Projekt-Kontext

## Projekt-Übersicht
- **Typ**: Funding Arbitrage Bot (Lighter Premium + X10/Extended)
- **Status**: Production-ready, Phase 1 Complete
- **Tests**: 273 Unit Tests passing

## Letzte Änderungen

### 2026-01-16: Code Cleanup & Linting
- **F401**: 4 unused imports entfernt
- **E501**: 41 line-too-long Fehler behoben
- **Test-Fix**: Rebalance-Cooldown-Mock in Test hinzugefügt
- **Validierung**: 394 Tests pass, alle ruff checks sauber

### 2026-01-16: Reentrant Logging Fix
- **Problem**: Windows Ctrl+C verursachte `RuntimeError: reentrant call`
- **Fix**: Signal-Handler loggt nicht mehr direkt, nur Flag setzen
- **Datei**: `src/funding_bot/app/run.py` (Zeilen 115-140)

### 2026-01-15: Performance-Optimierungen
- Parallel symbol evaluation in scanner.py
- Parallel position fetches in exit_eval.py
- Batch parallelization in ingestion.py
- Single-pass filtering in manager.py
- Depth cache TTL (5s) in fresh.py
- itertools.islice statt list slicing

### 2026-01-15: Bug Fixes
- `get_order()` fehlender symbol Parameter in close.py
- Funding history log spam reduziert (INFO → DEBUG)
- `__main__.py` CLI erstellt

## Kritische Code-Pfade
| Pfad | Risiko | Hinweis |
|------|--------|---------|
| services/positions/close.py | KRITISCH | VWAP, Coordinated Close |
| adapters/exchanges/*/adapter.py | KRITISCH | Exchange-spezifisch |
| app/run.py | MITTEL | Signal Handling, Startup |

## Bekannte Patterns
- **Rebalance**: Notional-basiert (USD), nicht Quantity
- **Exit Rules**: First-hit-wins Semantik
- **Funding Rates**: Bereits Dezimal, NICHT /100 teilen!
- **account_index**: 0 ≠ None bei Lighter
