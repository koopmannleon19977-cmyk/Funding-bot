#!/usr/bin/env python3
"""
Robustes Backup-Tool für FundingBot
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
# KONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Ordner die komplett gesichert werden sollen (rekursiv)
# Format: "Quellordner"
DIRECTORIES_TO_BACKUP = [
    "src",
    "scripts",
    "tests",
    "utils",
    "data",
    "docs",
]

# Einzelne Dateien im Root-Verzeichnis
ROOT_FILES = [
    "config.py",
    "requirements.txt",
    "backup.py",
    "START_BOT2.bat",
    ".env",
    "README.md",
    ".gitignore",
]

# Muster die ignoriert werden (für shutil.ignore_patterns)
IGNORE_PATTERNS = [
    "__pycache__",
    "*.pyc",
    "*.log", 
    "*.db-journal",
    ".DS_Store",
]

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = Path("backups")

def create_backup():
    """Erstellt timestamped Backup"""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = BACKUP_DIR / timestamp
    
    print(f"📦 Erstelle Backup: {timestamp}")
    print(f"   Ziel: {backup_folder}")
    
    # Erstelle Backup-Root
    backup_folder.mkdir(parents=True, exist_ok=True)
    
    backed_up_count = 0
    
    # 1. Sicherere komplette Verzeichnisse
    for dir_name in DIRECTORIES_TO_BACKUP:
        source_dir = BASE_DIR / dir_name
        dest_dir = backup_folder / dir_name
        
        if source_dir.exists() and source_dir.is_dir():
            try:
                # ignore_patterns factory erstellen mit unseren Mustern
                shutil.copytree(
                    source_dir, 
                    dest_dir, 
                    ignore=shutil.ignore_patterns(*IGNORE_PATTERNS), 
                    dirs_exist_ok=True
                )
                
                # Zähle Dateien (nur für Statistik)
                count = sum(1 for _ in dest_dir.rglob('*') if _.is_file())
                print(f"✅ Ordner gesichert: {dir_name} ({count} Dateien)")
                backed_up_count += count
                
            except Exception as e:
                print(f"❌ Fehler beim Sichern von {dir_name}: {e}")
        else:
            print(f"⚠️  Ordner nicht gefunden: {dir_name}")

    # 2. Sicherere Root-Dateien
    print(f"   Sichere Root-Dateien...")
    for filename in ROOT_FILES:
        source = BASE_DIR / filename
        dest = backup_folder / filename
        
        if source.exists() and source.is_file():
            shutil.copy2(source, dest)
            print(f"✅ {filename}")
            backed_up_count += 1
        else:
            # .env ist optional bzgl. Fehlermeldung, aber wir wollens wissen
            if filename == ".env":
                 print(f"ℹ️  .env nicht gefunden (optional)")
            else:
                 print(f"⚠️  Datei nicht gefunden: {filename}")

    # Info File & Stats
    total_size = sum(f.stat().st_size for f in backup_folder.rglob('*') if f.is_file())
    
    print(f"\n{'─' * 50}")
    print(f"✅ Backup Erfolgreich Completed!")
    print(f"   Gesamtdateien: {backed_up_count}")
    print(f"   Gesamtgröße:   {total_size / 1024 / 1024:.2f} MB")
    print(f"   Pfad:          {backup_folder.absolute()}")
    print(f"{'─' * 50}")

    list_recent_backups()

def list_recent_backups(n=5):
    if not BACKUP_DIR.exists():
        return
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], reverse=True)[:n]
    if backups:
        print(f"\n📚 Letzte Backups:")
        for i, backup in enumerate(backups, 1):
            try:
                size_mb = sum(f.stat().st_size for f in backup.rglob('*') if f.is_file()) / 1024 / 1024
                count = sum(1 for f in backup.rglob('*') if f.is_file())
                print(f"  {i}. {backup.name} \t({count} Files, {size_mb:.2f} MB)")
            except: pass

def restore_backup(timestamp: str):
    backup_folder = BACKUP_DIR / timestamp
    if not backup_folder.exists():
        print(f"❌ Backup {timestamp} nicht gefunden!")
        return
        
    print(f"⚠️  ACHTUNG: Restore überschreibt aktuelle Dateien!")
    if input("Wirklich wiederherstellen? (yes/no): ").lower() != "yes":
        return
        
    print("⏳ Restore läuft...")
    # Wir nutzen copytree um alles vom Backup zurück ins Base Dir zu kopieren
    # dirs_exist_ok=True erlaubt überschreiben
    try:
        shutil.copytree(backup_folder, BASE_DIR, dirs_exist_ok=True)
        print("✅ Restore erfolgreich abgeschlossen.")
    except Exception as e:
        print(f"❌ Fehler beim Restore: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) < 3: print("Need timestamp"); exit()
        restore_backup(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        list_recent_backups()
    else:
        create_backup()