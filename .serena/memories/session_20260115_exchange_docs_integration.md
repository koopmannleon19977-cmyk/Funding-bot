# Exchange Documentation Integration - Session 2026-01-15

## Erstellte Dokumentation

### docs/memory/EXCHANGE_API_REFERENCE.md (790 Zeilen)
Umfassende API-Referenz mit:
- Lighter & X10 Rate Limits (detailliert mit Endpoint-Gewichten)
- Funding Rate Berechnungsformeln für beide Exchanges
- Liquidation-Mechanismen und Waterfall-Prozesse
- Error Codes, Transaction Types und Data Structures
- SDK-Nutzung mit vollständigen Code-Beispielen
- WebSocket-Limits, Channels und Protokoll-Details
- Margin Schedule und Market Groups für X10

### docs/memory/REPO_INDEX.md
Token-optimierter Repository-Index (94% Reduktion):
- Architektur-Diagramm (Application → Service → Adapter → Domain)
- Module-Index mit File:Line Referenzen
- Kritische Code-Pfade für Entry/Exit Flow
- Exit Rule Priority (First-Hit-Wins)

### docs/memory/SYMBOL_MAP.md
Symbol Quick-Reference:
- Alle Domain-Klassen mit File:Line
- Ports und Adapters
- Service-Klassen
- Key Functions nach Kategorie
- Config Keys

## Kritische Erkenntnisse

### Lighter Rate Limits (DETAILLIERT)
| Endpoint | Gewicht |
|----------|---------|
| sendTx, sendTxBatch, nextNonce | 6 |
| trades, recentTrades | 1200 |
| Standard endpoints | 300 |

Premium: 24K weighted/min
Standard: 60/min
WebSocket: 200 msg/min (sendTx NICHT gezählt!)

### X10 Kritische Parameter
- Pagination: MAX 100 pro Request (IMMER paginate!)
- Maker Rebates: 0.0025%-0.0175% (Volume-basiert)
- 6 Market Groups mit unterschiedlichen Margins
- Insurance Fund: Max 15%/Tag Depletion global

### Funding Rate Unterschiede
| Exchange | Samples | TWAP Interval | Max Cap |
|----------|---------|---------------|---------|
| Lighter | 60/hour | 1/min | ±0.5% |
| X10 | 720/hour | 1/5s | 1-3% (Group) |

## Lokale Doc-Quellen

### external_repos/lighter/ (87 Dateien)
Wichtigste:
- apidocs.lighter.xyz_docs_rate-limits.md
- apidocs.lighter.xyz_docs_data-structures-constants-and-errors.md
- docs.lighter.xyz_perpetual-futures_funding.md
- docs.lighter.xyz_perpetual-futures_liquidations-and-llp-insurance-fund.md
- apidocs.lighter.xyz_docs_websocket-reference.md

### external_repos/extended/ (47 Dateien)
Wichtigste:
- docs.extended.exchange_extended-resources_trading_funding-payments.md
- docs.extended.exchange_extended-resources_trading_margin-schedule.md
- docs.extended.exchange_extended-resources_trading_liquidation-logic.md
- api.docs.extended.exchange_#extended-api-documentation.*.md

## Verwendung

Bei Exchange-Problemen:
1. docs/memory/EXCHANGE_API_REFERENCE.md konsultieren
2. Bei Bedarf lokale Docs in external_repos/ durchsuchen
3. Für Code-Navigation: docs/memory/SYMBOL_MAP.md

Session completed: 2026-01-15
