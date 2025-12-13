#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze PnL tracking: Compare CSV export with bot logs and DB.

Usage:
    python analyze_pnl.py [--csv <path>] [--log <path>] [--db <path>]

Options:
    --csv <path>    Path to Lighter trade export CSV (default: auto-detect in Downloads)
    --log <path>    Path to bot log file (default: most recent in logs/)
    --db <path>     Path to trades.db (default: data/trades.db)
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.pnl_utils import sum_closed_pnl_from_csv, parse_csv_file, reconcile_csv_vs_db


def find_latest_csv(directory: str = None) -> Optional[str]:
    """Find the most recent Lighter trade export CSV."""
    if directory is None:
        # Try common locations
        candidates = [
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Desktop"),
            ".",
        ]
    else:
        candidates = [directory]
    
    for dir_path in candidates:
        if not os.path.isdir(dir_path):
            continue
        files = list(Path(dir_path).glob("lighter-trade-export-*.csv"))
        if files:
            # Sort by modification time, newest first
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            return str(files[0])
    
    return None


def find_latest_log(logs_dir: str = "logs") -> Optional[str]:
    """Find the most recent bot log file."""
    if not os.path.isdir(logs_dir):
        return None
    
    log_files = list(Path(logs_dir).glob("funding_bot_*_FULL.log"))
    if not log_files:
        return None
    
    # Sort by modification time, newest first
    log_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return str(log_files[0])


def parse_log_pnl(log_path: str) -> Dict[str, Dict[str, Any]]:
    """Parse PnL recordings from bot log file."""
    log_pnl = {}
    
    if not os.path.isfile(log_path):
        return log_pnl
    
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            # Look for shutdown PnL recordings
            match = re.search(
                r'💰 Shutdown PnL for (\w+-USD): PnL=\$([-\d.]+), Funding=\$([-\d.]+)',
                line
            )
            if match:
                symbol = match.group(1)
                pnl = float(match.group(2))
                funding = float(match.group(3))
                log_pnl[symbol] = {
                    'pnl': pnl,
                    'funding': funding,
                    'source': 'shutdown'
                }
            
            # Also look for realized PnL in manage_open_trades
            match = re.search(
                r'📊 (\w+-USD) Realized PnL: .+total=\$([-\d.]+)',
                line
            )
            if match:
                symbol = match.group(1)
                pnl = float(match.group(2))
                if symbol not in log_pnl:
                    log_pnl[symbol] = {
                        'pnl': pnl,
                        'funding': 0.0,
                        'source': 'trade_close'
                    }
            
            # Also look for pre-close PnL
            match = re.search(
                r'📊 (\w+-USD) Pre-Close PnL: uPnL=\$([-\d.]+), rPnL=\$([-\d.]+), funding=\$([-\d.]+)',
                line
            )
            if match:
                symbol = match.group(1)
                upnl = float(match.group(2))
                rpnl = float(match.group(3))
                funding = float(match.group(4))
                if symbol not in log_pnl:
                    log_pnl[symbol] = {
                        'pnl': upnl + rpnl,
                        'funding': funding,
                        'upnl': upnl,
                        'rpnl': rpnl,
                        'source': 'pre_close'
                    }
    
    return log_pnl


def get_db_pnl(db_path: str) -> Dict[str, float]:
    """Get PnL values from database per symbol."""
    db_pnl = {}
    
    if not os.path.isfile(db_path):
        return db_pnl
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT symbol, pnl FROM trades WHERE status = 'closed' AND pnl != 0"
        )
        for row in cursor.fetchall():
            symbol, pnl = row
            if symbol not in db_pnl:
                db_pnl[symbol] = 0.0
            db_pnl[symbol] += float(pnl or 0)
        conn.close()
    except Exception as e:
        print(f"Error reading DB: {e}")
    
    return db_pnl


