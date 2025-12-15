import csv
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Read bot's funding_fees.csv
bot_data = {}
with open(PROJECT_ROOT / 'funding_fees.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        key = (row['market'].replace('-USD', '').upper(), row['time'][:10])  # Market and date
        bot_data[key] = {
            'payment': float(row['funding_payment']),
            'rate': float(row['rate']),
            'position_size': float(row['position_size']),
            'value': float(row['value']),
            'side': row['side'],
            'time': row['time'],
            'market_full': row['market']
        }

# Read Lighter funding export
lighter_data = {}
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T08_11_53.047Z-UTC.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        market = row['Market'].upper()
        date = row['Date'][:10]
        key = (market, date)
        
        lighter_data[key] = {
            'payment': float(row['Payment']),
            'rate': row['Rate'],
            'position_size': float(row['Position Size']),
            'side': row['Side'].upper(),
            'time': row['Date']
        }

print("=" * 120)
print("DETAILED FUNDING AUDIT - RATE & VALUE ANALYSIS")
print("=" * 120)

# Key pairs to check with detailed rate analysis
test_pairs = [
    ('AVNT', '2025-12-15'),
    ('WLFI', '2025-12-15'),
    ('FARTCOIN', '2025-12-15'),
    ('STRK', '2025-12-14'),
    ('POPCAT', '2025-12-14')
]

print("\nDETAILED COMPARISON OF TEST PAIRS:\n")

for market, date in test_pairs:
    key = (market, date)
    
    print(f"\n{market} on {date}")
    print("-" * 120)
    
    if key in bot_data:
        b = bot_data[key]
        print(f"Bot:")
        print(f"  Position Size:    {b['position_size']:.1f}")
        print(f"  Value (in USD):    {b['value']:.6f}")
        print(f"  Funding Rate:      {b['rate']:.6f} ({b['rate']*100:.6f}%)")
        print(f"  Funding Payment:   {b['payment']:.6f}")
        print(f"  Side:              {b['side']}")
        print(f"  Timestamp:         {b['time']}")
        
        # Calculate what payment should be based on position size and rate
        expected_by_pos_size = b['position_size'] * b['rate']
        print(f"  Calc (pos*rate):   {expected_by_pos_size:.6f}")
    else:
        print(f"Bot: NOT FOUND for {date}")
    
    if key in lighter_data:
        l = lighter_data[key]
        rate_pct = float(l['rate'].replace('%', '')) / 100 if '%' in l['rate'] else float(l['rate'])
        print(f"\nLighter:")
        print(f"  Position Size:    {l['position_size']:.1f}")
        print(f"  Funding Rate:      {l['rate']}")
        print(f"  Funding Payment:   {l['payment']:.6f}")
        print(f"  Side:              {l['side']}")
        print(f"  Timestamp:         {l['time']}")
        
        # Calculate what payment should be based on position size and rate
        expected_by_pos_size = l['position_size'] * rate_pct
        print(f"  Calc (pos*rate):   {expected_by_pos_size:.6f}")
    else:
        print(f"Lighter: NOT FOUND for {date}")
    
    if key in bot_data and key in lighter_data:
        b = bot_data[key]
        l = lighter_data[key]
        
        print(f"\nComparison:")
        print(f"  Bot payment:       {b['payment']:>12.6f}")
        print(f"  Lighter payment:   {l['payment']:>12.6f}")
        print(f"  Difference:        {abs(b['payment'] - l['payment']):>12.6f}")
        
        # Check if it's a rate * position calculation mismatch
        bot_calc_from_rate = b['position_size'] * b['rate']
        lighter_rate_pct = float(l['rate'].replace('%', '')) / 100
        lighter_calc_from_rate = l['position_size'] * lighter_rate_pct
        
        print(f"\n  Bot (pos * rate):      {bot_calc_from_rate:.6f}")
        print(f"  Lighter (pos * rate):  {lighter_calc_from_rate:.6f}")

# Now do a broad comparison
print("\n\n" + "=" * 120)
print("BROAD MARKET COMPARISON - SIGN DISTRIBUTION")
print("=" * 120)

markets_with_opposite_signs = []
markets_with_same_signs = []
markets_with_zero = []

for market in ['AVNT', 'WLFI', 'FARTCOIN', 'STRK', 'POPCAT', 'NEAR', 'EDEN', 'RESOLV', 'ONDO', 'SEI']:
    bot_entries = {k: v for k, v in bot_data.items() if k[0] == market}
    lighter_entries = {k: v for k, v in lighter_data.items() if k[0] == market}
    
    if bot_entries and lighter_entries:
        # Get first entry for each
        b = list(bot_entries.values())[0]
        l = list(lighter_entries.values())[0]
        
        bot_sign = '+' if b['payment'] > 0 else '-' if b['payment'] < 0 else '0'
        lighter_sign = '+' if l['payment'] > 0 else '-' if l['payment'] < 0 else '0'
        
        if bot_sign != '0' and lighter_sign != '0':
            if (b['payment'] > 0 and l['payment'] < 0) or (b['payment'] < 0 and l['payment'] > 0):
                markets_with_opposite_signs.append(market)
            else:
                markets_with_same_signs.append(market)
        else:
            markets_with_zero.append(market)

print(f"\nMarkets with OPPOSITE signs (bot vs Lighter): {len(markets_with_opposite_signs)}")
for m in markets_with_opposite_signs:
    print(f"  - {m}")

print(f"\nMarkets with SAME signs (bot vs Lighter): {len(markets_with_same_signs)}")
for m in markets_with_same_signs:
    print(f"  - {m}")

print(f"\nMarkets with ZERO in either: {len(markets_with_zero)}")
for m in markets_with_zero:
    print(f"  - {m}")

# Conclusion
print("\n" + "=" * 120)
print("AUDIT CONCLUSION")
print("=" * 120)

opposite_pct = len(markets_with_opposite_signs) / (len(markets_with_opposite_signs) + len(markets_with_same_signs)) * 100 if (len(markets_with_opposite_signs) + len(markets_with_same_signs)) > 0 else 0

print(f"\nOpposite sign occurrence: {opposite_pct:.1f}% of tested markets")

if len(markets_with_opposite_signs) > len(markets_with_same_signs):
    print("\n✗ CRITICAL ISSUE CONFIRMED:")
    print("  Bot funding payments show SYSTEMATIC SIGN INVERSION with Lighter export")
    print("  This means the funding_received fix has NOT been applied correctly")
    print("\n  EXPECTED FIX:")
    print("    Current code likely has: funding_received = -funding_rate * position_value")
    print("    Should be:                funding_received = funding_rate * position_value")
    print("\n  Alternatively:")
    print("    The sign convention may be inverted in how values are stored")
else:
    print("\n✓ Sign distribution appears CORRECT")
    print("  Funding values align with Lighter's convention")

print("\n" + "=" * 120)
