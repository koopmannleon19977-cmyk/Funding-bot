#!/usr/bin/env python3
"""
PnL Analysis Script - Analyzes trades.db for PnL tracking issues
"""
import sqlite3
import sys
from datetime import datetime

# Fix Windows encoding
sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = "data/trades.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("=" * 60)
    print("[ANALYSIS] TRADES.DB PNL ANALYSIS")
    print("=" * 60)
    
    # 1. Recent trades
    print("\n[TRADES] LETZTE 10 TRADES:")
    print("-" * 60)
    cursor.execute("""
        SELECT symbol, status, size_usd, pnl, funding_collected, 
               created_at, closed_at
        FROM trades 
        ORDER BY created_at DESC 
        LIMIT 10
    """)
    
    for row in cursor.fetchall():
        created = datetime.fromtimestamp(row['created_at']/1000).strftime('%Y-%m-%d %H:%M')
        closed = ""
        if row['closed_at']:
            closed = datetime.fromtimestamp(row['closed_at']/1000).strftime('%Y-%m-%d %H:%M')
        
        print(f"  {row['symbol']:12} | {row['status']:6} | ${row['size_usd']:8.2f} | "
              f"PnL: ${row['pnl']:8.4f} | Funding: ${row['funding_collected']:8.4f} | "
              f"{created} → {closed}")
    
    # 2. Summary
    print("\n[SUMMARY] ZUSAMMENFASSUNG:")
    print("-" * 60)
    
    cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(SUM(funding_collected), 0) as total_funding,
            COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as profitable_trades,
            COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) as losing_trades,
            COALESCE(SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END), 0) as zero_pnl_trades
        FROM trades
    """)
    
    row = cursor.fetchone()
    print(f"  Total Trades:      {row['total_trades']}")
    print(f"  Closed Trades:     {row['closed_trades']}")
    print(f"  Open Trades:       {row['open_trades']}")
    print(f"  Profitable:        {row['profitable_trades']}")
    print(f"  Losing:            {row['losing_trades']}")
    print(f"  Zero PnL:          {row['zero_pnl_trades']} [WARNING]" if row['zero_pnl_trades'] > 0 else f"  Zero PnL:          {row['zero_pnl_trades']}")
    print(f"  Total PnL:         ${row['total_pnl']:.4f}")
    print(f"  Total Funding:     ${row['total_funding']:.4f}")
    print(f"  Net Result:        ${row['total_pnl'] + row['total_funding']:.4f}")
    
    # 3. Check for PnL issues
    print("\n[ISSUES] POTENZIELLE PROBLEME:")
    print("-" * 60)
    
    # Trades mit 0 PnL
    cursor.execute("""
        SELECT symbol, size_usd, created_at, closed_at
        FROM trades 
        WHERE status = 'closed' AND pnl = 0 AND size_usd > 10
        ORDER BY closed_at DESC
        LIMIT 5
    """)
    zero_pnl = cursor.fetchall()
    
    if zero_pnl:
        print(f"  [X] {len(zero_pnl)} geschlossene Trades mit PnL = $0.00:")
        for row in zero_pnl:
            print(f"     - {row['symbol']} (${row['size_usd']:.2f})")
    else:
        print("  [OK] Keine Trades mit PnL = $0.00")
    
    # Trades mit 0 Funding
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM trades 
        WHERE status = 'closed' AND funding_collected = 0
    """)
    zero_funding = cursor.fetchone()['count']
    
    if zero_funding > 0:
        print(f"  [!] {zero_funding} Trades ohne Funding-Tracking")
    else:
        print("  [OK] Alle Trades haben Funding-Daten")
    
    # 4. PnL Snapshots
    print("\n[SNAPSHOTS] PNL SNAPSHOTS:")
    print("-" * 60)
    cursor.execute("SELECT COUNT(*) as count FROM pnl_snapshots")
    snapshot_count = cursor.fetchone()['count']
    
    if snapshot_count == 0:
        print("  [X] KEINE Snapshots vorhanden - PnL-Historie wird nicht gespeichert!")
    else:
        print(f"  [OK] {snapshot_count} Snapshots vorhanden")
        cursor.execute("""
            SELECT * FROM pnl_snapshots ORDER BY timestamp DESC LIMIT 3
        """)
        for row in cursor.fetchall():
            ts = datetime.fromtimestamp(row['timestamp']/1000).strftime('%Y-%m-%d %H:%M')
            print(f"     {ts}: Total=${row['total_pnl']:.4f}, "
                  f"uPnL=${row['unrealized_pnl']:.4f}, "
                  f"rPnL=${row['realized_pnl']:.4f}, "
                  f"Funding=${row['funding_pnl']:.4f}")
    
    # 5. Funding History
    print("\n[FUNDING] FUNDING HISTORY:")
    print("-" * 60)
    cursor.execute("SELECT COUNT(*) as count FROM funding_history")
    funding_count = cursor.fetchone()['count']
    
    if funding_count == 0:
        print("  [X] KEINE Funding-Records - Funding-Rates werden nicht gespeichert!")
    else:
        print(f"  [OK] {funding_count} Funding-Records vorhanden")
    
    conn.close()
    
    print("\n" + "=" * 60)
    print("Analyse abgeschlossen")
    print("=" * 60)

if __name__ == "__main__":
    main()
