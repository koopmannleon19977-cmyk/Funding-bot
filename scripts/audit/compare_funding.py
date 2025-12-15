import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load bot export
bot_data = []
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T08_11_53.047Z-UTC.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        bot_data.append(row)

print('=== BOT EXPORT ===')
print(f'Anzahl Einträge: {len(bot_data)}')
if bot_data:
    dates = [row['Date'] for row in bot_data]
    print(f'Zeitraum: {min(dates)} bis {max(dates)}')
    print(f'Spalten: {list(bot_data[0].keys())}')
print()

# Load Lighter export
lighter_data = []
lighter_path = PROJECT_ROOT / 'funding_fees.csv'
with open(lighter_path, 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        lighter_data.append(row)

print('=== LIGHTER EXPORT ===')
print(f'Anzahl Einträge: {len(lighter_data)}')
if lighter_data:
    times = [row['time'] for row in lighter_data]
    print(f'Zeitraum: {min(times)} bis {max(times)}')
    print(f'Spalten: {list(lighter_data[0].keys())}')
print()

# Calculate totals
bot_total = sum(float(row['Payment']) for row in bot_data)
lighter_total = sum(float(row['funding_payment']) for row in lighter_data)

print('=== SUMMEN VERGLEICH ===')
print(f'Bot Total Payment: {bot_total:.6f}')
print(f'Lighter Total Payment: {lighter_total:.6f}')
print(f'Differenz: {abs(bot_total - lighter_total):.6f}')
print()

# Aggregate by market
bot_by_market = defaultdict(float)
for row in bot_data:
    market = row['Market'].upper()
    bot_by_market[market] += float(row['Payment'])

lighter_by_market = defaultdict(float)
for row in lighter_data:
    market = row['market'].replace('-USD', '').upper()
    lighter_by_market[market] += float(row['funding_payment'])

# Compare markets
print('=== MÄRKTE VERGLEICH ===')
bot_markets = set(bot_by_market.keys())
lighter_markets = set(lighter_by_market.keys())
print(f'Bot Märkte: {len(bot_markets)}')
print(f'Lighter Märkte: {len(lighter_markets)}')
only_bot = bot_markets - lighter_markets
only_lighter = lighter_markets - bot_markets
if only_bot:
    print(f'Nur im Bot: {only_bot}')
if only_lighter:
    print(f'Nur in Lighter: {only_lighter}')
print()

# Payment per market comparison
print('=== PAYMENT PRO MARKT ===')
all_markets = sorted(bot_markets | lighter_markets)
print(f'{"Markt":<15} {"Bot":>12} {"Lighter":>12} {"Diff":>12} {"Match":>6}')
print('-' * 60)

mismatches = []
for market in all_markets:
    bot_val = bot_by_market.get(market, 0)
    lighter_val = lighter_by_market.get(market, 0)
    diff = bot_val - lighter_val
    match = abs(diff) < 0.001
    status = "✓" if match else "✗"
    print(f'{market:<15} {bot_val:>12.6f} {lighter_val:>12.6f} {diff:>12.6f} {status:>6}')
    if not match:
        mismatches.append((market, bot_val, lighter_val, diff))
print()

# Detailed comparison for recent entries
print('=== DETAILLIERTER VERGLEICH (neueste Einträge) ===')
print('\nBot Daten (neueste 15):')
bot_sorted = sorted(bot_data, key=lambda x: x['Date'], reverse=True)[:15]
for row in bot_sorted:
    print(f"  {row['Market']:15} {row['Side']:6} {row['Date']} Pos:{row['Position Size']:>10} Pay:{float(row['Payment']):>12.6f}")

print('\nLighter Daten (neueste 15):')
lighter_sorted = sorted(lighter_data, key=lambda x: x['time'], reverse=True)[:15]
for row in lighter_sorted:
    market = row['market'].replace('-USD', '')
    side = row['side'].lower()
    time = row['time'][:19]
    pos = row['position_size']
    pay = float(row['funding_payment'])
    print(f"  {market:15} {side:6} {time} Pos:{pos:>10} Pay:{pay:>12.6f}")

# Entry-by-entry comparison
print('\n=== EINTRAG-FÜR-EINTRAG VERGLEICH ===')

# Create lookup for lighter data by (market, date_hour)
lighter_lookup = defaultdict(list)
for row in lighter_data:
    market = row['market'].replace('-USD', '').upper()
    # Parse time and extract hour
    time_str = row['time'][:13]  # YYYY-MM-DDTHH
    time_str = time_str.replace('T', ' ') + ':00:00'
    lighter_lookup[(market, time_str)].append(row)

# Check each bot entry
match_count = 0
no_match_count = 0
payment_diff_count = 0

print('\nPrüfe Bot-Einträge gegen Lighter...')
for row in bot_data[:50]:  # Check first 50
    market = row['Market'].upper()
    date = row['Date']
    bot_payment = float(row['Payment'])
    bot_pos = float(row['Position Size'])
    
    key = (market, date)
    lighter_entries = lighter_lookup.get(key, [])
    
    if not lighter_entries:
        # Check if there's a sign difference (SHORT vs LONG interpretation)
        no_match_count += 1
        if no_match_count <= 5:
            print(f"  NICHT GEFUNDEN: {market} {date}")
    else:
        # Find matching entry
        found = False
        for le in lighter_entries:
            lighter_payment = float(le['funding_payment'])
            lighter_pos = float(le['position_size'])
            # Check if payments are similar (allowing for sign differences)
            if abs(abs(bot_payment) - abs(lighter_payment)) < 0.001:
                match_count += 1
                found = True
                break
        if not found:
            payment_diff_count += 1
            if payment_diff_count <= 5:
                print(f"  PAYMENT DIFF: {market} {date} Bot:{bot_payment:.6f}")
                for le in lighter_entries:
                    print(f"    -> Lighter: {float(le['funding_payment']):.6f}")

print()
print('=== ZUSAMMENFASSUNG ===')
print(f'Total Bot Einträge: {len(bot_data)}')
print(f'Total Lighter Einträge: {len(lighter_data)}')
print(f'Bot Payment Summe: {bot_total:.6f}')
print(f'Lighter Payment Summe: {lighter_total:.6f}')
print(f'Gesamtdifferenz: {abs(bot_total - lighter_total):.6f}')
print()
print(f'Märkte mit Abweichungen: {len(mismatches)}')
if mismatches:
    print('Details der Abweichungen:')
    for market, bot_val, lighter_val, diff in mismatches:
        print(f'  {market}: Bot={bot_val:.6f}, Lighter={lighter_val:.6f}, Diff={diff:.6f}')
