#!/usr/bin/env python3
"""
Robustes Backup-Tool für FundingBot
Usage: 
    python backup.py          # Erstellt ein Backup (Alles ausser Ignoriertes)
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

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = BASE_DIR / "backups"

# Ordner, die NICHT gesichert werden sollen
EXCLUDE_DIRS = {
    "backups",       # Sich selbst nicht sichern
    ".venv",         # Virtual Environment
    ".git",          # Git History (optional, meist zu groß)
    ".idea",         # IDE Settings
    "__pycache__",   # Python Cache
    ".pytest_cache", # Test Cache
    ".mypy_cache",   # Type Check Cache
    "logs",          # Logs nicht ins Backup (optional)
}

# Dateimuster, die ignoriert werden solllen
IGNORE_PATTERNS = [
    "*.pyc",
    "*.log", 
    "*.db-journal",
    ".DS_Store",
    "*.tmp",
    "Thumbs.db"
]

def create_backup():
    """Erstellt timestamped Backup von ALLEM im Bot-Ordner"""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_folder = BACKUP_DIR / timestamp
    
    print(f"📦 Erstelle Backup: {timestamp}")
    print(f"   Quelle: {BASE_DIR}")
    print(f"   Ziel:   {dest_folder}")
    
    # Sicherstellen, dass Backup-Dir existiert
    if not BACKUP_DIR.exists():
        BACKUP_DIR.mkdir()

    try:
        # Wir kopieren alles von BASE_DIR nach dest_folder
        # Dabei nutzen wir shutil.copytree mit ignore_function, um Excludes zu filtern
        
        def _ignore_filter(path, names):
            ignored = set()
            path_obj = Path(path)
            
            # Check Ordner Excludes (nur auf Top-Level oder rekursiv?)
            # Hier: Wir prüfen, ob der aktuelle Ordnername in EXCLUDE_DIRS ist.
            for name in names:
                if name in EXCLUDE_DIRS:
                    ignored.add(name)
            
            # Check Patterns (Dateien)
            ignore_patterns = shutil.ignore_patterns(*IGNORE_PATTERNS)(path, names)
            ignored.update(ignore_patterns)
            
            return ignored

        shutil.copytree(BASE_DIR, dest_folder, ignore=_ignore_filter, dirs_exist_ok=True)
        
        # Statistik
        total_files = sum(1 for f in dest_folder.rglob('*') if f.is_file())
        total_size = sum(f.stat().st_size for f in dest_folder.rglob('*') if f.is_file())
        
        print(f"\n✅ Backup erfolgreich!")
        print(f"   Dateien: {total_files}")
        print(f"   Größe:   {total_size / 1024 / 1024:.2f} MB")
        print(f"   Pfad:    {dest_folder}\n")
        
    except Exception as e:
        print(f"\n❌ FEHLER beim Backup: {e}")
        # Cleanup bei Fehler
        if dest_folder.exists():
            shutil.rmtree(dest_folder)

    list_recent_backups()

def list_recent_backups(n=5):
    if not BACKUP_DIR.exists():
        return
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], reverse=True)[:n]
    if backups:
        print(f"📚 Letzte Backups:")
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
        
    print(f"⚠️  ACHTUNG: Restore überschreibt aktuelle Dateien im Bot-Ordner!")
    print(f"   Dies wird alle Dateien in {BASE_DIR} mit dem Backup überschreiben.")
    if input("Wirklich fortfahren? (yes/no): ").lower() != "yes":
        return
        
    print("⏳ Restore läuft...")
    try:
        shutil.copytree(backup_folder, BASE_DIR, dirs_exist_ok=True)
        print("✅ Restore erfolgreich abgeschlossen.")
    except Exception as e:
        print(f"❌ Fehler beim Restore: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) < 3: print("Benutzung: python backup.py restore <timestamp_ordner>"); exit()
        restore_backup(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        list_recent_backups()
    else:
        create_backup()