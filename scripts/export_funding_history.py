#!/usr/bin/env python3
"""
Export funding history and totals from the database to CSV.
"""

import os
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = Path("data/funding.db")
HISTORY_EXPORT_FILE = "exports/funding_history_export.csv"
TOTALS_EXPORT_FILE = "exports/funding_totals_export.csv"


def export_funding():
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH}")
        return

    print(f"📂 Connecting to database: {DB_PATH}")
    try:
        conn = sqlite3.connect(DB_PATH)

        # 1. Export Funding History (Granular)
        print("📊 Exporting Funding History...")
        try:
            df_history = pd.read_sql_query("SELECT * FROM funding_history ORDER BY timestamp DESC", conn)
            if df_history.empty:
                print("   ⚠️  funding_history table is EMPTY. No granular history found.")
            else:
                df_history.to_csv(HISTORY_EXPORT_FILE, index=False)
                print(f"   ✅ Saved {len(df_history)} rows to {HISTORY_EXPORT_FILE}")
        except Exception as e:
            print(f"   ❌ Error exporting funding history: {e}")

        # 2. Export Trade Totals (Cumulative)
        print("\n📊 Exporting Trade Funding Totals...")
        try:
            df_trades = pd.read_sql_query(
                "SELECT symbol, status, funding_collected, pnl, created_at, closed_at FROM trades WHERE funding_collected != 0 ORDER BY created_at DESC",
                conn,
            )
            if df_trades.empty:
                print("   ⚠️  No trades found with non-zero funding_collected.")
            else:
                df_trades.to_csv(TOTALS_EXPORT_FILE, index=False)
                print(f"   ✅ Saved {len(df_trades)} rows to {TOTALS_EXPORT_FILE}")
        except Exception as e:
            print(f"   ❌ Error exporting trade totals: {e}")

        conn.close()

    except Exception as e:
        print(f"❌ Database error: {e}")


if __name__ == "__main__":
    # Check if pandas is installed
    try:
        import pandas
    except ImportError:
        print("❌ pandas is not installed. Please install it with: pip install pandas")
        # Fallback to pure python csv export if needed, but for now just exit likely.
        # Actually let's implement fallback since I know pandas failed earlier in the env check!
        print("🔄 Switching to pure Python CSV export...")
        import csv

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # History
        try:
            cursor.execute("SELECT * FROM funding_history ORDER BY timestamp DESC")
            rows = cursor.fetchall()
            if rows:
                col_names = [description[0] for description in cursor.description]
                with open(HISTORY_EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(col_names)
                    writer.writerows(rows)
                print(f"   ✅ Saved {len(rows)} rows to {HISTORY_EXPORT_FILE}")
            else:
                print("   ⚠️  funding_history table is EMPTY.")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        # Totals
        try:
            cursor.execute(
                "SELECT symbol, status, funding_collected, pnl, created_at, closed_at FROM trades WHERE funding_collected != 0 ORDER BY created_at DESC"
            )
            rows = cursor.fetchall()
            if rows:
                col_names = [description[0] for description in cursor.description]
                with open(TOTALS_EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(col_names)
                    writer.writerows(rows)
                print(f"   ✅ Saved {len(rows)} rows to {TOTALS_EXPORT_FILE}")
            else:
                print("   ⚠️  No trades with funding found.")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        conn.close()
        sys.exit(0)

    export_funding()
