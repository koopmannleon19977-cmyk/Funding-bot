#!/usr/bin/env python
"""
Check database PnL values and identify issues.

Features:
- Lists total trade counts (open, closed)
- Shows trades with zero PnL or zero funding
- Provides summary statistics
- Outputs to both console and file
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default paths
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
DEFAULT_OUTPUT_FILE = Path(__file__).parent.parent / "db_report.txt"


def check_database(db_path: str, output_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Check database for PnL issues and generate report.
    
    Returns:
        Dict with statistics and issue lists
    """
    output = []
    results = {
        "total_trades": 0,
        "open_trades": 0,
        "closed_trades": 0,
        "zero_pnl_trades": [],
        "zero_funding_trades": [],
        "both_zero_trades": [],
        "total_pnl": 0.0,
        "total_funding": 0.0,
    }
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Count all trades
        cur.execute("SELECT COUNT(*) FROM trades")
        results["total_trades"] = cur.fetchone()[0]
        output.append(f"Total trades: {results['total_trades']}")
        
        # Count open trades
        cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'open'")
        results["open_trades"] = cur.fetchone()[0]
        output.append(f"Open trades: {results['open_trades']}")
        
        # Count closed trades
        cur.execute("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
        results["closed_trades"] = cur.fetchone()[0]
        output.append(f"Closed trades: {results['closed_trades']}")
        
        # Get total PnL and funding
        cur.execute("""
            SELECT 
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(SUM(funding_collected), 0) as total_funding
            FROM trades 
            WHERE status = 'closed'
        """)
        row = cur.fetchone()
        results["total_pnl"] = float(row["total_pnl"] or 0)
        results["total_funding"] = float(row["total_funding"] or 0)
        output.append(f"\nTotal Realized PnL: ${results['total_pnl']:.4f}")
        output.append(f"Total Funding Collected: ${results['total_funding']:.4f}")
        
        # ═══════════════════════════════════════════════════════════════
        # Find closed trades with PnL = 0
        # ═══════════════════════════════════════════════════════════════
        output.append("\n" + "=" * 60)
        output.append("CLOSED TRADES WITH ZERO PNL")
        output.append("=" * 60)
        
        cur.execute("""
            SELECT symbol, pnl, funding_collected, status, created_at, closed_at
            FROM trades 
            WHERE status = 'closed' AND (pnl = 0 OR pnl IS NULL)
            ORDER BY closed_at DESC
        """)
        zero_pnl_rows = cur.fetchall()
        results["zero_pnl_trades"] = [dict(r) for r in zero_pnl_rows]
        
        output.append(f"Count: {len(zero_pnl_rows)}")
        if zero_pnl_rows:
            output.append("")
            for row in zero_pnl_rows[:20]:  # Limit to first 20
                output.append(
                    f"  {row['symbol']}: PnL=${row['pnl'] or 0:.4f}, "
                    f"Funding=${row['funding_collected'] or 0:.4f}"
                )
            if len(zero_pnl_rows) > 20:
                output.append(f"  ... and {len(zero_pnl_rows) - 20} more")
        
        # ═══════════════════════════════════════════════════════════════
        # Find closed trades with Funding = 0
        # ═══════════════════════════════════════════════════════════════
        output.append("\n" + "=" * 60)
        output.append("CLOSED TRADES WITH ZERO FUNDING")
        output.append("=" * 60)
        
        cur.execute("""
            SELECT symbol, pnl, funding_collected, status, created_at, closed_at
            FROM trades 
            WHERE status = 'closed' AND (funding_collected = 0 OR funding_collected IS NULL)
            ORDER BY closed_at DESC
        """)
        zero_funding_rows = cur.fetchall()
        results["zero_funding_trades"] = [dict(r) for r in zero_funding_rows]
        
        output.append(f"Count: {len(zero_funding_rows)}")
        if zero_funding_rows:
            output.append("")
            for row in zero_funding_rows[:20]:  # Limit to first 20
                output.append(
                    f"  {row['symbol']}: PnL=${row['pnl'] or 0:.4f}, "
                    f"Funding=${row['funding_collected'] or 0:.4f}"
                )
            if len(zero_funding_rows) > 20:
                output.append(f"  ... and {len(zero_funding_rows) - 20} more")
        
        # ═══════════════════════════════════════════════════════════════
        # Find closed trades with BOTH PnL = 0 AND Funding = 0
        # ═══════════════════════════════════════════════════════════════
        output.append("\n" + "=" * 60)
        output.append("⚠️ CLOSED TRADES WITH BOTH PNL AND FUNDING = 0")
        output.append("=" * 60)
        
        cur.execute("""
            SELECT symbol, pnl, funding_collected, status, created_at, closed_at
            FROM trades 
            WHERE status = 'closed' 
              AND (pnl = 0 OR pnl IS NULL)
              AND (funding_collected = 0 OR funding_collected IS NULL)
            ORDER BY closed_at DESC
        """)
        both_zero_rows = cur.fetchall()
        results["both_zero_trades"] = [dict(r) for r in both_zero_rows]
        
        output.append(f"Count: {len(both_zero_rows)}")
        if both_zero_rows:
            output.append("")
            output.append("These trades may have PnL recording issues!")
            output.append("")
            for row in both_zero_rows[:30]:  # Show more for these critical cases
                output.append(
                    f"  {row['symbol']}: closed_at={row['closed_at']}"
                )
            if len(both_zero_rows) > 30:
                output.append(f"  ... and {len(both_zero_rows) - 30} more")
        
        # ═══════════════════════════════════════════════════════════════
        # Show recent closed trades (sample)
        # ═══════════════════════════════════════════════════════════════
        output.append("\n" + "=" * 60)
        output.append("RECENT CLOSED TRADES (Sample)")
        output.append("=" * 60)
        
        cur.execute("""
            SELECT symbol, pnl, funding_collected, status, created_at, closed_at
            FROM trades 
            WHERE status = 'closed'
            ORDER BY closed_at DESC
            LIMIT 10
        """)
        recent_rows = cur.fetchall()
        
        for row in recent_rows:
            output.append(
                f"  {row['symbol']}: PnL=${row['pnl'] or 0:.4f}, "
                f"Funding=${row['funding_collected'] or 0:.4f}"
            )
        
        # ═══════════════════════════════════════════════════════════════
        # Summary
        # ═══════════════════════════════════════════════════════════════
        output.append("\n" + "=" * 60)
        output.append("SUMMARY")
        output.append("=" * 60)
        output.append(f"Total Trades: {results['total_trades']}")
        output.append(f"  Open: {results['open_trades']}")
        output.append(f"  Closed: {results['closed_trades']}")
        output.append(f"")
        output.append(f"Closed trades with zero PnL: {len(results['zero_pnl_trades'])}")
        output.append(f"Closed trades with zero funding: {len(results['zero_funding_trades'])}")
        output.append(f"Closed trades with BOTH zero: {len(results['both_zero_trades'])} ⚠️")
        output.append(f"")
        output.append(f"Total Realized PnL: ${results['total_pnl']:.4f}")
        output.append(f"Total Funding: ${results['total_funding']:.4f}")
        output.append(f"Combined: ${results['total_pnl'] + results['total_funding']:.4f}")
        
        conn.close()
        
    except Exception as e:
        output.append(f"Error: {e}")
        results["error"] = str(e)
    
    # Write to file if specified
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(output))
        print(f"Report written to: {output_file}")
    
    # Print to console
    print('\n'.join(output))
    sys.stdout.flush()
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description='Check database PnL values and identify issues.'
    )
    parser.add_argument(
        '--db',
        type=str,
        default=str(DEFAULT_DB_PATH),
        help=f'Path to trades.db (default: {DEFAULT_DB_PATH})'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=str(DEFAULT_OUTPUT_FILE),
        help=f'Output file path (default: {DEFAULT_OUTPUT_FILE})'
    )
    parser.add_argument(
        '--no-file',
        action='store_true',
        help='Do not write output to file'
    )
    
    args = parser.parse_args()
    
    output_file = None if args.no_file else args.output
    results = check_database(args.db, output_file)
    
    # Exit code based on issues found
    if results.get("both_zero_trades"):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
