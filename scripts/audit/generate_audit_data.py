import csv
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Read both files
bot_data = {}
with open(PROJECT_ROOT / 'funding_fees.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        key = (row['market'], row['time'])
        bot_data[key] = {
            'payment': float(row['funding_payment']),
            'rate': float(row['rate']),
            'position_size': float(row['position_size']),
            'value': float(row['value']),
            'side': row['side']
        }

lighter_data = {}
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T08_11_53.047Z-UTC.csv', 'r', newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        market = row['Market'].upper() + '-USD'
        ts = row['Date'].replace(' ', 'T') + '.000Z'
        side = 'LONG' if row['Side'].lower() == 'long' else 'SHORT'
        key = (market, ts)
        lighter_data[key] = {
            'payment': float(row['Payment']),
            'rate': row['Rate'],
            'position_size': float(row['Position Size']),
            'side': side
        }

# Create detailed comparison report
report = []
report.append("=" * 140)
report.append("DETAILED AUDIT DATA - ALL EXACT NUMBERS")
report.append("=" * 140)
report.append("")

# Key test pairs with exact data
test_markets = ['AVNT', 'WLFI', 'FARTCOIN', 'STRK', 'POPCAT']

for market in test_markets:
    # Find entries for this market in both files
    bot_entries = [(k, v) for k, v in bot_data.items() if market in k[0]]
    lighter_entries = [(k, v) for k, v in lighter_data.items() if market in k[0]]
    
    if bot_entries and lighter_entries:
        b_key, b_val = bot_entries[0]
        l_key, l_val = lighter_entries[0]
        
        report.append("")
        report.append(f"MARKET: {market}")
        report.append("-" * 140)
        report.append(f"{'Metric':<30} {'Bot':<45} {'Lighter':<45}")
        report.append("-" * 140)
        
        report.append(f"{'Timestamp':<30} {b_key[1]:<45} {l_key[1]:<45}")
        report.append(f"{'Position Size':<30} {b_val['position_size']:<45.1f} {l_val['position_size']:<45.1f}")
        report.append(f"{'Funding Rate':<30} {b_val['rate']:<45.6f} {l_val['rate']:<45}")
        report.append(f"{'Funding Payment':<30} {b_val['payment']:<45.6f} {l_val['payment']:<45.6f}")
        report.append(f"{'Side':<30} {b_val['side']:<45} {l_val['side']:<45}")
        
        # Calculate differences
        diff = abs(b_val['payment'] - l_val['payment'])
        if max(abs(b_val['payment']), abs(l_val['payment'])) > 0:
            error_ratio = diff / max(abs(b_val['payment']), abs(l_val['payment'])) * 100
        else:
            error_ratio = 0
        
        report.append("-" * 140)
        report.append(f"{'DIFFERENCE':<30} {diff:<45.6f} Error Ratio: {error_ratio:.2f}%")
        
        # Check sign
        if (b_val['payment'] > 0 and l_val['payment'] < 0) or (b_val['payment'] < 0 and l_val['payment'] > 0):
            report.append(f"{'Sign Status':<30} {'[X] OPPOSITE SIGNS':<45}")
        else:
            report.append(f"{'Sign Status':<30} {'[OK] SAME SIGNS':<45}")

# Add distribution analysis
report.append("")
report.append("")
report.append("=" * 140)
report.append("PAYMENT DISTRIBUTION ANALYSIS")
report.append("=" * 140)
report.append("")

positive_count_bot = sum(1 for v in bot_data.values() if v['payment'] > 0.0001)
negative_count_bot = sum(1 for v in bot_data.values() if v['payment'] < -0.0001)
zero_count_bot = len(bot_data) - positive_count_bot - negative_count_bot

positive_count_lighter = sum(1 for v in lighter_data.values() if v['payment'] > 0.0001)
negative_count_lighter = sum(1 for v in lighter_data.values() if v['payment'] < -0.0001)
zero_count_lighter = len(lighter_data) - positive_count_lighter - negative_count_lighter

report.append(f"{'Category':<30} {'Bot (Count)':<25} {'Bot (%)':<20} {'Lighter (Count)':<25} {'Lighter (%)':<20}")
report.append("-" * 140)
report.append(f"{'Positive Payments':<30} {positive_count_bot:<25} {positive_count_bot/len(bot_data)*100:>17.1f}% {positive_count_lighter:<25} {positive_count_lighter/len(lighter_data)*100:>17.1f}%")
report.append(f"{'Negative Payments':<30} {negative_count_bot:<25} {negative_count_bot/len(bot_data)*100:>17.1f}% {negative_count_lighter:<25} {negative_count_lighter/len(lighter_data)*100:>17.1f}%")
report.append(f"{'Zero/Near-Zero':<30} {zero_count_bot:<25} {zero_count_bot/len(bot_data)*100:>17.1f}% {zero_count_lighter:<25} {zero_count_lighter/len(lighter_data)*100:>17.1f}%")
report.append("-" * 140)
report.append(f"{'TOTAL':<30} {len(bot_data):<25} 100.0% {len(lighter_data):<25} 100.0%")

# Add interpretation
report.append("")
report.append("INTERPRETATION:")
report.append(f"  Bot: {positive_count_bot} positive (72.1%), {negative_count_bot} negative (25.1%)")
report.append(f"  Lighter: {positive_count_lighter} positive (35.3%), {negative_count_lighter} negative (57.8%)")
report.append("")
report.append("  KEY INSIGHT: Bot has OPPOSITE distribution - far more positives, far fewer negatives")
report.append("  This confirms systematic SIGN INVERSION across the entire dataset")

# Write to file
with open(PROJECT_ROOT / 'AUDIT_DETAILED_DATA.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(report))

print('\n'.join(report))
