#!/usr/bin/env python3
"""
Fix X10 funding rates that were incorrectly divided by 100.

BUG: X10 API returns funding rates in DECIMAL format (e.g., -0.000360),
     but the code was dividing by 100, making rates 100× too small.

FIX: Multiply all X10 funding_rate_hourly and funding_apy by 100.

This migration:
1. Creates backup of funding_history table
2. Multiplies X10 rates by 100
3. Validates the fix
4. Provides rollback option

Usage:
    python scripts/migrations/fix_x10_funding_rate_multiply_100.py
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def get_db_path() -> Path:
    """Get the database path from settings or default."""
    # Try common locations
    paths = [
        Path("data/funding_v2.db"),
        Path("data/funding.db"),
        Path("funding_v2.db"),
        Path("funding.db"),
    ]

    for path in paths:
        if path.exists():
            return path

    # Default to data/funding_v2.db
    return Path("data/funding_v2.db")


def backup_database(db_path: Path) -> Path:
    """Create a backup of the database before migration.

    Args:
        db_path: Path to the database file

    Returns:
        Path to the backup file
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"{db_path.stem}_backup_{timestamp}.db"

    import shutil
    shutil.copy2(db_path, backup_path)

    print(f"[BACKUP] Created backup: {backup_path}")
    return backup_path


def analyze_before_migration(db_path: Path) -> dict:
    """Analyze database state before migration.

    Args:
        db_path: Path to the database

    Returns:
        Analysis results
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Count X10 records
    cursor.execute("""
        SELECT
            COUNT(*) as total_records,
            COUNT(DISTINCT symbol) as unique_symbols,
            MIN(timestamp) as earliest,
            MAX(timestamp) as latest
        FROM funding_history
        WHERE exchange = 'X10'
    """)

    stats = cursor.fetchone()
    results = {
        "total_records": stats[0],
        "unique_symbols": stats[1],
        "earliest_timestamp": stats[2],
        "latest_timestamp": stats[3],
    }

    # Sample a few records to show current state
    cursor.execute("""
        SELECT symbol, timestamp, rate_hourly, rate_apy
        FROM funding_history
        WHERE exchange = 'X10'
        ORDER BY timestamp DESC
        LIMIT 5
    """)

    samples = cursor.fetchall()

    conn.close()

    print("\n[ANALYSIS] Database State Before Migration:")
    print(f"  Total X10 Records: {results['total_records']}")
    print(f"  Unique Symbols: {results['unique_symbols']}")
    print(f"  Time Range: {results['earliest_timestamp']} to {results['latest_timestamp']}")

    print("\n  Sample Records (BEFORE):")
    print("  " + "-" * 100)
    for symbol, ts, rate_hourly, rate_apy in samples:
        print(f"  {symbol:<10} {ts} | hourly={rate_hourly:>10.6f} | apy={rate_apy:>10.2f}%")

    results["samples"] = samples
    return results


def migrate_x10_rates(db_path: Path, dry_run: bool = False) -> bool:
    """Multiply all X10 funding rates by 100 to fix the bug.

    Args:
        db_path: Path to the database
        dry_run: If True, show what would be done without doing it

    Returns:
        True if successful
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    if dry_run:
        print("\n[DRY RUN] Would execute the following SQL:")
        print("  UPDATE funding_history")
        print("  SET rate_hourly = rate_hourly * 100,")
        print("      rate_apy = rate_apy * 100")
        print("  WHERE exchange = 'X10'")
        conn.close()
        return True

    try:
        # Begin transaction
        conn.execute("BEGIN TRANSACTION")

        # Update X10 records
        cursor.execute("""
            UPDATE funding_history
            SET rate_hourly = rate_hourly * 100,
                rate_apy = rate_apy * 100
            WHERE exchange = 'X10'
        """)

        updated_count = cursor.rowcount

        # Validate the update
        cursor.execute("""
            SELECT
                COUNT(*) as count,
                MIN(rate_hourly) as min_hourly,
                MAX(rate_hourly) as max_hourly,
                MIN(rate_apy) as min_apy,
                MAX(rate_apy) as max_apy
            FROM funding_history
            WHERE exchange = 'X10'
        """)

        validation = cursor.fetchone()

        # Check for unreasonable values (should be between -100% and +100% hourly)
        cursor.execute("""
            SELECT COUNT(*) as outliers
            FROM funding_history
            WHERE exchange = 'X10'
              AND ABS(rate_hourly) > 1.0
        """)

        outliers = cursor.fetchone()[0]

        # Commit transaction
        conn.commit()

        print(f"\n[MIGRATION] Success!")
        print(f"  Records Updated: {updated_count}")
        print(f"  Validation:")
        print(f"    Hourly Rate Range: {validation[1]:.6f} to {validation[2]:.6f}")
        print(f"    APY Range: {validation[3]:.2f}% to {validation[4]:.2f}%")
        print(f"    Outliers (>100% hourly): {outliers}")

        if outliers > 0:
            print(f"\n  [WARNING] Found {outliers} records with |hourly_rate| > 100%")
            print("  This might indicate data quality issues - please review!")

        conn.close()
        return True

    except Exception as e:
        conn.rollback()
        print(f"\n[ERROR] Migration failed: {e}")
        print("  Transaction rolled back - no changes made")
        conn.close()
        return False


