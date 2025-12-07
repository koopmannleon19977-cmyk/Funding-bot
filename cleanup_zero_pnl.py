#!/usr/bin/env python
"""
Cleanup script to remove $0.00 PnL trades from Kelly history.

These trades were closed through cleanup operations (ghost/zombie cleanup)
and don't represent real trading results.
"""
import sqlite3

DB_PATH = "data/trades.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Count before
    cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
    total_before = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed' AND pnl = 0")
    zero_pnl = cur.fetchone()[0]
    
    print(f"Total closed trades: {total_before}")
    print(f"Zero PnL trades to remove: {zero_pnl}")
    
    if zero_pnl == 0:
        print("✅ No zero PnL trades found - nothing to clean up!")
        conn.close()
        return
    
    # Ask for confirmation
    confirm = input(f"\nDelete {zero_pnl} trades with $0.00 PnL? (yes/no): ")
    
    if confirm.lower() == 'yes':
        cur.execute("DELETE FROM trades WHERE status = 'closed' AND pnl = 0")
        conn.commit()
        
        # Count after
        cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
        total_after = cur.fetchone()[0]
        
        print(f"\n✅ Deleted {zero_pnl} zero PnL trades")
        print(f"Remaining closed trades: {total_after}")
    else:
        print("❌ Cancelled - no changes made")
    
    conn.close()

if __name__ == "__main__":
    main()
