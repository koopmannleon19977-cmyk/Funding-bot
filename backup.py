#!/usr/bin/env python3
"""
Einfaches Backup-Tool für FundingBot
Usage: 
    python backup.py          # Erstellt ein Backup
    python backup.py list     # Zeigt alle Backups
    python backup.py restore <timestamp>  # Stellt Backup wieder her
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# DATEIEN DIE GESICHERT WERDEN (vollständige Liste aller wichtigen Dateien)
# ═══════════════════════════════════════════════════════════════════════════════
FILES_TO_BACKUP = [
    # ─────────────────────────────────────────────────────────────────────────
    # ROOT: Config & Hauptdateien
    # ─────────────────────────────────────────────────────────────────────────
    "config.py",
    "requirements.txt",
    "backup.py",
    "START_BOT2.bat",
    ".env",  # WICHTIG: API Keys (nicht in Git!)
    
    # ─────────────────────────────────────────────────────────────────────────
    # SCRIPTS: Bot-Skripte und Utilities
    # ─────────────────────────────────────────────────────────────────────────
    "scripts/monitor_funding_final.py",  # HAUPTSKRIPT
    "scripts/force_close.py",
    "scripts/full_cleanup.py",
    
    # ─────────────────────────────────────────────────────────────────────────
    # SRC: Core Modules
    # ─────────────────────────────────────────────────────────────────────────
    "src/__init__.py",
    "src/account_manager.py",
    "src/adaptive_threshold.py",
    "src/btc_correlation.py",
    "src/database.py",
    "src/event_loop.py",
    "src/fee_manager.py",
    "src/fee_tracker.py",
    "src/funding_history_collector.py",
    "src/funding_tracker.py",
    "src/kelly_sizing.py",
    "src/latency_arb.py",
    "src/open_interest_tracker.py",
    "src/orderbook_utils.py",
    "src/parallel_execution.py",
    "src/prediction.py",
    "src/prediction_v2.py",
    "src/rate_limiter.py",
    "src/state_manager.py",
    "src/telegram_bot.py",
    "src/volatility_monitor.py",
    "src/websocket_manager.py",
    
    # ─────────────────────────────────────────────────────────────────────────
    # SRC/ADAPTERS: Exchange Adapters
    # ─────────────────────────────────────────────────────────────────────────
    "src/adapters/__init__.py",
    "src/adapters/base_adapter.py",
    "src/adapters/lighter_adapter.py",
    "src/adapters/x10_adapter.py",
    
    # ─────────────────────────────────────────────────────────────────────────
    # DATA: Datenbank & State
    # ─────────────────────────────────────────────────────────────────────────
    "data/trades.db",
    "data/kelly_history.json",
    "data/prediction_history.json",
    "data/state_snapshot.json",
]

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = Path("backups")

def create_backup():
    """Erstellt timestamped Backup aller wichtigen Dateien"""
    
    # Timestamp für eindeutige Backup-Namen
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = BACKUP_DIR / timestamp
    
    # Erstelle Backup-Ordner
    backup_folder.mkdir(parents=True, exist_ok=True)
    
    print(f"📦 Erstelle Backup: {backup_folder}")
    print(f"   Sichere {len(FILES_TO_BACKUP)} Dateien...\n")
    
    backed_up = 0
    skipped = []
    
    for file_path in FILES_TO_BACKUP:
        source = BASE_DIR / file_path
        
        if not source.exists():
            skipped.append(file_path)
            continue
        
        # Behalte Ordnerstruktur bei
        dest = backup_folder / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        # Kopiere Datei
        shutil.copy2(source, dest)
        size_kb = source.stat().st_size / 1024
        print(f"✅ {file_path} ({size_kb:.1f} KB)")
        backed_up += 1
    
    # Erstelle Info-Datei
    info_file = backup_folder / "backup_info.txt"
    with open(info_file, "w", encoding="utf-8") as f:
        f.write(f"═══════════════════════════════════════════════════════════\n")
        f.write(f"  FUNDING BOT BACKUP\n")
        f.write(f"═══════════════════════════════════════════════════════════\n")
        f.write(f"Erstellt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Dateien gesichert: {backed_up}\n")
        f.write(f"Dateien übersprungen: {len(skipped)}\n\n")
        f.write(f"Gesicherte Dateien:\n")
        for file in FILES_TO_BACKUP:
            if file not in skipped:
                f.write(f"  ✅ {file}\n")
        if skipped:
            f.write(f"\nÜbersprungen (nicht gefunden):\n")
            for file in skipped:
                f.write(f"  ⚠️ {file}\n")
    
    # Berechne Gesamtgröße
    total_size = sum(f.stat().st_size for f in backup_folder.rglob('*') if f.is_file())
    total_mb = total_size / 1024 / 1024
    
    print(f"\n{'─' * 50}")
    print(f"✅ Backup abgeschlossen: {backed_up} Dateien ({total_mb:.2f} MB)")
    
    if skipped:
        print(f"⚠️  Übersprungen: {len(skipped)} Dateien (nicht gefunden)")
    
    print(f"📍 Speicherort: {backup_folder.absolute()}")
    
    # Zeige die letzten Backups
    list_recent_backups()

def list_recent_backups(n=5):
    """Listet die letzten N Backups auf"""
    if not BACKUP_DIR.exists():
        print("📭 Keine Backups vorhanden")
        return
    
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], reverse=True)[:n]
    
    if backups:
        print(f"\n📚 Letzte {len(backups)} Backups:")
        for i, backup in enumerate(backups, 1):
            size = sum(f.stat().st_size for f in backup.rglob('*') if f.is_file())
            size_mb = size / 1024 / 1024
            file_count = sum(1 for f in backup.rglob('*') if f.is_file())
            print(f"  {i}. {backup.name} ({file_count} Dateien, {size_mb:.2f} MB)")
    else:
        print("📭 Keine Backups vorhanden")

def restore_backup(timestamp: str):
    """Stellt ein Backup wieder her"""
    backup_folder = BACKUP_DIR / timestamp
    
    if not backup_folder.exists():
        print(f"❌ Backup nicht gefunden: {timestamp}")
        list_recent_backups(10)
        return
    
    print(f"\n⚠️  ACHTUNG: Alle aktuellen Dateien werden überschrieben!")
    print(f"   Backup: {timestamp}")
    confirm = input(f"\nWiederherstellen? (yes/no): ")
    
    if confirm.lower() != "yes":
        print("❌ Abgebrochen")
        return
    
    restored = 0
    for file_path in FILES_TO_BACKUP:
        source = backup_folder / file_path
        dest = BASE_DIR / file_path
        
        if not source.exists():
            continue
        
        # Erstelle Zielordner falls nötig
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        # Kopiere zurück
        shutil.copy2(source, dest)
        print(f"✅ Wiederhergestellt: {file_path}")
        restored += 1
    
    print(f"\n✅ Backup wiederhergestellt: {restored} Dateien")

if __name__ == "__main__":
    import sys
    
    print("═══════════════════════════════════════════════════════════")
    print("  🔒 FUNDING BOT BACKUP TOOL")
    print("═══════════════════════════════════════════════════════════\n")
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        
        if cmd == "restore":
            if len(sys.argv) < 3:
                print("Usage: python backup.py restore <timestamp>")
                list_recent_backups(10)
            else:
                restore_backup(sys.argv[2])
        elif cmd == "list":
            list_recent_backups(10)
        else:
            print("Unbekannter Befehl.\n")
            print("Usage:")
            print("  python backup.py              # Backup erstellen")
            print("  python backup.py list         # Alle Backups anzeigen")
            print("  python backup.py restore <ts> # Backup wiederherstellen")
    else:
        # Backup-Modus (Standard)
        create_backup()