def verify_after_migration(db_path: Path) -> dict:
    """Verify database state after migration.

    Args:
        db_path: Path to the database

    Returns:
        Verification results
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Sample same records as before
    cursor.execute("""
        SELECT symbol, timestamp, rate_hourly, rate_apy
        FROM funding_history
        WHERE exchange = 'X10'
        ORDER BY timestamp DESC
        LIMIT 5
    """)

    samples = cursor.fetchall()

    # Check specific problematic symbols (BERA should show much higher APY now)
    cursor.execute("""
        SELECT
            symbol,
            AVG(rate_hourly) as avg_hourly,
            AVG(rate_apy) as avg_apy
        FROM funding_history
        WHERE exchange = 'X10' AND symbol = 'BERA'
        GROUP BY symbol
    """)

    bera_stats = cursor.fetchone()

    conn.close()

    print("\n[VERIFICATION] Database State After Migration:")
    print("  Sample Records (AFTER):")
    print("  " + "-" * 100)
    for symbol, ts, rate_hourly, rate_apy in samples:
        print(f"  {symbol:<10} {ts} | hourly={rate_hourly:>10.6f} | apy={rate_apy:>10.2f}%")

    if bera_stats:
        print(f"\n  BERA Statistics (AFTER):")
        print(f"    Avg Hourly Rate: {bera_stats[1]:.6f}")
        print(f"    Avg APY: {bera_stats[2]:.2f}%")

    return {"samples": samples, "bera_stats": bera_stats}


def main():
    """Main migration entry point."""
    print("=" * 100)
    print("X10 FUNDING RATE BUG FIX MIGRATION")
    print("=" * 100)
    print("\nThis migration fixes X10 funding rates that were incorrectly divided by 100.")
    print("All X10 rates will be multiplied by 100 to correct the bug.\n")

    # Get database path
    db_path = get_db_path()

    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}")
        print("  Please ensure the database exists before running this migration.")
        return 1

    print(f"[INFO] Using database: {db_path}\n")

    # Step 1: Analyze before migration
    print("Step 1: Analyzing database state...")
    before_stats = analyze_before_migration(db_path)

    if before_stats["total_records"] == 0:
        print("\n[INFO] No X10 records found - nothing to migrate.")
        print("  This is normal if the bot hasn't fetched any X10 data yet.")
        return 0

    # Step 2: Create backup
    print("\nStep 2: Creating backup...")
    backup_path = backup_database(db_path)

    # Step 3: Confirm with user
    print(f"\nStep 3: Ready to migrate {before_stats['total_records']} X10 records.")
    print(f"  Backup: {backup_path}")

    response = input("\n  Proceed with migration? (yes/no): ").strip().lower()

    if response not in ("yes", "y"):
        print("\n[INFO] Migration cancelled by user.")
        return 0

    # Step 4: Run migration
    print("\nStep 4: Running migration...")
    if not migrate_x10_rates(db_path, dry_run=False):
        print("\n[ERROR] Migration failed - see above for details.")
        print("  Your database is still in the original state.")
        print(f"  Backup: {backup_path}")
        return 1

    # Step 5: Verify after migration
    print("\nStep 5: Verifying migration...")
    after_stats = verify_after_migration(db_path)

    # Compare before/after
    if before_stats["samples"] and after_stats["samples"]:
        print("\n[COMPARISON] Before vs After (same records):")
        print("  " + "-" * 100)
        for i, (before, after) in enumerate(zip(before_stats["samples"], after_stats["samples"])):
            symbol, ts, old_hourly, old_rate_apy = before
            _, _, new_hourly, new_rate_apy = after
            multiplier = new_hourly / old_hourly if old_hourly != 0 else 0
            print(f"  {symbol:<10} {ts}")
            print(f"    BEFORE: hourly={old_hourly:>10.6f} | apy={old_rate_apy:>10.2f}%")
            print(f"    AFTER:  hourly={new_hourly:>10.6f} | apy={new_rate_apy:>10.2f}% (×{multiplier:.0f})")

    print("\n" + "=" * 100)
    print("[SUCCESS] Migration completed successfully!")
    print("=" * 100)
    print("\nNext Steps:")
    print("1. Restart the bot to use the corrected funding rate calculation")
    print("2. The bot will now fetch NEW X10 data correctly (no /100 division)")
    print("3. Run partial backfill to update recent data (last 7 days)")
    print(f"\nBackup saved at: {backup_path}")
    print("You can rollback by restoring the backup if needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
