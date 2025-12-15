"""Funding Fee Verification Script.

Vergleicht Bot-Tracking mit Exchange-Daten.
"""

from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Lade alle Daten
print("=" * 80)
print("FUNDING FEE VERIFICATION REPORT")
print("=" * 80)

# 1. Bot's Funding Fees
print("\n1. LOADING BOT FUNDING FEES (funding_fees.csv)")
bot_funding = pd.read_csv(PROJECT_ROOT / 'funding_fees.csv')
print(f"   Total entries: {len(bot_funding)}")
print(f"   Date range: {bot_funding['time'].min()} to {bot_funding['time'].max()}")
bot_total_funding = bot_funding['funding_payment'].sum()
print(f"   Total Funding Payments (Bot): {bot_total_funding:.6f}")

# 2. Exchange Funding Fees
print("\n2. LOADING EXCHANGE FUNDING FEES (lighter-funding-export)")
exchange_funding = pd.read_csv(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T08_11_53.047Z-UTC.csv')
print(f"   Total entries: {len(exchange_funding)}")
print(f"   Date range: {exchange_funding['Date'].min()} to {exchange_funding['Date'].max()}")
exchange_total_funding = exchange_funding['Payment'].sum()
print(f"   Total Funding Payments (Exchange): {exchange_total_funding:.6f}")

# 3. Vergleich der Summen
print("\n" + "=" * 80)
print("3. FUNDING FEE COMPARISON")
print("=" * 80)
print(f"   Bot Total:      {bot_total_funding:.6f}")
print(f"   Exchange Total: {exchange_total_funding:.6f}")
print(f"   Difference:     {abs(bot_total_funding - exchange_total_funding):.6f}")

# 4. Detaillierter Vergleich nach Market
print("\n" + "=" * 80)
print("4. MARKET-BY-MARKET COMPARISON")
print("=" * 80)

# Bot Funding nach Market
bot_by_market = bot_funding.groupby('market')['funding_payment'].sum()

# Exchange Funding nach Market (Market ohne -USD suffix)
exchange_funding['Market_Clean'] = exchange_funding['Market'] + '-USD'
exchange_by_market = exchange_funding.groupby('Market_Clean')['Payment'].sum()

# Alle Markets
all_markets = set(bot_by_market.index) | set(exchange_by_market.index)

discrepancies = []
print(f"\n{'Market':<20} {'Bot':<15} {'Exchange':<15} {'Diff':<15} {'Status'}")
print("-" * 80)

for market in sorted(all_markets):
    bot_val = bot_by_market.get(market, 0)
    exchange_val = exchange_by_market.get(market, 0)
    diff = bot_val - exchange_val
    
    # Status
    if abs(diff) < 0.0001:
        status = "✓ OK"
    elif market not in bot_by_market.index:
        status = "❌ MISSING IN BOT"
        discrepancies.append((market, 'Missing in Bot', exchange_val))
    elif market not in exchange_by_market.index:
        status = "⚠️ ONLY IN BOT"
        discrepancies.append((market, 'Only in Bot', bot_val))
    else:
        status = f"❌ DIFF {diff:.6f}"
        discrepancies.append((market, 'Value mismatch', diff))
    
    print(f"{market:<20} {bot_val:<15.6f} {exchange_val:<15.6f} {diff:<15.6f} {status}")

# 5. Trades Verification
print("\n" + "=" * 80)
print("5. TRADES VERIFICATION")
print("=" * 80)

bot_trades = pd.read_csv(PROJECT_ROOT / 'trades.csv')
exchange_trades = pd.read_csv(PROJECT_ROOT / 'lighter-trade-export-2025-12-15T08_11_16.906Z-UTC.csv')

print(f"   Bot Trades:      {len(bot_trades)}")
print(f"   Exchange Trades: {len(exchange_trades)}")

# Bot Trade Volume
bot_trade_volume = bot_trades['total_value'].sum()
print(f"\n   Bot Total Trade Volume:      ${bot_trade_volume:,.2f}")

# Exchange Trade Volume
exchange_trade_volume = exchange_trades['Trade Value'].sum()
print(f"   Exchange Total Trade Volume: ${exchange_trade_volume:,.2f}")
print(f"   Difference:                  ${abs(bot_trade_volume - exchange_trade_volume):,.2f}")

# 6. PnL from trades
print("\n" + "=" * 80)
print("6. PNL VERIFICATION")
print("=" * 80)

realized_pnl = pd.read_csv(PROJECT_ROOT / 'realized_pnl.csv')
print(f"   Total PnL Entries: {len(realized_pnl)}")

total_realised = realized_pnl['realised_pnl'].sum()
total_trade_pnl = realized_pnl['trade_pnl'].sum()
total_funding_from_pnl = realized_pnl['funding_fees'].sum()
total_trading_fees = realized_pnl['trading_fees'].sum()

print(f"\n   From realized_pnl.csv:")
print(f"   - Total Realised PnL:  ${total_realised:,.6f}")
print(f"   - Trade PnL:           ${total_trade_pnl:,.6f}")
print(f"   - Funding Fees:        ${total_funding_from_pnl:,.6f}")
print(f"   - Trading Fees:        ${total_trading_fees:,.6f}")

# Exchange Closed PnL
exchange_closed_pnl = exchange_trades[exchange_trades['Closed PnL'] != '-']['Closed PnL'].astype(float).sum()
print(f"\n   From Exchange Export:")
print(f"   - Closed PnL:          ${exchange_closed_pnl:,.6f}")

# 7. Detailed Time-based comparison
print("\n" + "=" * 80)
print("7. LATEST FUNDING EVENTS COMPARISON (Last 24h)")
print("=" * 80)

# Parse dates
bot_funding['datetime'] = pd.to_datetime(bot_funding['time'])
exchange_funding['datetime'] = pd.to_datetime(exchange_funding['Date'])

cutoff = datetime.now() - timedelta(days=2)

recent_bot = bot_funding[bot_funding['datetime'] > cutoff].copy()
recent_exchange = exchange_funding[exchange_funding['datetime'] > cutoff].copy()

print(f"\n   Recent Bot Funding Events: {len(recent_bot)}")
print(f"   Recent Exchange Events:    {len(recent_exchange)}")

# Group by hour and compare
print("\n   Recent Bot Funding by Time:")
for idx, row in recent_bot.head(20).iterrows():
    print(f"     {row['time']} | {row['market']:<15} | Size: {row['position_size']:<10} | Payment: {row['funding_payment']:.6f}")

print("\n   Recent Exchange Funding by Time:")
for idx, row in recent_exchange.head(20).iterrows():
    print(f"     {row['Date']} | {row['Market']:<15} | Size: {row['Position Size']:<10} | Payment: {row['Payment']:.6f}")

# 8. WICHTIG: Vorzeichen-Analyse
print("\n" + "=" * 80)
print("8. SIGN ANALYSIS (IMPORTANT)")
print("=" * 80)

# Check if signs are inverted
bot_positive = (bot_funding['funding_payment'] > 0).sum()
bot_negative = (bot_funding['funding_payment'] < 0).sum()
exchange_positive = (exchange_funding['Payment'] > 0).sum()
exchange_negative = (exchange_funding['Payment'] < 0).sum()

print(f"\n   Bot Funding Signs:")
print(f"     - Positive (received): {bot_positive}")
print(f"     - Negative (paid):     {bot_negative}")

print(f"\n   Exchange Funding Signs:")
print(f"     - Positive (received): {exchange_positive}")
print(f"     - Negative (paid):     {exchange_negative}")

# 9. Position Side Comparison
print("\n" + "=" * 80)
print("9. POSITION SIDE VERIFICATION")
print("=" * 80)

bot_longs = bot_funding[bot_funding['side'] == 'LONG']['market'].nunique()
bot_shorts = bot_funding[bot_funding['side'] == 'SHORT']['market'].nunique()

print(f"\n   Bot Position Sides:")
print(f"     - LONG positions tracked:  {bot_longs} markets")
print(f"     - SHORT positions tracked: {bot_shorts} markets")

exchange_longs = exchange_funding[exchange_funding['Side'] == 'long']['Market'].nunique()
exchange_shorts = exchange_funding[exchange_funding['Side'] == 'short']['Market'].nunique()

print(f"\n   Exchange Position Sides:")
print(f"     - long positions:  {exchange_longs} markets")
print(f"     - short positions: {exchange_shorts} markets")

# 10. Summary
print("\n" + "=" * 80)
print("10. SUMMARY & DISCREPANCIES")
print("=" * 80)

if len(discrepancies) > 0:
    print(f"\n   ⚠️  Found {len(discrepancies)} markets with discrepancies:")
    for market, issue, value in discrepancies[:10]:
        print(f"     - {market}: {issue} ({value:.6f})")
else:
    print("\n   ✓ All markets match!")

# Final verdict
print("\n" + "=" * 80)
print("FINAL VERDICT")
print("=" * 80)

funding_diff_pct = abs(bot_total_funding - exchange_total_funding) / max(abs(exchange_total_funding), 0.0001) * 100

if funding_diff_pct < 5:
    print(f"\n   ✓ Funding fees are CORRECTLY tracked (diff: {funding_diff_pct:.2f}%)")
elif funding_diff_pct < 20:
    print(f"\n   ⚠️ Funding fees have MINOR discrepancies (diff: {funding_diff_pct:.2f}%)")
else:
    print(f"\n   ❌ Funding fees have SIGNIFICANT discrepancies (diff: {funding_diff_pct:.2f}%)")

# Check if signs might be inverted
if np.sign(bot_total_funding) != np.sign(exchange_total_funding):
    print("\n   ⚠️ WARNING: Signs appear to be INVERTED between bot and exchange!")
    print("     Bot records POSITIVE when receiving funding, Exchange may use different convention")

print("\n" + "=" * 80)
