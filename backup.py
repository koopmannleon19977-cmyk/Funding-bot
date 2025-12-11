#!/usr/bin/env python3
"""
Robustes Backup- und Cleanup-Tool für FundingBot

Usage: 
    python backup.py              # Erstellt ein vollständiges Backup
    python backup.py list         # Zeigt alle Backups
    python backup.py restore <timestamp>  # Stellt Backup wieder her
    python backup.py cleanup      # Räumt alte Logs und Backups auf
    python backup.py cleanup --dry-run   # Zeigt was gelöscht würde
"""

import os
import sys
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Set

# ═══════════════════════════════════════════════════════════════════════════════
# KONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = BASE_DIR / "backups"
LOGS_DIR = BASE_DIR / "logs"

# ═══════════════════════════════════════════════════════════════════════════════
# WICHTIGE DATEIEN - Diese werden IMMER gesichert
# ═══════════════════════════════════════════════════════════════════════════════
IMPORTANT_FILES = [
    "config.py",           # Konfiguration
    "requirements.txt",    # Dependencies
    "START_BOT2.bat",      # Startskript
    "backup.py",           # Dieses Tool
]

IMPORTANT_DIRS = [
    "src",                 # Haupt-Code
    "scripts",             # Hilfsskripte
    "tests",               # Tests
    "docs",                # Dokumentation
    "data",                # Datenbank & State
]

# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP KONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
KEEP_LOGS = 3              # Anzahl Logs die behalten werden
KEEP_BACKUPS = 5           # Anzahl datums-basierte Backups die behalten werden
KEEP_NAMED_BACKUPS = True  # Beschriftete Backups behalten (z.B. "Läuft")

# Ordner, die NICHT gesichert werden sollen
EXCLUDE_DIRS = {
    "backups",       # Sich selbst nicht sichern
    ".venv",         # Virtual Environment
    ".git",          # Git History
    ".idea",         # IDE Settings
    "__pycache__",   # Python Cache
    ".pytest_cache", # Test Cache
    ".mypy_cache",   # Type Check Cache
    "logs",          # Logs separat behandeln
    "node_modules",  # Node modules (falls vorhanden)
}

# Dateimuster, die ignoriert werden
IGNORE_PATTERNS = [
    "*.pyc",
    "*.log", 
    "*.db-journal",
    ".DS_Store",
    "*.tmp",
    "Thumbs.db",
    "*.bak",
]


