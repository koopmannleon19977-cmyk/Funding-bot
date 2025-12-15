#!/usr/bin/env python3
"""
Detaillierte Konsistenzprüfung der CSV-Dateien
"""

import csv
from decimal import Decimal
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def round_val(v, decimals=8):
    """Sichere Runden von Dezimalwerten"""
    try:
        return round(float(v), decimals)
    except:
        return None

# ==============================================================================
# DATEN LADEN
# ==============================================================================

# Lighter Trade Export (Close Trades)
lighter_trades = []
with open(PROJECT_ROOT / 'lighter-trade-export-2025-12-15T07_30_31.266Z-UTC.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        if 'Close' in row['Side']:
            lighter_trades.append(row)

# Bot's trades.csv
bot_trades = []
with open(PROJECT_ROOT / 'trades.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_trades.append(row)

# Bot's realized_pnl.csv
bot_pnl = []
with open(PROJECT_ROOT / 'realized_pnl.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_pnl.append(row)

# Lighter Funding Export
lighter_funding = []
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T07_30_41.264Z-UTC.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        lighter_funding.append(row)

# Bot's funding_fees.csv
bot_funding = []
with open(PROJECT_ROOT / 'funding_fees.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_funding.append(row)

# ==============================================================================
# 5 VERSCHIEDENE TRADES AUSWÄHLEN
# ==============================================================================

selected_trades = []
symbols_seen = set()

for trade in lighter_trades:
    symbol = trade['Market']
    if symbol not in symbols_seen and len(selected_trades) < 5:
        selected_trades.append(trade)
        symbols_seen.add(symbol)

# ==============================================================================
# TRADE VALIDIERUNG
# ==============================================================================

print("="*90)
print("TRADE VALIDATION".center(90))
print("="*90)

matches = 0
mismatches = 0

for idx, lt in enumerate(selected_trades, 1):
    symbol = lt['Market']
    lt_date = lt['Date']
    lt_size = round_val(lt['Size'])
    lt_price = round_val(lt['Price'])
    lt_pnl = round_val(lt['Closed PnL'])
    
    print(f"\nTrade {idx}: {symbol} | {lt_date}")
    print("-" * 90)
    
    # Finde in Bot trades.csv
    bot_t = None
    for t in bot_trades:
        if symbol in t['market']:
            if 'SELL' in t['side'] and abs(round_val(t['qty']) - lt_size) < 0.1:
                bot_t = t
                break
    
    # Finde in Bot realized_pnl.csv
    bot_p = None
    for p in bot_pnl:
        if symbol in p['market']:
            if abs(round_val(p['size']) - lt_size) < 0.1:
                bot_p = p
                break
    
    # Lighter Daten
    print(f"Lighter Exchange:")
    print(f"  Entry Price:    N/A (Close Trade)")
    print(f"  Size:           {lt_size}")
    print(f"  Exit Price:     ${lt_price}")
    print(f"  Closed PnL:     ${lt_pnl}")
    print(f"  Time:           {lt_date}")
    
    # Bot trades.csv
    if bot_t:
        bot_t_price = round_val(bot_t['price'])
        bot_t_qty = round_val(bot_t['qty'])
        print(f"\nBot trades.csv:")
        print(f"  Entry Price:    ${bot_t_price}")
        print(f"  Size:           {bot_t_qty}")
        print(f"  Time:           {bot_t['time']}")
        
        price_match = abs(bot_t_price - lt_price) < 0.001
        size_match = abs(bot_t_qty - lt_size) < 0.1
        print(f"  Price Match:    {'✅ OK' if price_match else '❌ MISMATCH'}")
        print(f"  Size Match:     {'✅ OK' if size_match else '❌ MISMATCH'}")
    else:
        print(f"\nBot trades.csv: ❌ NICHT GEFUNDEN")
    
    # Bot realized_pnl.csv
    if bot_p:
        bot_p_size = round_val(bot_p['size'])
        bot_p_entry = round_val(bot_p['entry_price'])
        bot_p_exit = round_val(bot_p['exit_price'])
        bot_p_pnl = round_val(bot_p['realised_pnl'])
        bot_p_trade_pnl = round_val(bot_p['trade_pnl'])
        
        print(f"\nBot realized_pnl.csv:")
        print(f"  Entry Price:    ${bot_p_entry}")
        print(f"  Exit Price:     ${bot_p_exit}")
        print(f"  Size:           {bot_p_size}")
        print(f"  Realized PnL:   ${bot_p_pnl}")
        print(f"  Trade PnL:      ${bot_p_trade_pnl}")
        
        # Berechne erwartete PnL
        if bot_p_entry and bot_p_exit and bot_p_size:
            calc_pnl = (bot_p_exit - bot_p_entry) * bot_p_size
            pnl_match = abs(calc_pnl - bot_p_trade_pnl) < 0.01
            print(f"  Calculated PnL: ${calc_pnl}")
            print(f"  PnL Match:      {'✅ OK' if pnl_match else '❌ MISMATCH'}")
        
        # Vergleich mit Lighter
        pnl_lighter_match = abs(bot_p_pnl - lt_pnl) < 0.1
        print(f"  Lighter Match:  {'✅ OK' if pnl_lighter_match else '❌ MISMATCH'}")
        
        if bot_t and pnl_lighter_match and price_match and size_match:
            matches += 1
        else:
            mismatches += 1
    else:
        print(f"\nBot realized_pnl.csv: ❌ NICHT GEFUNDEN")
        mismatches += 1

# ==============================================================================
# FUNDING VALIDIERUNG
# ==============================================================================

print("\n" + "="*90)
print("FUNDING VALIDATION".center(90))
print("="*90)

# Wähle 4 Funding Payments aus verschiedenen Märkten
funding_selected = []
funding_symbols = set()

for f in lighter_funding:
    if f['Market'] not in funding_symbols and len(funding_selected) < 4:
        funding_selected.append(f)
        funding_symbols.add(f['Market'])

funding_matches = 0
funding_mismatches = 0

for idx, lf in enumerate(funding_selected, 1):
    symbol = lf['Market']
    lf_date = lf['Date']
    lf_size = round_val(lf['Position Size'])
    lf_payment = round_val(lf['Payment'])
    lf_rate = lf['Rate']
    
    print(f"\nFunding {idx}: {symbol} | {lf_date}")
    print("-" * 90)
    
    # Finde in Bot funding_fees.csv
    bot_f = None
    for f in bot_funding:
        if symbol in f['market']:
            if abs(round_val(f['position_size']) - lf_size) < 0.1:
                bot_f = f
                break
    
    # Lighter Daten
    print(f"Lighter Exchange:")
    print(f"  Position Size:  {lf_size}")
    print(f"  Payment Amount: ${lf_payment}")
    print(f"  Rate:           {lf_rate}")
    print(f"  Time:           {lf_date}")
    
    # Bot funding_fees.csv
    if bot_f:
        bot_f_size = round_val(bot_f['position_size'])
        bot_f_payment = round_val(bot_f['funding_payment'])
        bot_f_rate = round_val(bot_f['rate'], 10)
        
        print(f"\nBot funding_fees.csv:")
        print(f"  Position Size:  {bot_f_size}")
        print(f"  Payment Amount: ${bot_f_payment}")
        print(f"  Rate:           {bot_f_rate}")
        print(f"  Time:           {bot_f['time']}")
        
        size_match = abs(bot_f_size - lf_size) < 0.1
        payment_match = abs(bot_f_payment - lf_payment) < 0.0001
        
        print(f"  Size Match:     {'✅ OK' if size_match else '❌ MISMATCH'}")
        print(f"  Payment Match:  {'✅ OK' if payment_match else '❌ MISMATCH'}")
        
        if size_match and payment_match:
            funding_matches += 1
        else:
            funding_mismatches += 1
    else:
        print(f"\nBot funding_fees.csv: ❌ NICHT GEFUNDEN")
        funding_mismatches += 1

# ==============================================================================
# SPEZIELLE CHECKS
# ==============================================================================

print("\n" + "="*90)
print("SPECIAL CHECKS".center(90))
print("="*90)

# RESOLV-USD am 2025-12-14
print("\nCheck 1: RESOLV-USD am 2025-12-14 (Doppelte Position?)")
print("-" * 90)
resolv_trades = [t for t in lighter_trades if t['Market'] == 'RESOLV' and '2025-12-14' in t['Date']]
print(f"Gefundene RESOLV Close Trades am 2025-12-14: {len(resolv_trades)}")
for t in resolv_trades[:3]:
    print(f"  - {t['Date']}: Size={t['Size']}, Price={t['Price']}, PnL={t['Closed PnL']}")

# STRK-USD mit Extended Wait Rollback
print("\nCheck 2: STRK-USD mit Extended Wait Rollback")
print("-" * 90)
strk_trades = [t for t in lighter_trades if t['Market'] == 'STRK' and '2025-12-14' in t['Date']]
print(f"Gefundene STRK Close Trades am 2025-12-14: {len(strk_trades)}")
for t in strk_trades[:3]:
    print(f"  - {t['Date']}: Size={t['Size']}, Price={t['Price']}, PnL={t['Closed PnL']}")

# EDEN-USD (mehrere Trades)
print("\nCheck 3: EDEN-USD (mehrere Trades)")
print("-" * 90)
eden_trades = [t for t in lighter_trades if t['Market'] == 'EDEN' and '2025-12-14' in t['Date']]
print(f"Gefundene EDEN Close Trades am 2025-12-14: {len(eden_trades)}")
total_eden_size = sum(round_val(t['Size']) for t in eden_trades if round_val(t['Size']))
print(f"Gesamtgröße: {total_eden_size}")
for t in eden_trades[:5]:
    print(f"  - {t['Date']}: Size={t['Size']}, Price={t['Price']}, PnL={t['Closed PnL']}")

# ==============================================================================
# SUMMARY
# ==============================================================================

print("\n" + "="*90)
print("SUMMARY".center(90))
print("="*90)
print(f"\nTrade Validation:")
print(f"  Total Trades Checked:       {len(selected_trades)}")
print(f"  Trades 100% Match:          {matches}")
print(f"  Trades with Discrepancies:  {mismatches}")

print(f"\nFunding Validation:")
print(f"  Total Funding Checked:      {len(funding_selected)}")
print(f"  Funding 100% Match:         {funding_matches}")
print(f"  Funding with Discrepancies: {funding_mismatches}")

print(f"\nOverall Status:")
if mismatches == 0 and funding_mismatches == 0:
    print("  ✅ ALL CHECKS PASSED - 100% CONSISTENCY")
else:
    print(f"  ⚠️  FOUND {mismatches + funding_mismatches} DISCREPANCY/IES")

print("\n" + "="*90)
