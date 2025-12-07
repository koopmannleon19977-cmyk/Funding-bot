#!/usr/bin/env python3
"""
Cleanup Script: Remove old trades with pnl=0 from database
This fixes the Kelly Win Rate by removing corrupted historical data.
"""

import sqlite3
import os
import glob

def find_database():
    """Find the database file"""
    # Check common locations
    candidates = [
        "data/trades.db",
        "funding.db", 
        "data/funding.db",
        "trades.db"
    ]
    
    for path in candidates:
        if os.path.exists(path):
            return path
    
    # Search for any .db file
    db_files = glob.glob("**/*.db", recursive=True)
    if db_files:
        print(f"Found database files: {db_files}")
        return db_files[0]
    
    return None

def main():
    db_path = find_database()
    
    if not db_path:
        print("❌ No database file found!")
        return
    
    print(f"📂 Opening database: {db_path}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # First, list all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cursor.fetchall()]
    print(f"📋 Tables in database: {tables}")
    
    # Find the right table (could be trade_history, trades, closed_trades, etc.)
    trade_table = None
    pnl_column = None
    
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"   {table}: {columns}")
        
        # Look for pnl column
        for col in columns:
            if 'pnl' in col.lower():
                trade_table = table
                pnl_column = col
                break
        if trade_table:
            break
    
    if not trade_table:
        print("❌ No trade table with pnl column found!")
        conn.close()
        return
    
    print(f"\n✅ Found trade table: {trade_table} (pnl column: {pnl_column})")
    
    # Check current state
    cursor.execute(f"SELECT COUNT(*) FROM {trade_table} WHERE {pnl_column} = 0 OR {pnl_column} IS NULL")
    zero_pnl_count = cursor.fetchone()[0]
    
    cursor.execute(f"SELECT COUNT(*) FROM {trade_table} WHERE {pnl_column} != 0 AND {pnl_column} IS NOT NULL")
    valid_count = cursor.fetchone()[0]
    
    cursor.execute(f"SELECT COUNT(*) FROM {trade_table}")
    total_count = cursor.fetchone()[0]
    
    print(f"\n📊 Current Database State:")
    print(f"   Total trades: {total_count}")
    print(f"   Trades with pnl=0 (corrupted): {zero_pnl_count}")
    print(f"   Trades with valid pnl: {valid_count}")
    
    if zero_pnl_count == 0:
        print("\n✅ No corrupted trades found. Database is clean!")
        conn.close()
        return
    
    # Show some examples
    print(f"\n📋 Sample corrupted entries:")
    cursor.execute(f"SELECT * FROM {trade_table} WHERE {pnl_column} = 0 OR {pnl_column} IS NULL LIMIT 5")
    for row in cursor.fetchall():
        print(f"   {row}")
    
    # Confirm deletion
    print(f"\n⚠️  About to DELETE {zero_pnl_count} trades with pnl=0")
    confirm = input("   Type 'yes' to confirm: ")
    
    if confirm.lower() != 'yes':
        print("❌ Aborted.")
        conn.close()
        return
    
    # Delete corrupted entries
    cursor.execute(f"DELETE FROM {trade_table} WHERE {pnl_column} = 0 OR {pnl_column} IS NULL")
    deleted = cursor.rowcount
    conn.commit()
    
    print(f"\n✅ Deleted {deleted} corrupted trades")
    
    # Verify
    cursor.execute(f"SELECT COUNT(*) FROM {trade_table}")
    remaining = cursor.fetchone()[0]
    print(f"📊 Remaining trades: {remaining}")
    
    conn.close()
    print("\n🎉 Done! Kelly will start fresh with clean data on next bot run.")

if __name__ == "__main__":
    main()