def print_comparison(
    csv_pnl: Dict[str, Dict[str, Any]],
    log_pnl: Dict[str, Dict[str, Any]],
    db_pnl: Dict[str, float],
):
    """Print detailed comparison of PnL data from all sources."""
    print("\n" + "=" * 80)
    print("📊 PNL COMPARISON RESULTS")
    print("=" * 80)
    
    all_symbols = set(csv_pnl.keys()) | set(log_pnl.keys()) | set(db_pnl.keys())
    
    matches = []
    mismatches = []
    missing_in_log = []
    missing_in_csv = []
    missing_in_db = []
    
    for symbol in sorted(all_symbols):
        csv_val = csv_pnl.get(symbol, {}).get('total_pnl', 0.0)
        log_val = log_pnl.get(symbol, {}).get('pnl', 0.0)
        db_val = db_pnl.get(symbol, 0.0)
        
        in_csv = symbol in csv_pnl
        in_log = symbol in log_pnl
        in_db = symbol in db_pnl
        
        print(f"\n{symbol}:")
        
        if in_csv:
            trade_count = csv_pnl[symbol].get('trade_count', 0)
            print(f"  CSV Total PnL:     ${csv_val:+.6f} ({trade_count} close trades)")
        else:
            print(f"  CSV Total PnL:     NOT FOUND")
        
        if in_log:
            source = log_pnl[symbol].get('source', 'unknown')
            print(f"  Bot Log PnL:       ${log_val:+.6f} (source: {source})")
        else:
            print(f"  Bot Log PnL:       NOT FOUND")
        
        if in_db:
            print(f"  Database PnL:      ${db_val:+.6f}")
        else:
            print(f"  Database PnL:      NOT FOUND")
        
        # Determine status
        if in_csv and in_log:
            diff = abs(csv_val - log_val)
            diff_pct = (diff / abs(csv_val) * 100) if csv_val != 0 else 0
            
            if diff < 0.01:
                status = "✅ MATCH"
                matches.append(symbol)
            else:
                status = f"❌ MISMATCH (diff: ${diff:.6f}, {diff_pct:.2f}%)"
                mismatches.append({
                    'symbol': symbol,
                    'csv_pnl': csv_val,
                    'log_pnl': log_val,
                    'db_pnl': db_val,
                    'difference': csv_val - log_val,
                })
            print(f"  Status:            {status}")
        elif in_csv and not in_log:
            print(f"  Status:            ⚠️ MISSING IN LOG")
            missing_in_log.append(symbol)
        elif in_log and not in_csv:
            print(f"  Status:            ⚠️ MISSING IN CSV")
            missing_in_csv.append(symbol)
        
        if in_csv or in_log:
            if not in_db:
                print(f"  DB Status:         ⚠️ MISSING IN DB")
                missing_in_db.append(symbol)
            elif in_csv:
                db_diff = abs(csv_val - db_val)
                if db_diff < 0.01:
                    print(f"  DB Status:         ✅ MATCHES CSV")
                else:
                    print(f"  DB Status:         ❌ DIFFERS FROM CSV (diff: ${db_diff:.6f})")
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"✅ Matches (CSV vs Log):     {len(matches)}")
    print(f"❌ Mismatches (CSV vs Log):  {len(mismatches)}")
    print(f"⚠️  Missing in log:          {len(missing_in_log)}")
    print(f"⚠️  Missing in CSV:          {len(missing_in_csv)}")
    print(f"⚠️  Missing in DB:           {len(missing_in_db)}")
    
    # Detailed mismatches
    if mismatches:
        print("\n" + "=" * 80)
        print("❌ DETAILED MISMATCHES (CSV vs Log)")
        print("=" * 80)
        for m in mismatches:
            print(f"  {m['symbol']}:")
            print(f"    CSV:  ${m['csv_pnl']:+.6f}")
            print(f"    Log:  ${m['log_pnl']:+.6f}")
            print(f"    DB:   ${m['db_pnl']:+.6f}")
            print(f"    Diff: ${m['difference']:+.6f}")
    
    # CSV trade details for mismatches
    if mismatches:
        print("\n" + "=" * 80)
        print("CSV TRADE DETAILS FOR MISMATCHES")
        print("=" * 80)
        for m in mismatches:
            symbol = m['symbol']
            if symbol in csv_pnl:
                print(f"\n{symbol} - CSV Trades:")
                for trade in csv_pnl[symbol].get('trades', []):
                    print(
                        f"  {trade['date']}: Size={trade['size']:.6f}, "
                        f"Price={trade['price']:.6f}, PnL=${trade['pnl']:+.6f}"
                    )
    
    return {
        'matches': matches,
        'mismatches': mismatches,
        'missing_in_log': missing_in_log,
        'missing_in_csv': missing_in_csv,
        'missing_in_db': missing_in_db,
    }


def main():
    parser = argparse.ArgumentParser(
        description='Analyze PnL tracking: Compare CSV export with bot logs and DB.'
    )
    parser.add_argument(
        '--csv',
        type=str,
        help='Path to Lighter trade export CSV (default: auto-detect in Downloads)'
    )
    parser.add_argument(
        '--log',
        type=str,
        help='Path to bot log file (default: most recent in logs/)'
    )
    parser.add_argument(
        '--db',
        type=str,
        default='data/trades.db',
        help='Path to trades.db (default: data/trades.db)'
    )
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("PNL TRACKING ANALYSIS")
    print("=" * 80)
    
    # Find or use CSV file
    csv_path = args.csv or find_latest_csv()
    if not csv_path:
        print("⚠️  No CSV file found. Use --csv to specify path.")
        csv_rows = []
    else:
        print(f"📄 CSV file: {csv_path}")
        csv_rows = parse_csv_file(csv_path)
        print(f"   Loaded {len(csv_rows)} rows")
    
    # Find or use log file
    log_path = args.log or find_latest_log()
    if not log_path:
        print("⚠️  No log file found. Use --log to specify path.")
        log_pnl = {}
    else:
        print(f"📄 Log file: {log_path}")
        log_pnl = parse_log_pnl(log_path)
        print(f"   Found {len(log_pnl)} symbols with PnL recordings")
    
    # DB path
    db_path = args.db
    print(f"📄 Database: {db_path}")
    db_pnl = get_db_pnl(db_path)
    print(f"   Found {len(db_pnl)} symbols with closed PnL")
    
    # Parse CSV using pnl_utils
    csv_pnl = sum_closed_pnl_from_csv(csv_rows)
    
    # Run comparison
    results = print_comparison(csv_pnl, log_pnl, db_pnl)
    
    # Exit code based on mismatches
    sys.exit(1 if results['mismatches'] else 0)


if __name__ == "__main__":
    main()
