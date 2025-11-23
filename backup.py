#!/usr/bin/env python3
"""
Einfaches Backup-Tool für FundingBot
Usage: python backup.py
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

# Dateien, die gesichert werden sollen
FILES_TO_BACKUP = [
    "scripts/monitor_funding.py",  # ✅ WICHTIG!
    "src/adapters/lighter_adapter.py",
    "src/adapters/x10_adapter.py",
    "src/adapters/base_adapter.py",
    "src/prediction_v2.py",
    "src/latency_arb.py",
    "src/rate_limiter.py",
    "src/state_manager.py",
    "src/parallel_execution.py",
    "src/adaptive_threshold.py",
    "src/volatility_monitor.py",
    "src/prediction.py",
    "src/websocket_manager.py",
    "scripts/monitor_funding_final.py",
    "src/account_manager.py",
    "src/telegram_bot.py",
    "config.py",
]

# Backup-Verzeichnis
BACKUP_DIR = Path("backups")

def create_backup():
    """Erstellt timestamped Backup aller wichtigen Dateien"""
    
    # Timestamp für eindeutige Backup-Namen
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = BACKUP_DIR / timestamp
    
    # Erstelle Backup-Ordner
    backup_folder.mkdir(parents=True, exist_ok=True)
    
    print(f"📦 Erstelle Backup: {backup_folder}")
    
    backed_up = 0
    for file_path in FILES_TO_BACKUP:
        source = Path(file_path)
        
        if not source.exists():
            print(f"⚠️  Überspringe: {file_path} (nicht gefunden)")
            continue
        
        # Behalte Ordnerstruktur bei
        dest = backup_folder / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        # Kopiere Datei
        shutil.copy2(source, dest)
        print(f"✅ {file_path}")
        backed_up += 1
    
    # Erstelle Info-Datei
    info_file = backup_folder / "backup_info.txt"
    with open(info_file, "w", encoding="utf-8") as f:
        f.write(f"Backup erstellt: {datetime.now()}\n")
        f.write(f"Dateien gesichert: {backed_up}\n")
        f.write(f"\nInhalt:\n")
        for file in FILES_TO_BACKUP:
            f.write(f"  - {file}\n")
    
    print(f"\n✅ Backup abgeschlossen: {backed_up} Dateien")
    print(f"📍 Speicherort: {backup_folder.absolute()}")
    
    # Zeige die letzten 5 Backups
    list_recent_backups()

def list_recent_backups(n=5):
    """Listet die letzten N Backups auf"""
    if not BACKUP_DIR.exists():
        return
    
    backups = sorted(BACKUP_DIR.iterdir(), reverse=True)[:n]
    
    if backups:
        print(f"\n📚 Letzte {len(backups)} Backups:")
        for i, backup in enumerate(backups, 1):
            size = sum(f.stat().st_size for f in backup.rglob('*') if f.is_file())
            size_mb = size / 1024 / 1024
            print(f"  {i}. {backup.name} ({size_mb:.2f} MB)")

def restore_backup(timestamp: str):
    """Stellt ein Backup wieder her"""
    backup_folder = BACKUP_DIR / timestamp
    
    if not backup_folder.exists():
        print(f"❌ Backup nicht gefunden: {timestamp}")
        list_recent_backups(10)
        return
    
    print(f"⚠️  ACHTUNG: Alle aktuellen Dateien werden überschrieben!")
    confirm = input(f"Backup '{timestamp}' wiederherstellen? (yes/no): ")
    
    if confirm.lower() != "yes":
        print("❌ Abgebrochen")
        return
    
    restored = 0
    for file_path in FILES_TO_BACKUP:
        source = backup_folder / file_path
        dest = Path(file_path)
        
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
    
    if len(sys.argv) > 1:
        # Restore-Modus
        if sys.argv[1] == "restore":
            if len(sys.argv) < 3:
                print("Usage: python backup.py restore <timestamp>")
                list_recent_backups(10)
            else:
                restore_backup(sys.argv[2])
        elif sys.argv[1] == "list":
            list_recent_backups(10)
        else:
            print("Unknown command. Use: backup.py [restore <timestamp> | list]")
    else:
        # Backup-Modus (Standard)
        create_backup()