import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Read bot's funding_fees.csv
bot_data = {}
with open(PROJECT_ROOT / 'funding_fees.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        key = (row['market'], row['time'])  # Use market and timestamp as key
        bot_data[key] = {
            'payment': float(row['funding_payment']),
            'side': row['side'],
            'position_size': float(row['position_size'])
        }

# Read Lighter funding export
lighter_data = {}
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T08_11_53.047Z-UTC.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Convert market name and timestamp format
        market = row['Market'].upper() + '-USD'
        # Convert "2025-12-15 08:00:00" to ISO format
        ts = row['Date'].replace(' ', 'T') + '.000Z'
        side = 'LONG' if row['Side'].lower() == 'long' else 'SHORT'
        
        key = (market, ts)
        lighter_data[key] = {
            'payment': float(row['Payment']),
            'side': side,
            'position_size': float(row['Position Size']),
            'rate': row['Rate']
        }

print("=" * 100)
print("COMPREHENSIVE FUNDING AUDIT: Bot vs Lighter Export")
print("=" * 100)
print()

# Analyze matching entries
matches = 0
mismatches = []
inverted_matches = 0
zero_payment_bot = 0
zero_payment_lighter = 0

# Key pairs to check specifically
key_checks = {
    'AVNT-USD': '2025-12-15T08:00:00.692Z',
    'WLFI-USD': '2025-12-15T08:00:00.692Z',
    'FARTCOIN-USD': '2025-12-15T08:00:00.692Z',
    'STRK-USD': '2025-12-14T17:00:00.692Z',
    'POPCAT-USD': '2025-12-14T17:00:00.692Z'
}

print("KEY COMPARISON PAIRS:")
print("-" * 100)

for market, ts in key_checks.items():
    # Find matching entries (try both timestamp formats)
    ts_no_ms = ts.replace('.692Z', '.000Z')
    ts_old = ts.replace('Z', '.104Z')
    ts_old_no_ms = ts.replace('.692Z', '.104Z')
    
    bot_key = None
    lighter_key = None
    
    # Look in bot data
    for k in bot_data.keys():
        if k[0] == market and (k[1].startswith(ts[:13]) or k[1].startswith(ts_old[:13])):
            bot_key = k
            break
    
    # Look in lighter data
    for k in lighter_data.keys():
        if k[0] == market and (k[1].startswith(ts[:13]) or k[1].startswith(ts_old[:13])):
            lighter_key = k
            break
    
    print(f"\n{market} at {ts[:16]}:")
    if bot_key:
        b = bot_data[bot_key]
        print(f"  Bot:    {b['payment']:>12.6f} ({b['side']}, size: {b['position_size']})")
    else:
        print(f"  Bot:    NOT FOUND")
    
    if lighter_key:
        l = lighter_data[lighter_key]
        print(f"  Lighter: {l['payment']:>12.6f} ({l['side']}, size: {l['position_size']})")
    else:
        print(f"  Lighter: NOT FOUND")
    
    if bot_key and lighter_key:
        b = bot_data[bot_key]
        l = lighter_data[lighter_key]
        
        # Check if values match
        if abs(b['payment'] - l['payment']) < 1e-10:
            print(f"  ✓ MATCH: Values are identical")
            matches += 1
        elif abs(b['payment'] + l['payment']) < 1e-10:
            print(f"  ⚠ INVERTED: Bot={b['payment']}, Lighter={l['payment']} (opposite signs)")
            inverted_matches += 1
        else:
            error = abs(b['payment'] - l['payment'])
            error_ratio = error / max(abs(b['payment']), abs(l['payment']), 0.0001)
            print(f"  ✗ MISMATCH: Difference={error:.6f}, Error Ratio={error_ratio:.2%}")
            mismatches.append({
                'market': market,
                'timestamp': ts,
                'bot_value': b['payment'],
                'lighter_value': l['payment'],
                'difference': error,
                'ratio': error_ratio
            })

print("\n" + "=" * 100)
print("OVERALL STATISTICS:")
print("=" * 100)

# Count total comparable entries
comparable_markets = set()
for market, ts in bot_data.keys():
    market_base = market.replace('-USD', '')
    comparable_markets.add(market_base)

print(f"\nTotal entries in bot file: {len(bot_data)}")
print(f"Total entries in Lighter file: {len(lighter_data)}")

# Find entries in both
both = 0
bot_only = 0
lighter_only = 0

bot_markets = set(k[0] for k in bot_data.keys())
lighter_markets = set(k[0] for k in lighter_data.keys())

common_markets = bot_markets & lighter_markets

print(f"\nMarkets in both files: {len(common_markets)}")
print(f"Markets only in bot: {len(bot_markets - lighter_markets)}")
print(f"Markets only in Lighter: {len(lighter_markets - bot_markets)}")

print(f"\nKey findings for primary test pairs:")
print(f"  Exact matches: {matches}/5")
print(f"  Inverted (opposite signs): {inverted_matches}/5")
print(f"  Mismatches: {len(mismatches)}/5")

if mismatches:
    print(f"\nDetailed mismatches:")
    for m in mismatches:
        print(f"  {m['market']} @ {m['timestamp']}")
        print(f"    Bot: {m['bot_value']:.6f}")
        print(f"    Lighter: {m['lighter_value']:.6f}")
        print(f"    Difference: {m['difference']:.6f} ({m['ratio']:.2%})")

# Check for X10 data
print("\n" + "=" * 100)
print("X10 FUNDING DATA CHECK:")
print("=" * 100)

x10_markets = [k[0] for k in bot_data.keys() if 'X10' in k[0].upper()]
print(f"\nX10 markets in bot file: {len(x10_markets)}")
if x10_markets:
    print(f"X10 symbols found: {set(x10_markets)}")
else:
    print("No X10 markets found in bot funding_fees.csv")

# Analyze systematic patterns
print("\n" + "=" * 100)
print("PATTERN ANALYSIS:")
print("=" * 100)

positive_bot = sum(1 for k, v in bot_data.items() if v['payment'] > 0.0001)
negative_bot = sum(1 for k, v in bot_data.items() if v['payment'] < -0.0001)
positive_lighter = sum(1 for k, v in lighter_data.items() if v['payment'] > 0.0001)
negative_lighter = sum(1 for k, v in lighter_data.items() if v['payment'] < -0.0001)

print(f"\nBot funding_payment distribution:")
print(f"  Positive entries: {positive_bot}")
print(f"  Negative entries: {negative_bot}")
print(f"  Zero/near-zero: {len(bot_data) - positive_bot - negative_bot}")

print(f"\nLighter Payment distribution:")
print(f"  Positive entries: {positive_lighter}")
print(f"  Negative entries: {negative_lighter}")
print(f"  Zero/near-zero: {len(lighter_data) - positive_lighter - negative_lighter}")

print("\n" + "=" * 100)
print("VERDICT ON FUNDING FIX:")
print("=" * 100)

if inverted_matches == 5 and len(mismatches) == 0:
    print("\n✗ CRITICAL ISSUE: Bot shows INVERTED funding signs!")
    print("  All test pairs show opposite signs (negative where Lighter shows positive/vice versa)")
    print("  FIX NEEDED: Change 'funding_received = change' to 'funding_received = -change'")
    print("  (or vice versa depending on current implementation)")
elif matches == 5:
    print("\n✓ FUNDING FIX VERIFIED: All test pairs match perfectly!")
    print("  Bot and Lighter funding values align correctly")
    print("  The funding_received fix has been applied correctly")
elif len(mismatches) > 0:
    print(f"\n⚠ PARTIAL ISSUES: {len(mismatches)} test pairs show discrepancies")
    print("  Not a simple sign inversion - further investigation needed")
    for m in mismatches:
        print(f"    {m['market']}: Bot={m['bot_value']:.6f}, Lighter={m['lighter_value']:.6f}")
else:
    print("\n? INCONCLUSIVE: Cannot determine fix status from available data")

print("\n" + "=" * 100)
