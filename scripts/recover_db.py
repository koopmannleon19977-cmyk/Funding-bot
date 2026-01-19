#!/usr/bin/env python3
"""
Database Recovery Tool
Inspects and attempts to recover data from corrupted SQLite databases
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def safe_connect(db_path: str) -> sqlite3.Connection | None:
    """Attempt to connect to possibly corrupt database"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"❌ Cannot connect to database: {e}")
        return None


def run_integrity_check(conn: sqlite3.Connection) -> list[str]:
    """Run SQLite integrity check"""
    try:
        cursor = conn.execute("PRAGMA integrity_check")
        results = [row[0] for row in cursor.fetchall()]
        return results
    except Exception as e:
        return [f"ERROR: {e}"]


def get_table_list(conn: sqlite3.Connection) -> list[str]:
    """Get list of tables in database"""
    try:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"❌ Cannot get table list: {e}")
        return []


def recover_table_data(conn: sqlite3.Connection, table: str) -> tuple[list[dict[str, Any]], int]:
    """
    Attempt to recover data from a table.
    Returns (recovered_rows, total_corrupted_rows)
    """
    recovered = []
    corrupted_count = 0

    try:
        cursor = conn.execute(f"SELECT * FROM {table}")
        while True:
            try:
                row = cursor.fetchone()
                if row is None:
                    break
                recovered.append(dict(row))
            except Exception:
                corrupted_count += 1
                # Try to continue reading
                continue

        return recovered, corrupted_count

    except Exception as e:
        print(f"  ❌ Cannot read from {table}: {e}")
        return recovered, corrupted_count


def export_recovered_data(data: dict[str, list[dict[str, Any]]], output_path: str):
    """Export recovered data to JSON"""
    try:
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"✅ Exported recovered data to: {output_path}")
    except Exception as e:
        print(f"❌ Cannot export data: {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python recover_db.py <path_to_corrupt_db>")
        print("\nOr just drag and drop the .corrupt_* file onto this script")
        sys.exit(1)

    db_path = sys.argv[1]

    if not Path(db_path).exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    print("═" * 80)
    print("  DATABASE RECOVERY TOOL")
    print("═" * 80)
    print(f"Database: {db_path}")
    print()

    # Connect
    print("🔌 Connecting to database...")
    conn = safe_connect(db_path)
    if not conn:
        sys.exit(1)

    # Integrity Check
    print("\n🔍 Running integrity check...")
    integrity_results = run_integrity_check(conn)

    if len(integrity_results) == 1 and integrity_results[0] == "ok":
        print("✅ Database is NOT corrupt!")
    else:
        print("⚠️ Database has integrity issues:")
        for issue in integrity_results[:10]:  # Show first 10 issues
            print(f"  - {issue}")
        if len(integrity_results) > 10:
            print(f"  ... and {len(integrity_results) - 10} more issues")

    # Get tables
    print("\n📋 Discovering tables...")
    tables = get_table_list(conn)
    if not tables:
        print("❌ No tables found or cannot read schema")
        conn.close()
        sys.exit(1)

    print(f"Found {len(tables)} tables: {', '.join(tables)}")

    # Recover data from each table
    print("\n🔨 Attempting data recovery...")
    recovered_data = {}
    total_recovered = 0
    total_corrupted = 0

    for table in tables:
        print(f"\n  📊 Table: {table}")
        rows, corrupted = recover_table_data(conn, table)

        if rows:
            recovered_data[table] = rows
            total_recovered += len(rows)
            print(f"    ✅ Recovered {len(rows)} rows")

        if corrupted > 0:
            total_corrupted += corrupted
            print(f"    ⚠️ {corrupted} corrupted rows (unrecoverable)")

        if not rows and corrupted == 0:
            print("    ℹ️ Table is empty")

    conn.close()

    # Summary
    print("\n" + "═" * 80)
    print("  RECOVERY SUMMARY")
    print("═" * 80)
    print(f"✅ Recovered: {total_recovered} rows")
    print(f"❌ Corrupted: {total_corrupted} rows")
    print(f"📊 Tables with data: {len([t for t in recovered_data if recovered_data[t]])}")

    # Export if we recovered anything
    if total_recovered > 0:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"recovered_data_{timestamp}.json"
        print("\n💾 Exporting recovered data...")
        export_recovered_data(recovered_data, output_path)

        # Show important data
        if "trades" in recovered_data and recovered_data["trades"]:
            print("\n📈 RECOVERED TRADES:")
            for trade in recovered_data["trades"][:5]:  # Show first 5
                symbol = trade.get("symbol", "N/A")
                status = trade.get("status", "N/A")
                pnl = trade.get("pnl", 0)
                funding = trade.get("funding_collected", 0)
                print(f"  - {symbol}: status={status}, PnL=${pnl:.2f}, Funding=${funding:.2f}")

            if len(recovered_data["trades"]) > 5:
                print(f"  ... and {len(recovered_data['trades']) - 5} more trades")

    print("\n" + "═" * 80)
    print("Done!")
    print("═" * 80)


if __name__ == "__main__":
    main()
