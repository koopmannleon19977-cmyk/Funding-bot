#!/usr/bin/env python
"""Quick script to check database PnL values"""
import sqlite3
import sys

DB_PATH = r"c:\Users\koopm\funding-bot\data\trades.db"
OUTPUT_FILE = r"c:\Users\koopm\funding-bot\db_report.txt"

output = []

try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Count all trades
    cur.execute("SELECT COUNT(*) FROM trades")
    total = cur.fetchone()[0]
    output.append(f"Total trades: {total}")
    
    # Count closed trades
    cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
    closed = cur.fetchone()[0]
    output.append(f"Closed trades: {closed}")
    
    # Count zero PnL trades
    cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed' AND (pnl = 0 OR pnl IS NULL)")
    zero_pnl = cur.fetchone()[0]
    output.append(f"Zero PnL closed trades: {zero_pnl}")
    
    # Show sample trades
    output.append("\n--- Sample closed trades ---")
    cur.execute("SELECT symbol, pnl, status, created_at FROM trades WHERE status = 'closed' ORDER BY created_at DESC LIMIT 10")
    for row in cur.fetchall():
        output.append(f"  {row[0]}: PnL=${row[1]}, status={row[2]}")
    
    conn.close()
    
except Exception as e:
    output.append(f"Error: {e}")

# Write to file
with open(OUTPUT_FILE, 'w') as f:
    f.write('\n'.join(output))

# Also print
print('\n'.join(output))
sys.stdout.flush()
