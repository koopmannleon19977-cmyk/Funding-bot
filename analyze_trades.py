#!/usr/bin/env python3
"""
Trade Analysis Script - Analysiert alle Trades aus der Datenbank
Nutzung: python analyze_trades.py
"""

import sqlite3
import sys
from collections import defaultdict

# Fix Windows encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = "data/trades.db"

def analyze():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Alle Trades
        cursor.execute("SELECT * FROM trades ORDER BY id DESC")
        trades = cursor.fetchall()
        
        if not trades:
            print("[X] Keine Trades in der Datenbank gefunden.")
            return
        
        print(f"\n=== TRADE ANALYSE - {len(trades)} Trades total ===\n")
        print("=" * 70)
        
        # Statistiken
        total_pnl = 0
        total_funding = 0
        total_fees = 0
        open_trades = 0
        closed_trades = 0
        winners = 0
        losers = 0
        
        by_symbol = defaultdict(lambda: {"pnl": 0, "count": 0, "funding": 0})
        by_status = defaultdict(int)
        
        for t in trades:
            pnl = t['pnl'] or 0
            funding = t['funding_collected'] or 0
            fees = (t['entry_fee_x10'] or 0) + (t['entry_fee_lighter'] or 0) + \
                   (t['exit_fee_x10'] or 0) + (t['exit_fee_lighter'] or 0)
            
            total_pnl += pnl
            total_funding += funding
            total_fees += fees
            
            status = t['status'] or "unknown"
            by_status[status] += 1
            
            if status == "open":
                open_trades += 1
            else:
                closed_trades += 1
                if pnl > 0.001:
                    winners += 1
                elif pnl < -0.001:
                    losers += 1
            
            symbol = t['symbol']
            by_symbol[symbol]["pnl"] += pnl
            by_symbol[symbol]["funding"] += funding
            by_symbol[symbol]["count"] += 1
        
        # Gesamtuebersicht
        print("\n[GESAMT]")
        print("-" * 40)
        print(f"  Total PnL:        ${total_pnl:+.4f}")
        print(f"  Funding Earned:   ${total_funding:+.4f}")
        print(f"  Total Fees:       ${total_fees:.4f}")
        net = total_pnl + total_funding - total_fees
        print(f"  Net Profit:       ${net:+.4f}")
        
        print(f"\n  Offene Trades:    {open_trades}")
        print(f"  Geschlossen:      {closed_trades}")
        
        if closed_trades > 0:
            print(f"\n  Gewinner:         {winners}")
            print(f"  Verlierer:        {losers}")
            if winners + losers > 0:
                win_rate = winners / (winners + losers) * 100
                print(f"  Win Rate:         {win_rate:.1f}%")
        
        # Status Breakdown
        print("\n[STATUS]")
        print("-" * 40)
        for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
            print(f"  {status}: {count}")
        
        # Top Symbole
        sorted_symbols = sorted(by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True)
        
        if len(sorted_symbols) > 0:
            print("\n[TOP 5 SYMBOLE]")
            print("-" * 40)
            for symbol, data in sorted_symbols[:5]:
                print(f"  {symbol}: PnL=${data['pnl']:+.4f}, Funding=${data['funding']:+.4f} ({data['count']}x)")
            
            print("\n[FLOP 5 SYMBOLE]")
            print("-" * 40)
            for symbol, data in sorted_symbols[-5:]:
                print(f"  {symbol}: PnL=${data['pnl']:+.4f}, Funding=${data['funding']:+.4f} ({data['count']}x)")
        
        # Letzte 10 Trades
        print("\n[LETZTE 10 TRADES]")
        print("-" * 75)
        print(f"{'Symbol':<15} {'Status':<10} {'Size':>8} {'PnL':>10} {'Funding':>10}")
        print("-" * 75)
        
        for t in trades[:10]:
            symbol = (t['symbol'] or "?")[:14]
            status = (t['status'] or "?")[:9]
            size = t['size_usd'] or 0
            pnl = t['pnl'] or 0
            funding = t['funding_collected'] or 0
            
            print(f"{symbol:<15} {status:<10} ${size:>6.0f} ${pnl:>8.4f} ${funding:>8.4f}")
        
        print("\n" + "=" * 70)
        print("[OK] Analyse abgeschlossen!")
        
        if open_trades > 0:
            print(f"\n[INFO] {open_trades} Trades noch offen - PnL wird nach Schliessung berechnet.")
        
        conn.close()
        
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    analyze()
