#!/usr/bin/env python3
"""
Einfaches Backup-Tool für FundingBot
Usage: python backup.py
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

# Dateien, die gesichert werden sollen (vom Nutzer spezifiziert + vorhandene Skripte)
# Duplikate werden automatisch entfernt.
RAW_FILES_TO_BACKUP = [
    # Explicit user list (absolute paths converted to relative below if needed)
    r"c:\Users\koopm\funding-bot\src\__init__.py",
    r"c:\Users\koopm\funding-bot\src\account_manager.py",
    r"c:\Users\koopm\funding-bot\src\adaptive_threshold.py",
    r"c:\Users\koopm\funding-bot\src\btc_correlation.py",
    r"c:\Users\koopm\funding-bot\src\database.py",
    r"c:\Users\koopm\funding-bot\src\event_loop.py",
    r"c:\Users\koopm\funding-bot\src\funding_history_collector.py",
    r"c:\Users\koopm\funding-bot\src\funding_tracker.py",
    r"c:\Users\koopm\funding-bot\src\kelly_sizing.py",
    r"c:\Users\koopm\funding-bot\src\latency_arb.py",
    r"c:\Users\koopm\funding-bot\src\open_interest_tracker.py",
    r"c:\Users\koopm\funding-bot\src\parallel_execution.py",
    r"c:\Users\koopm\funding-bot\src\prediction.py",
    r"c:\Users\koopm\funding-bot\src\prediction_v2.py",
    r"c:\Users\koopm\funding-bot\src\rate_limiter.py",
    r"c:\Users\koopm\funding-bot\src\state_manager.py",
    r"c:\Users\koopm\funding-bot\src\telegram_bot.py",
    r"c:\Users\koopm\funding-bot\src\volatility_monitor.py",
    r"c:\Users\koopm\funding-bot\src\websocket_manager.py",
    r"c:\Users\koopm\funding-bot\src\adapters\base_adapter.py",
    r"c:\Users\koopm\funding-bot\src\adapters\lighter_adapter.py",
    r"c:\Users\koopm\funding-bot\src\adapters\x10_adapter.py",
    # Existing additional useful files
    "scripts/monitor_funding.py",
    "scripts/monitor_funding_final.py",
    "config.py",
]

BASE_DIR = Path(__file__).resolve().parent

def _normalize_paths(raw_list):
    seen = set()
    normalized = []
    for p in raw_list:
        pp = Path(p)
        if pp.is_absolute():
            try:
                # Make relative to project root (BASE_DIR)
                rel = pp.relative_to(BASE_DIR)
            except ValueError:
                # If path is outside, keep absolute (still backed up preserving structure)
                rel = pp
        else:
            rel = pp
        # Uniform POSIX-style inside zip for consistency
        rel_str = str(rel).replace('\\', '/')
        if rel_str not in seen:
            seen.add(rel_str)
            normalized.append(rel_str)
    return normalized

FILES_TO_BACKUP = _normalize_paths(RAW_FILES_TO_BACKUP)

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
        source = BASE_DIR / file_path if not Path(file_path).is_absolute() else Path(file_path)
        
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
        dest = BASE_DIR / file_path if not Path(file_path).is_absolute() else Path(file_path)
        
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