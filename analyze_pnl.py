#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze PnL tracking: Compare CSV export with bot logs
"""
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Parse CSV
csv_file = r"c:\Users\koopm\Downloads\lighter-trade-export-2025-12-13T00_45_07.482Z-UTC.csv"
log_file = r"logs\funding_bot_LEON_20251213_014305_FULL.log"

print("=" * 80)
print("PNL TRACKING ANALYSIS")
print("=" * 80)

# Normalize symbol names (CSV uses "TAO", bot uses "TAO-USD")
def normalize_symbol(symbol):
    """Normalize symbol to match bot format"""
    if symbol and not symbol.endswith('-USD'):
        return f"{symbol}-USD"
    return symbol

# Parse CSV trades
csv_trades = defaultdict(list)
with open(csv_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        market = row['Market']
        side = row['Side']
        date_str = row['Date']
        closed_pnl = row['Closed PnL']
        
        if side == 'Close Short' and closed_pnl and closed_pnl != '-':
            try:
                pnl_value = float(closed_pnl)
                symbol = normalize_symbol(market)
                csv_trades[symbol].append({
                    'date': date_str,
                    'pnl': pnl_value,
                    'size': float(row['Size']),
                    'price': float(row['Price']),
                    'trade_value': float(row['Trade Value'])
                })
            except (ValueError, TypeError):
                pass

# Sum PnL per symbol from CSV
csv_pnl_totals = {}
for symbol, trades in csv_trades.items():
    total_pnl = sum(t['pnl'] for t in trades)
    csv_pnl_totals[symbol] = {
        'total_pnl': total_pnl,
        'trade_count': len(trades),
        'trades': trades
    }

# Parse log file for PnL recordings
log_pnl = {}
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        # Look for shutdown PnL recordings
        match = re.search(r'💰 Shutdown PnL for (\w+-USD): PnL=\$([-\d.]+), Funding=\$([-\d.]+)', line)
        if match:
            symbol = match.group(1)
            pnl = float(match.group(2))
            funding = float(match.group(3))
            log_pnl[symbol] = {
                'pnl': pnl,
                'funding': funding
            }
        
        # Also look for pre-close PnL
        match = re.search(r'📊 (\w+-USD) Pre-Close PnL: uPnL=\$([-\d.]+), rPnL=\$([-\d.]+), funding=\$([-\d.]+)', line)
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
                    'rpnl': rpnl
                }

# Compare
print("\n📊 COMPARISON RESULTS")
print("=" * 80)

all_symbols = set(csv_pnl_totals.keys()) | set(log_pnl.keys())

mismatches = []
matches = []

for symbol in sorted(all_symbols):
    csv_data = csv_pnl_totals.get(symbol, {})
    log_data = log_pnl.get(symbol, {})
    
    csv_pnl = csv_data.get('total_pnl', 0.0)
    log_pnl_val = log_data.get('pnl', 0.0)
    
    if symbol in csv_pnl_totals and symbol in log_pnl:
        diff = abs(csv_pnl - log_pnl_val)
        diff_pct = (diff / abs(csv_pnl) * 100) if csv_pnl != 0 else 0
        
        status = "✅ MATCH" if diff < 0.01 else "❌ MISMATCH"
        
        print(f"\n{symbol}:")
        print(f"  CSV Total PnL:     ${csv_pnl:+.6f} ({csv_data.get('trade_count', 0)} trades)")
        print(f"  Bot Recorded PnL:  ${log_pnl_val:+.6f}")
        print(f"  Difference:        ${diff:.6f} ({diff_pct:.2f}%)")
        print(f"  Status:            {status}")
        
        if diff >= 0.01:
            mismatches.append({
                'symbol': symbol,
                'csv_pnl': csv_pnl,
                'log_pnl': log_pnl_val,
                'diff': diff
            })
        else:
            matches.append(symbol)
    elif symbol in csv_pnl_totals:
        print(f"\n{symbol}:")
        print(f"  CSV Total PnL:     ${csv_pnl:+.6f} ({csv_data.get('trade_count', 0)} trades)")
        print(f"  Bot Recorded PnL:  NOT FOUND IN LOG")
        print(f"  Status:            ⚠️ MISSING")
    elif symbol in log_pnl:
        print(f"\n{symbol}:")
        print(f"  CSV Total PnL:     NOT FOUND IN CSV")
        print(f"  Bot Recorded PnL:  ${log_pnl_val:+.6f}")
        print(f"  Status:            ⚠️ MISSING")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"✅ Matches: {len(matches)}")
print(f"❌ Mismatches: {len(mismatches)}")
print(f"⚠️  Missing in log: {len([s for s in csv_pnl_totals.keys() if s not in log_pnl])}")
print(f"⚠️  Missing in CSV: {len([s for s in log_pnl.keys() if s not in csv_pnl_totals])}")

if mismatches:
    print("\n❌ DETAILED MISMATCHES:")
    for m in mismatches:
        print(f"  {m['symbol']}: CSV=${m['csv_pnl']:+.6f} vs Bot=${m['log_pnl']:+.6f} (diff=${m['diff']:.6f})")

# Show CSV trade details for mismatches
if mismatches:
    print("\n" + "=" * 80)
    print("CSV TRADE DETAILS FOR MISMATCHES")
    print("=" * 80)
    for m in mismatches:
        symbol = m['symbol']
        if symbol in csv_pnl_totals:
            print(f"\n{symbol} - CSV Trades:")
            for trade in csv_pnl_totals[symbol]['trades']:
                print(f"  {trade['date']}: Size={trade['size']:.6f}, Price={trade['price']:.6f}, PnL=${trade['pnl']:+.6f}")