def create_backup(include_logs: bool = False, description: str = None):
    """Erstellt ein vollständiges Backup des Bot-Ordners.
    
    Args:
        include_logs: Wenn True, werden auch Logs gesichert
        description: Optionale Beschreibung für den Backup-Ordnernamen
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"{timestamp}_{description}" if description else timestamp
    dest_folder = BACKUP_DIR / folder_name
    
    print(f"📦 Erstelle Backup: {folder_name}")
    print(f"   Quelle: {BASE_DIR}")
    print(f"   Ziel:   {dest_folder}")
    print()
    
    # Sicherstellen, dass Backup-Dir existiert
    BACKUP_DIR.mkdir(exist_ok=True)

    try:
        # Ignore-Filter Funktion
        exclude_dirs = EXCLUDE_DIRS.copy()
        if not include_logs:
            exclude_dirs.add("logs")
            
        def _ignore_filter(path, names):
            ignored = set()
            
            # Ordner ausschließen
            for name in names:
                if name in exclude_dirs:
                    ignored.add(name)
            
            # Patterns filtern
            pattern_ignored = shutil.ignore_patterns(*IGNORE_PATTERNS)(path, names)
            ignored.update(pattern_ignored)
            
            return ignored

        # Kopieren
        shutil.copytree(BASE_DIR, dest_folder, ignore=_ignore_filter, dirs_exist_ok=True)
        
        # Backup-Info erstellen
        info_file = dest_folder / "backup_info.txt"
        with open(info_file, "w", encoding="utf-8") as f:
            f.write(f"Backup erstellt: {datetime.now().isoformat()}\n")
            f.write(f"Beschreibung: {description or 'Kein'}\n")
            f.write(f"Logs enthalten: {include_logs}\n")
            f.write(f"\nEnthaltene Dateien:\n")
            for item in IMPORTANT_FILES:
                status = "✅" if (dest_folder / item).exists() else "❌"
                f.write(f"  {status} {item}\n")
            for item in IMPORTANT_DIRS:
                status = "✅" if (dest_folder / item).exists() else "❌"
                f.write(f"  {status} {item}/\n")
        
        # Statistik
        total_files = sum(1 for f in dest_folder.rglob('*') if f.is_file())
        total_size = sum(f.stat().st_size for f in dest_folder.rglob('*') if f.is_file())
        
        print(f"✅ Backup erfolgreich!")
        print(f"   Dateien: {total_files}")
        print(f"   Größe:   {total_size / 1024 / 1024:.2f} MB")
        print(f"   Pfad:    {dest_folder}")
        print()
        
        # Wichtige Dateien prüfen
        print("📋 Wichtige Dateien geprüft:")
        for item in IMPORTANT_FILES:
            status = "✅" if (dest_folder / item).exists() else "❌ FEHLT!"
            print(f"   {status} {item}")
        for item in IMPORTANT_DIRS:
            status = "✅" if (dest_folder / item).exists() else "❌ FEHLT!"
            print(f"   {status} {item}/")
        print()
        
    except Exception as e:
        print(f"\n❌ FEHLER beim Backup: {e}")
        if dest_folder.exists():
            shutil.rmtree(dest_folder)
        return None

    list_recent_backups()
    return dest_folder


def list_recent_backups(n: int = 10):
    """Zeigt die letzten N Backups an."""
    if not BACKUP_DIR.exists():
        print("📁 Noch keine Backups vorhanden.")
        return []
        
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], reverse=True)
    
    if not backups:
        print("📁 Noch keine Backups vorhanden.")
        return []
    
    print(f"📚 Backups ({len(backups)} gesamt):")
    for i, backup in enumerate(backups[:n], 1):
        try:
            size_mb = sum(f.stat().st_size for f in backup.rglob('*') if f.is_file()) / 1024 / 1024
            count = sum(1 for f in backup.rglob('*') if f.is_file())
            
            # Prüfen ob beschriftet (kein reiner Timestamp)
            is_named = not backup.name.replace("_", "").isdigit()
            marker = "📌" if is_named else "  "
            
            print(f"  {marker} {i:2}. {backup.name:<30} ({count:3} Files, {size_mb:6.2f} MB)")
        except Exception:
            print(f"     {i:2}. {backup.name:<30} (Fehler beim Lesen)")
    
    if len(backups) > n:
        print(f"     ... und {len(backups) - n} weitere")
    
    return backups


def restore_backup(timestamp: str):
    """Stellt ein Backup wieder her."""
    # Suche nach passendem Backup
    matching = [d for d in BACKUP_DIR.iterdir() if d.is_dir() and timestamp in d.name]
    
    if not matching:
        print(f"❌ Kein Backup gefunden das '{timestamp}' enthält!")
        print("   Verfügbare Backups:")
        list_recent_backups()
        return False
    
    if len(matching) > 1:
        print(f"⚠️ Mehrere Backups gefunden:")
        for m in matching:
            print(f"   - {m.name}")
        print("   Bitte genaueren Namen angeben.")
        return False
    
    backup_folder = matching[0]
    
    print(f"\n⚠️  ACHTUNG: Restore von {backup_folder.name}")
    print(f"   Dies wird Dateien in {BASE_DIR} überschreiben!")
    print(f"   (Backups-Ordner wird NICHT überschrieben)")
    
    if input("\n   Wirklich fortfahren? (yes/no): ").lower() != "yes":
        print("   Abgebrochen.")
        return False
    
    print("\n⏳ Restore läuft...")
    
    try:
        # Kopiere alles außer backups-Ordner
        for item in backup_folder.iterdir():
            if item.name == "backups":
                continue
            
            dest = BASE_DIR / item.name
            
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
            
            print(f"   ✅ {item.name}")
        
        print("\n✅ Restore erfolgreich abgeschlossen!")
        return True
        
    except Exception as e:
        print(f"\n❌ Fehler beim Restore: {e}")
        return False


def cleanup(dry_run: bool = False):
    """Räumt alte Logs und Backups auf.
    
    Args:
        dry_run: Wenn True, wird nur angezeigt was gelöscht würde
    """
    print("🧹 Cleanup läuft..." + (" (DRY RUN)" if dry_run else ""))
    print()
    
    deleted_count = 0
    freed_space = 0
    
    # ═══════════════════════════════════════════════════════════════════════
    # LOGS AUFRÄUMEN
    # ═══════════════════════════════════════════════════════════════════════
    if LOGS_DIR.exists():
        logs = sorted(LOGS_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        
        if len(logs) > KEEP_LOGS:
            print(f"📄 Logs: {len(logs)} gefunden, behalte {KEEP_LOGS}")
            
            for log in logs[KEEP_LOGS:]:
                size = log.stat().st_size
                print(f"   🗑️ {log.name} ({size / 1024:.1f} KB)")
                
                if not dry_run:
                    log.unlink()
                    
                deleted_count += 1
                freed_space += size
        else:
            print(f"📄 Logs: {len(logs)} gefunden (OK, max {KEEP_LOGS})")
    
    print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # BACKUPS AUFRÄUMEN
    # ═══════════════════════════════════════════════════════════════════════
    if BACKUP_DIR.exists():
        backups = sorted(
            [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True
        )
        
        # Trenne datums-basierte und beschriftete Backups
        dated_backups = []
        named_backups = []
        
        for backup in backups:
            # Prüfe ob der Name ein reiner Timestamp ist (nur Zahlen und _)
            is_timestamp_only = backup.name.replace("_", "").isdigit()
            
            if is_timestamp_only:
                dated_backups.append(backup)
            else:
                named_backups.append(backup)
        
        print(f"📦 Backups: {len(dated_backups)} datums-basiert, {len(named_backups)} beschriftet")
        
        # Beschriftete Backups behalten
        if KEEP_NAMED_BACKUPS and named_backups:
            print(f"   📌 Behalte beschriftete: {', '.join(b.name for b in named_backups)}")
        
        # Ältere datums-basierte Backups löschen
        if len(dated_backups) > KEEP_BACKUPS:
            print(f"   🗑️ Lösche {len(dated_backups) - KEEP_BACKUPS} alte datums-basierte Backups:")
            
            for backup in dated_backups[KEEP_BACKUPS:]:
                size = sum(f.stat().st_size for f in backup.rglob('*') if f.is_file())
                print(f"      - {backup.name} ({size / 1024 / 1024:.1f} MB)")
                
                if not dry_run:
                    shutil.rmtree(backup)
                    
                deleted_count += 1
                freed_space += size
        else:
            print(f"   ✅ Datums-basierte Backups OK (max {KEEP_BACKUPS})")
    
    print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # __PYCACHE__ AUFRÄUMEN
    # ═══════════════════════════════════════════════════════════════════════
    pycache_dirs = list(BASE_DIR.rglob("__pycache__"))
    if pycache_dirs:
        pycache_size = sum(
            sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
            for d in pycache_dirs
        )
        print(f"🐍 __pycache__: {len(pycache_dirs)} Ordner ({pycache_size / 1024 / 1024:.1f} MB)")
        
        if not dry_run:
            for d in pycache_dirs:
                shutil.rmtree(d, ignore_errors=True)
            print("   ✅ Gelöscht")
            deleted_count += len(pycache_dirs)
            freed_space += pycache_size
    
    print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # ZUSAMMENFASSUNG
    # ═══════════════════════════════════════════════════════════════════════
    if dry_run:
        print(f"📊 DRY RUN: Würde {deleted_count} Elemente löschen ({freed_space / 1024 / 1024:.2f} MB)")
        print("   Führe 'python backup.py cleanup' ohne --dry-run aus um wirklich zu löschen.")
    else:
        print(f"✅ Cleanup abgeschlossen: {deleted_count} Elemente gelöscht ({freed_space / 1024 / 1024:.2f} MB)")


def print_usage():
    """Zeigt Hilfe an."""
    print(__doc__)
    print("Beispiele:")
    print("  python backup.py                    # Backup erstellen")
    print("  python backup.py --with-logs        # Backup mit Logs")
    print("  python backup.py --desc 'Vor Fix'   # Backup mit Beschreibung")
    print("  python backup.py list               # Backups anzeigen")
    print("  python backup.py restore 20251211   # Backup wiederherstellen")
    print("  python backup.py cleanup            # Alte Dateien aufräumen")
    print("  python backup.py cleanup --dry-run  # Zeigen was gelöscht würde")


if __name__ == "__main__":
    args = sys.argv[1:]
    
    if not args:
        # Standard: Backup erstellen
        create_backup()
    
    elif args[0] == "list":
        list_recent_backups(20)
    
    elif args[0] == "restore":
        if len(args) < 2:
            print("❌ Benutzung: python backup.py restore <timestamp>")
            print("   Beispiel:  python backup.py restore 20251211_223253")
            list_recent_backups()
        else:
            restore_backup(args[1])
    
    elif args[0] == "cleanup":
        dry_run = "--dry-run" in args
        cleanup(dry_run=dry_run)
    
    elif args[0] in ("-h", "--help", "help"):
        print_usage()
    
    elif args[0] == "--with-logs":
        desc = None
        if "--desc" in args:
            idx = args.index("--desc")
            if idx + 1 < len(args):
                desc = args[idx + 1]
        create_backup(include_logs=True, description=desc)
    
    elif args[0] == "--desc":
        if len(args) < 2:
            print("❌ Beschreibung fehlt!")
        else:
            create_backup(description=args[1])
    
    else:
        print(f"❌ Unbekannter Befehl: {args[0]}")
        print_usage()
