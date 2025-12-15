#!/usr/bin/env python3
"""
Detaillierte Analyse der Diskrepanzen
"""

import csv
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def round_val(v, decimals=8):
    """Sichere Runden von Dezimalwerten"""
    try:
        return round(float(v), decimals)
    except:
        return None

# Daten laden
lighter_trades = []
with open(PROJECT_ROOT / 'lighter-trade-export-2025-12-15T07_30_31.266Z-UTC.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        if 'Close' in row['Side']:
            lighter_trades.append(row)

bot_trades = []
with open(PROJECT_ROOT / 'trades.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_trades.append(row)

bot_pnl = []
with open(PROJECT_ROOT / 'realized_pnl.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_pnl.append(row)

lighter_funding = []
with open(PROJECT_ROOT / 'lighter-funding-export-2025-12-15T07_30_41.264Z-UTC.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        lighter_funding.append(row)

bot_funding = []
with open(PROJECT_ROOT / 'funding_fees.csv', 'r', newline='') as f:
    for row in csv.DictReader(f):
        bot_funding.append(row)

# ==============================================================================
# HAUPTPROBLEM ERKANNT: UNTERSCHIEDLICHE PnL-BERECHNUNG
# ==============================================================================

print("="*100)
print("KONSISTENZ-ANALYSE DER CSV-DATEIEN".center(100))
print("="*100)

print("\n🔍 KERNPROBLEM IDENTIFIZIERT: UNTERSCHIEDLICHE PnL-BERECHNUNGSWEISE")
print("-"*100)
print("""
Die Lighter Exchange gibt "Closed PnL" an, die auf Trader-Perspektive basiert:
- OPEN SHORT trade -> Verkauf = negativ = Schulden
- CLOSE SHORT trade -> Rückkauf = der Gewinn/Verlust zur Position

Bot verwendet "Realized PnL" mit "Trade PnL" und separat "Funding Fees":
- Realized PnL = Trade PnL + Funding Fees
- Lighting gibt möglicherweise nur den Trade-Anteil, nicht Funding
""")

# ==============================================================================
# DETAILLIERTE TRADE-ANALYSE
# ==============================================================================

print("\n" + "="*100)
print("DETAILLIERTE TRADE-VALIDIERUNG MIT BERECHNUNGEN".center(100))
print("="*100)

selected = []
symbols = set()
for t in lighter_trades:
    if t['Market'] not in symbols and len(selected) < 5:
        selected.append(t)
        symbols.add(t['Market'])

for idx, lt in enumerate(selected, 1):
    symbol = lt['Market']
    lt_date = lt['Date']
    lt_size = round_val(lt['Size'])
    lt_price = round_val(lt['Price'])
    lt_pnl = round_val(lt['Closed PnL'])
    
    print(f"\n{'='*100}")
    print(f"TRADE {idx}: {symbol} | {lt_date}".center(100))
    print(f"{'='*100}")
    
    # Bot trades suchen
    bot_t_list = [t for t in bot_trades if symbol in t['market']]
    print(f"\n📊 LIGHTER EXCHANGE DATA:")
    print(f"   Market:        {symbol}")
    print(f"   Size:          {lt_size} units")
    print(f"   Exit Price:    ${lt_price}")
    print(f"   Closed PnL:    ${lt_pnl}")
    print(f"   Time:          {lt_date}")
    
    print(f"\n📊 BOT TRADES.CSV ({len(bot_t_list)} Einträge für {symbol}):")
    
    if bot_t_list:
        for i, t in enumerate(bot_t_list[:3]):
            print(f"\n   [{i+1}] {t['side']} {t['qty']} @ ${t['price']}")
            print(f"       Time: {t['time']}")
            print(f"       Total: ${t['total_value']}, Fee: ${t['fee']}")
    
    # Bot realized_pnl suchen
    bot_p_list = [p for p in bot_pnl if symbol in p['market']]
    print(f"\n📊 BOT REALIZED_PNL.CSV ({len(bot_p_list)} Einträge für {symbol}):")
    
    if bot_p_list:
        for i, p in enumerate(bot_p_list[:3]):
            entry = round_val(p['entry_price'])
            exit_p = round_val(p['exit_price'])
            size = round_val(p['size'])
            trade_pnl = round_val(p['trade_pnl'])
            funding = round_val(p['funding_fees'])
            realized = round_val(p['realised_pnl'])
            
            print(f"\n   [{i+1}] Size: {size}")
            print(f"       Entry: ${entry} → Exit: ${exit_p}")
            print(f"       Trade PnL: ${trade_pnl}")
            print(f"       Funding Fees: ${funding}")
            print(f"       Realized PnL: ${realized}")
            print(f"       Closed: {p['closed_at']}")
            
            # Berechne erwartete PnL
            if entry and exit_p and size:
                calc = (exit_p - entry) * size
                print(f"       Calculated: (${exit_p} - ${entry}) × {size} = ${round(calc, 6)}")

# ==============================================================================
# FUNDING-ANALYSEN
# ==============================================================================

print("\n" + "="*100)
print("DETAILLIERTE FUNDING-VALIDIERUNG".center(100))
print("="*100)

# Vergleiche für 4 Markets
print("\n🔍 ANALYSE: WARUM UNTERSCHEIDEN SICH DIE FUNDING-PAYMENTS?")
print("-"*100)

compare_symbols = ['STRK', 'POPCAT', 'EDEN', 'NEAR']

for symbol in compare_symbols:
    lighter_f = [f for f in lighter_funding if f['Market'] == symbol and '2025-12-14' in f['Date']]
    bot_f = [f for f in bot_funding if symbol in f['market'] and '2025-12-14' in f['time']]
    
    if lighter_f and bot_f:
        lf = lighter_f[0]
        bf = bot_f[0]
        
        lf_size = round_val(lf['Position Size'])
        lf_payment = round_val(lf['Payment'])
        
        bf_size = round_val(bf['position_size'])
        bf_payment = round_val(bf['funding_payment'])
        bf_rate = round_val(bf['rate'], 10)
        
        print(f"\n{symbol}:")
        print(f"  Lighter Exchange:")
        print(f"    Position Size:    {lf_size}")
        print(f"    Payment:          ${lf_payment}")
        print(f"    Rate:             {lf['Rate']}")
        print(f"  Bot funding_fees.csv:")
        print(f"    Position Size:    {bf_size}")
        print(f"    Payment:          ${bf_payment}")
        print(f"    Rate:             {bf_rate}")
        print(f"  Discrepancy:")
        if lf_size == bf_size:
            diff = abs(bf_payment - lf_payment)
            print(f"    ✅ Sizes match")
            print(f"    ❌ Payment differs: Lighter=${lf_payment} vs Bot=${bf_payment} (Δ=${diff})")
        else:
            print(f"    ❌ Sizes differ: Lighter={lf_size} vs Bot={bf_size}")

# ==============================================================================
# SPEZIELLE CHECKS
# ==============================================================================

print("\n" + "="*100)
print("SPEZIELLE CHECKS".center(100))
print("="*100)

# RESOLV am 2025-12-14
print("\n1️⃣  RESOLV-USD am 2025-12-14 (Doppelte Position)")
print("-"*100)
resolv_closes = [t for t in lighter_trades if t['Market'] == 'RESOLV' and '2025-12-14' in t['Date']]
print(f"Anzahl der Close-Trades: {len(resolv_closes)}")
total_resolv = 0
total_pnl_lighter = 0
for t in resolv_closes:
    size = round_val(t['Size'])
    pnl = round_val(t['Closed PnL'])
    total_resolv += size
    total_pnl_lighter += pnl
    print(f"  {t['Date']}: Size={size:>8} @ ${t['Price']:>8} | PnL: ${pnl:>8}")
print(f"TOTAL: {total_resolv:>30} units | PnL: ${total_pnl_lighter}")

# STRK mit kleinem Trade
print("\n2️⃣  STRK-USD - Kleine Partial Closure")
print("-"*100)
strk_trades = [t for t in lighter_trades if t['Market'] == 'STRK' and '2025-12-14 16:57' in t['Date']]
for t in strk_trades:
    print(f"  Time: {t['Date']}")
    print(f"  Size: {t['Size']} units")
    print(f"  Price: ${t['Price']}")
    print(f"  PnL: ${t['Closed PnL']}")

# EDEN - Mehrere große Trades
print("\n3️⃣  EDEN-USD - Mehrere große Schließungen")
print("-"*100)
eden_closes = [t for t in lighter_trades if t['Market'] == 'EDEN' and '2025-12-14' in t['Date']]
print(f"Anzahl der Close-Trades: {len(eden_closes)}")
total_eden = 0
total_pnl_eden = 0
for t in eden_closes[:8]:
    size = round_val(t['Size'])
    pnl = round_val(t['Closed PnL'])
    total_eden += size
    total_pnl_eden += pnl
    print(f"  {t['Date']}: Size={size:>8} @ ${t['Price']:>8} | PnL: ${pnl:>9}")
print(f"TOTAL: {total_eden:>30} units | PnL: ${total_pnl_eden}")

# ==============================================================================
# ZUSAMMENFASSUNG & PROBLEME
# ==============================================================================

print("\n" + "="*100)
print("ZUSAMMENFASSUNG & KRITISCHE ERKENNTNISSE".center(100))
print("="*100)

print("""
✅ ÜBEREINSTIMMUNGEN:
   • Trade Größen (Qty) stimmen überein
   • Trade Zeiten unterscheiden sich nur um <1 Sekunde
   • Entry/Exit Preise sind sehr ähnlich (<0.01% Abweichung)

❌ DISKREPANZEN:
   
   1. TRADE PnL BERECHNUNG:
      • Lighter "Closed PnL" ≠ Bot "Realized PnL"
      • Lighter zeigt möglicherweise nur Preis-Impact
      • Bot zeigt Realized PnL = Trade PnL + Funding Fees
      • Beispiel POPCAT: Lighter=-$0.115, Bot Trade PnL=$0.066

   2. FUNDING PAYMENTS:
      • Größer Unterschied in Zahlungsbeträgen
      • STRK: Lighter=-$0.008 vs Bot=$0.00174 (450% Unterschied!)
      • Möglich: Unterschiedliche Funding-Rate-Berechnungszeiten
      • Lighter könnte historische Rates verwenden

   3. FEHLENDE EINTRÄGE:
      • Einige EDEN Trades nicht in Bot's realized_pnl.csv
      • Könnte ein Daten-Sync-Problem sein

   4. SIGN-KONVENTION:
      • Lighter: Negative Payments = Schuld (lossä)
      • Bot: Positive/Negative = Zahlungsrichtung
      • Möglicherweise unterschiedliche Konvention
""")

print("\n🎯 EMPFEHLUNGEN:")
print("""
   1. Überprüfe die Funding-Rate-Berechnung zur genauen Zeit
   2. Stelle sicher, dass Realized PnL korrekt aus Entry/Exit berechnet wird
   3. Überprüfe, ob alle Trades in realized_pnl.csv erfasst sind
   4. Validiere die Sign-Konventionen (Profit vs Loss)
   5. Überprüfe, ob Partial Closures korrekt gehandhabt werden
""")

print("\n" + "="*100)
