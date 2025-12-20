# 📁 Projektstruktur - Funding Bot

## 🎯 Hauptverzeichnisse

```
funding-bot/
├── src/                    # Hauptquellcode
│   ├── core/              # Kern-Logik (state, trading, opportunities, etc.)
│   ├── adapters/          # Exchange-Adapter (Lighter, X10)
│   ├── application/       # Application Layer (Services, Execution, Lifecycle)
│   ├── domain/            # Domain Layer (Services, Validation, Risk)
│   ├── infrastructure/    # Infrastructure (Persistence, Messaging, API)
│   └── ...
├── scripts/               # Utility-Scripts
│   ├── audit/            # Audit-Scripts
│   └── archive/          # Alte/ungenutzte Scripts
├── tests/                 # Unit-Tests
├── docs/                  # Dokumentation
│   └── archive/          # Alte Dokumentation
├── data/                  # Runtime-Daten (DBs, State, etc.)
├── logs/                  # Log-Dateien
├── backups/               # Backups (nur neueste 2-3)
├── exports/               # CSV-Exports
└── archive/               # Alte Archive (Cleanup-Daten)
```

## 📝 Wichtige Dateien

- `src/main.py` - Bot Entry Point
- `config.py` - Konfiguration
- `requirements.txt` - Python Dependencies
- `README.md` - Hauptdokumentation
- `START_BOT2.bat` - Windows Start-Script

## 🔧 SDK-Referenzen

Die Verzeichnisse `Extended-TS-SDK-master/` und `lighter-ts-main/` sind **nur Referenzen** für die Entwicklung. Sie werden nicht direkt importiert, sondern dienen als Code-Referenz für die Implementierung der Adapter.

## 🗑️ Aufgeräumt

- ✅ Alte Log-Dateien gelöscht
- ✅ Alte Backups reduziert (nur neueste 2-3 behalten)
- ✅ Refactoring-Dokumentation ins `docs/archive/` verschoben
- ✅ Debug-Scripts ins `scripts/archive/` verschoben
- ✅ Ungenutzte Code-Dateien gelöscht
- ✅ Doppelte Dokumentation entfernt

## 📦 Wartung

- **Backups**: Werden automatisch von `scripts/backup.py` verwaltet
- **Logs**: Werden in `logs/` gespeichert (nicht in Git)
- **Exports**: CSV-Dateien in `exports/` (nicht in Git)

