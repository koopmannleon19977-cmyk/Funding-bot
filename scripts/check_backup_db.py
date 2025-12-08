#!/usr/bin/env python3
"""Check backup database for DOT-USD and other positions"""

import sqlite3
import os

# Check backup DB
backup_db = "backups/20251206_100049/data/trades.db"
current_db = "data/trades.db"

for db_path, label in [(backup_db, "BACKUP"), (current_db, "CURRENT")]:
    print(f"\n{'='*60}")
    print(f"📂 {label} DATABASE: {db_path}")
    print('='*60)
    
    if not os.path.exists(db_path):
        print(f"❌ Database not found!")
        continue
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # List tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cursor.fetchall()]
    print(f"Tables: {tables}")
    
    # Check trades table
    if 'trades' in tables:
        cursor.execute("SELECT COUNT(*) FROM trades")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
        open_count = cursor.fetchone()[0]
        
        print(f"\n📊 Trades: {total} total, {open_count} open")
        
        # Show open trades
        cursor.execute("SELECT symbol, status, side_x10, side_lighter, pnl FROM trades WHERE status='open'")
        open_trades = cursor.fetchall()
        if open_trades:
            print("\n🔓 Open trades:")
            for t in open_trades:
                print(f"   {t[0]}: status={t[1]}, X10={t[2]}, Lighter={t[3]}, pnl={t[4]}")
        
        # Check for DOT-USD specifically
        cursor.execute("SELECT * FROM trades WHERE symbol='DOT-USD'")
        dot_trades = cursor.fetchall()
        if dot_trades:
            print(f"\n🔍 DOT-USD entries: {len(dot_trades)}")
            for t in dot_trades:
                print(f"   {t}")
        else:
            print("\n🔍 DOT-USD: NOT FOUND in database!")
    
    conn.close()

print("\n" + "="*60)
print("ANALYSIS COMPLETE")
print("="*60)

