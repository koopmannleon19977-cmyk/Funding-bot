import sys
import os
import time
import asyncio 
import sqlite3 
from datetime import datetime, timedelta

# --- Pfad-Setup ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

try:
    from src.adapters.lighter_adapter import LighterAdapter
    from src.adapters.x10_adapter import X10Adapter
except ImportError as e:
    print(f"Fehler beim Importieren der Adapter: {e}")
    sys.exit(1)

# --- KONFIGURATION (Angepasst an Profi-Strategie) ---
TAKER_FEE_X10 = 0.00025 # 0.025%
MAKER_FEE_X10 = 0.00000 # 0.000%
FEES_LIGHTER = 0.00000 # 0.000%
TOTAL_FEES_PERCENT = (MAKER_FEE_X10 + FEES_LIGHTER) * 100 # 0.00% Gesamtgebühren

POSITION_SIZE_USD = 1000 
CONCURRENT_REQUEST_LIMIT = 5 
TAKE_PROFIT_USD = 5.00 # Fester PnL-Take-Profit (Sicherheitsnetz)
MIN_DAILY_PROFIT_FILTER = 0.1 # (0.1% = 0.033% pro 8h) - Filtert "Small Opps"
REFRESH_DELAY_SECONDS = 300 # 5 Minuten
DB_FILE = "funding.db" 

# --- Datenbank-Funktionen (NEUE STRUKTUR) ---
def setup_database():
    """Erstellt die Tabellen, falls sie nicht existieren."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS opportunities (
        timestamp TEXT, symbol TEXT, long_exchange TEXT, short_exchange TEXT,
        net_funding_apy REAL, spread_percent REAL, break_even_days REAL,
        daily_profit_usd REAL, profit_7_day_usd REAL
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_open TEXT,
        symbol TEXT,
        status TEXT, 
        long_exchange TEXT,
        short_exchange TEXT,
        entry_price_long REAL,
        entry_price_short REAL,
        position_size_usd REAL,
        entry_net_funding_rate_hourly REAL, -- NEU: Speichert stündliche Rate
        negative_flip_start_time TEXT, 
        timestamp_close TEXT,
        exit_price_long REAL,
        exit_price_short REAL,
        unrealized_pnl_usd REAL,
        entry_spread_percent REAL,
        entry_break_even_days REAL
    )
    """)
    
    conn.commit()
    conn.close()

def log_opportunity_to_db(opp, conn):
    """Speichert eine einzelne Opportunity in der SQLite-Datenbank."""
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        
        cursor.execute("""
        INSERT INTO opportunities (
            timestamp, symbol, long_exchange, short_exchange, 
            net_funding_apy, spread_percent, break_even_days, 
            daily_profit_usd, profit_7_day_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, opp['symbol'], opp['long_exchange'], opp['short_exchange'],
            opp['net_funding_apy'], opp['spread_percent'], opp['break_even_days'],
            opp['daily_profit_usd'], opp['profit_7_day_usd']
        ))
    except Exception as e:
        print(f"Fehler beim Schreiben in die Datenbank (opportunities): {e}")

def open_paper_trade(opp, prices, conn):
    """Öffnet einen neuen Paper-Trade in der DB (Speichert Spread und Break-Even)."""
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        
        entry_price_long = prices['long']
        entry_price_short = prices['short']
        
        cursor.execute("""
        INSERT INTO paper_trades (
            timestamp_open, symbol, status, long_exchange, short_exchange,
            entry_price_long, entry_price_short, position_size_usd, unrealized_pnl_usd, 
            entry_net_funding_rate_hourly, entry_spread_percent, entry_break_even_days
        ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, 0.0, ?, ?, ?)
        """, (
            now, opp['symbol'], opp['long_exchange'], opp['short_exchange'],
            entry_price_long, entry_price_short, POSITION_SIZE_USD, 
            opp['net_rate_hourly'], opp['spread_percent'], opp['break_even_days'] # Speichert stündliche Rate
        ))
        print(f"--- 📈 PAPER TRADE ERÖFFNET: LONG {opp['long_exchange']} / SHORT {opp['short_exchange']} für {opp['symbol']} ---")
    except Exception as e:
        print(f"Fehler beim Öffnen des Paper-Trades: {e}")

def close_paper_trade(trade_id, exit_long, exit_short, current_pnl, conn):
    """Schließt einen Paper-Trade in der DB und realisiert den PnL."""
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        
        cursor.execute("""
        UPDATE paper_trades SET 
            status = 'CLOSED', timestamp_close = ?, exit_price_long = ?,
            exit_price_short = ?, unrealized_pnl_usd = ?
        WHERE id = ?
        """, (now, exit_long, exit_short, current_pnl, trade_id))
        print(f"--- 💸 PAPER TRADE GESCHLOSSEN: Trade ID {trade_id} | Realisierter PnL: {current_pnl:+.2f}€ ---")
    except Exception as e:
        print(f"Fehler beim Schließen des Paper-Trades: {e}")

# --- PnL Update-Funktion (JETZT MIT DYNAMISCHER TWITTER-LOGIK) ---
async def update_open_paper_trades(lighter, x10, conn):
    """Aktualisiert den PnL für alle offenen Paper-Trades und prüft auf Schließbedingungen."""
    cursor = conn.cursor()
    
    # Holt die neuen dynamischen Felder
    query = """
        SELECT id, symbol, long_exchange, short_exchange, entry_price_long, entry_price_short, 
               position_size_usd, timestamp_open, entry_net_funding_rate_hourly, 
               negative_flip_start_time, entry_spread_percent, entry_break_even_days
        FROM paper_trades WHERE status = 'OPEN'
    """
    open_trades = cursor.execute(query).fetchall()

    if not open_trades: return

    print(f"\n--- Aktualisiere {len(open_trades)} offenen Paper-Trade(s) PnL (Dynamische Profi-Logik) ---")
    
    for trade in open_trades:
        (trade_id, symbol, long_ex, short_ex, entry_long, entry_short, size_usd, 
         timestamp_open, entry_net_rate_hourly, neg_flip_start, 
         entry_spread, entry_be_days) = trade
        
        current_price_long = None
        current_price_short = None
        
        # 1. Aktuelle Preise und Funding Rates holen
        l_rate = await lighter.fetch_funding_rate_async(symbol) # STÜNDLICHE Rate
        x_rate = x10.fetch_funding_rate(symbol) # STÜNDLICHE Rate
        if long_ex == 'Lighter': current_price_long = await lighter.fetch_mark_price_async(symbol)
        else: current_price_long = x10.fetch_mark_price(symbol)
        if short_ex == 'Lighter': current_price_short = await lighter.fetch_mark_price_async(symbol)
        else: current_price_short = x10.fetch_mark_price(symbol)

        if current_price_long is None or current_price_short is None or l_rate is None or x_rate is None:
            print(f"Fehler: Konnte PnL/Close-Logik für Trade {trade_id} ({symbol}) nicht berechnen (Preis/Rate fehlt).")
            continue

        # 2. PnL berechnen (Preis-Delta)
        size_long_coin = size_usd / entry_long
        size_short_coin = size_usd / entry_short
        pnl_long = (current_price_long * size_long_coin) - size_usd
        pnl_short = size_usd - (current_price_short * size_short_coin) 
        total_pnl = pnl_long + pnl_short
        
        # 3. PnL in DB aktualisieren
        cursor.execute("UPDATE paper_trades SET unrealized_pnl_usd = ? WHERE id = ?", (total_pnl, trade_id))
        print(f"  Trade {trade_id} ({symbol}) | TOTAL PnL: {total_pnl:+.2f}€")

        # --- SCHLIESSEN-LOGIK (Twitter @DegeniusQ / @Quantzilla34) ---
        net_funding_hourly_current = l_rate - x_rate 
        should_close = False
        close_reason = ""
        
        # Aktuellen Spread berechnen
        current_spread_percent = (abs(current_price_long - current_price_short) / current_price_short) * 100
        
        # *** Twitter Logik 1: Funding Flip & 24h-Regel ***
        is_funding_negative = False
        # entry_net_rate_hourly ist die rohe STÜNDLICHE Rate (kann pos oder neg sein)
        if entry_net_rate_hourly > 0: # Wir haben Lighter geshortet (l_rate - x_rate war positiv)
            if net_funding_hourly_current < 0: is_funding_negative = True
        else: # Wir haben X10 geshortet (l_rate - x_rate war negativ)
            if net_funding_hourly_current > 0: is_funding_negative = True

        if is_funding_negative: 
            if neg_flip_start is None:
                neg_flip_start = datetime.now().isoformat()
                cursor.execute("UPDATE paper_trades SET negative_flip_start_time = ? WHERE id = ?", (neg_flip_start, trade_id))
            else:
                start_time = datetime.fromisoformat(neg_flip_start)
                if datetime.now() - start_time >= timedelta(hours=24):
                    should_close = True
                    close_reason = "FUNDING FLIP (24H NEGATIVE)"
        else: # Rate ist positiv, Zähler zurücksetzen
            if neg_flip_start is not None:
                cursor.execute("UPDATE paper_trades SET negative_flip_start_time = NULL WHERE id = ?", (trade_id,))
        
        # *** Twitter Logik 2: Dynamische Haltedauer (1.5x Break-Even) ***
        time_diff = datetime.now() - datetime.fromisoformat(timestamp_open) 
        holding_days = time_diff.total_seconds() / (60 * 60 * 24)
        dynamic_max_days = max(3.0, min(10.0, (entry_be_days * 1.5))) 
        
        if holding_days >= dynamic_max_days and not should_close:
            should_close = True
            close_reason = f"DYNAMIC TIME EXIT ({holding_days:.1f} > {dynamic_max_days:.1f} days)"
        
        # *** Twitter Logik 3: Dynamische Spread-Konvergenz (50% des Entry-Spreads) ***
        convergence_threshold = entry_spread * 0.5 # 50% Konvergenz
        if current_spread_percent <= convergence_threshold and not should_close:
            should_close = True
            close_reason = "DYNAMIC SPREAD CONVERGENCE"
        
        # *** Sicherheits-Logik (Stop Loss / Take Profit PnL) ***
        if total_pnl >= TAKE_PROFIT_USD and not should_close:
            should_close = True
            close_reason = "TAKE PROFIT (PnL)"
        elif total_pnl < -10.00 and not should_close: # Stop Loss
            should_close = True
            close_reason = "STOP LOSS"

        if should_close:
             close_paper_trade(trade_id, current_price_long, current_price_short, total_pnl, conn)
             print(f"  Trade {trade_id} ({symbol}) GESCHLOSSEN. Grund: {close_reason}")
             
    conn.commit()
# --- Ende PnL Update-Funktion ---


def get_common_symbols():
    try:
        with open("symbols_common.txt", "r") as f:
            symbols = f.read().splitlines()
        return symbols
    except FileNotFoundError:
        print("Fehler: 'symbols_common.txt' nicht gefunden.")
        return []

async def calculate_opportunity(symbol, lighter, x10):
    """Holt Daten für ein Symbol und berechnet die Opportunity."""
    
    try:
        l_price = await lighter.fetch_mark_price_async(symbol)
        l_rate = await lighter.fetch_funding_rate_async(symbol) # Holt STÜNDLICHE Rate
        x_price = x10.fetch_mark_price(symbol)
        x_rate = x10.fetch_funding_rate(symbol) # Holt STÜNDLICHE Rate

        if None in [l_price, l_rate, x_price, x_rate]:
            return None

        prices = {"Lighter": l_price, "X10": x_price}
        
        # net_rate_hourly ist die rohe Netto-STUNDEN-Rate (Dezimal)
        net_rate_hourly = l_rate - x_rate
        
        # Bestimme Long/Short basierend auf der rohen Rate
        if net_rate_hourly > 0: # l_rate > x_rate (d.h. Lighter zahlt mehr/kostet weniger als X10)
            long_exchange = "X10"; short_exchange = "Lighter"
            long_rate = x_rate; short_rate = l_rate # Raten sind stündlich
            trade_prices = {"long": prices["X10"], "short": prices["Lighter"]}
        else:
            long_exchange = "Lighter"; short_exchange = "X10"
            long_rate = l_rate; short_rate = x_rate # Raten sind stündlich
            trade_prices = {"long": prices["Lighter"], "short": prices["X10"]}

        # Für Berechnungen brauchen wir den absoluten (positiven) Netto-Wert
        net_funding_hourly_decimal = abs(net_rate_hourly)

        spread_percent = (abs(l_price - x_price) / x_price) * 100
        
        # --- KORREKTE APY-BERECHNUNG (Stündliche Raten) ---
        
        # 1. Tägliche Rate und APY (als Dezimalzahl)
        net_funding_daily_decimal = net_funding_hourly_decimal * 24
        net_funding_apy_decimal = net_funding_daily_decimal * 365
        
        # 2. Konvertiere in PROZENT für die Anzeige und Filterung
        net_funding_daily_percent = net_funding_daily_decimal * 100
        net_funding_apy = net_funding_apy_decimal * 100
        
        # 3. Berechne die 8-Stunden-Rate (in Prozent) für die Anzeige
        net_funding_8h_percent = (net_funding_hourly_decimal * 8) * 100
        # --- ENDE KORREKTE APY-BERECHNUNG ---

        one_time_cost_percent = spread_percent + TOTAL_FEES_PERCENT
        daily_profit_usd = (net_funding_daily_percent / 100) * POSITION_SIZE_USD
        
        if net_funding_daily_percent == 0: break_even_days = float('inf') 
        else: break_even_days = one_time_cost_percent / net_funding_daily_percent

        profit_7_day_usd = (daily_profit_usd * 7) - ((one_time_cost_percent / 100) * POSITION_SIZE_USD)

        # Wende den "Small Opps" Filter an
        if net_funding_daily_percent < MIN_DAILY_PROFIT_FILTER:
            return None
            
        # NEU: Volatilitäts-Filter (aus Twitter-Analyse)
        # Ignoriere "Fake Alpha" (1000% APY) wenn der Spread zu hoch ist
        
        # DEBUG: Zeige alle Spreads an, bevor gefiltert wird
        print(f"DEBUG-SCAN: {symbol} | Spread: {spread_percent:.2f}% | Tägliches Netto-Funding: {net_funding_daily_percent:.2f}%")
        
        if spread_percent > 2.0: # Ignoriere Trades mit über 2% Spread (wie MON-USD)
            return None

        # Berechne die APYs für die einzelnen Börsen (jetzt korrekt)
        long_rate_apy_calc = (long_rate * 24 * 365) * 100
        short_rate_apy_calc = (short_rate * 24 * 365) * 100

        return {
            "symbol": symbol, "long_exchange": long_exchange, "short_exchange": short_exchange,
            "long_rate_apy": long_rate_apy_calc, "short_rate_apy": short_rate_apy_calc, 
            "net_funding_apy": net_funding_apy, "net_funding_8h_percent": net_funding_8h_percent,
            "spread_percent": spread_percent, "spread_usd": (spread_percent / 100) * POSITION_SIZE_USD,
            "fees_usd": (TOTAL_FEES_PERCENT / 100) * POSITION_SIZE_USD, "daily_profit_usd": daily_profit_usd,
            "break_even_days": break_even_days, "profit_7_day_usd": profit_7_day_usd,
            "trade_prices": trade_prices,
            "net_rate_hourly": net_rate_hourly # Speichert die rohe STÜNDLICHE Rate (pos/neg)
        }
        
    except Exception as e:
        if "429" in str(e) or "Too Many Requests" in str(e): pass 
        else: print(f"Fehler bei Berechnung von {symbol}: {e}")
        return None

def print_opportunity(opp):
    """Druckt die Opportunity in deinem gewünschten Format."""
    print(f"--- ✅ {opp['symbol']} ---")
    print(f"  {opp['short_exchange']}: {opp['short_rate_apy']:.1f}% APY (SHORT → du erhältst)")
    print(f"  {opp['long_exchange']}: {opp['long_rate_apy']:.1f}% APY (LONG → du zahlst)")
    print(f"  Net Funding: {opp['net_funding_apy']:.1f}% APY ({opp['net_funding_8h_percent']:.4f}% pro 8h)")
    print(f"  Spread: {opp['spread_percent']:.2f}% (~{opp['spread_usd']:.2f}€ bei {POSITION_SIZE_USD}€ Position)")
    print(f"  Fees: {TOTAL_FEES_PERCENT:.2f}% (~{opp['fees_usd']:.2f}€)")
    print(f"  Daily Profit: {opp['daily_profit_usd']:.2f}€ (mit {POSITION_SIZE_USD}€ Position)")
    print(f"  Break-Even: {opp['break_even_days']:.1f} Tage")
    print(f"  7-Day Profit: +{opp['profit_7_day_usd']:.2f}€")
    print(f"  Action: LONG {opp['long_exchange']}, SHORT {opp['short_exchange']}")
    print("-" * 30)

async def run_calculation_with_semaphore(symbol, lighter, x10, semaphore):
    async with semaphore:
        return await calculate_opportunity(symbol, lighter, x10)

async def run_monitor_async(conn):
    """Die Haupt-Scan-Logik (wird alle 5 Minuten aufgerufen)"""
    
    print(f"Starte Monitor-Mode (Kontrolliert, {CONCURRENT_REQUEST_LIMIT} Anfragen gleichzeitig)...")
    print(f"Suche nach Opportunities (mind. {MIN_DAILY_PROFIT_FILTER}% täglicher Funding-Profit)...")
    
    print("Initialisiere Adapter (hole alle Markt-Daten)...")
    lighter = LighterAdapter()
    x10 = X10Adapter()
    
    await asyncio.gather(
        lighter.load_market_cache(),
        x10.load_market_cache()
    )
    
    # PnL für offene Trades aktualisieren (JETZT MIT DYNAMISCHER LOGIK)
    await update_open_paper_trades(lighter, x10, conn)
    
    common_symbols = get_common_symbols()
    print(f"\n{len(common_symbols)} gemeinsame Symbole gefunden. Scanne...")
    
    start_time = time.time()
    
    tasks = []
    semaphore = asyncio.Semaphore(CONCURRENT_REQUEST_LIMIT) 
    
    for symbol in common_symbols:
        tasks.append(run_calculation_with_semaphore(symbol, lighter, x10, semaphore))

    results = await asyncio.gather(*tasks)
    opportunities = [res for res in results if res is not None]

    end_time = time.time()
    print(f"\nScan abgeschlossen in {end_time - start_time:.2f} Sekunden.")

    if not opportunities:
        print("\n=== KEINE PROFITABLEN OPPORTUNITIES GEFUNDEN ===")
        print("Kriterien wurden nicht erfüllt.")
        return

    print(f"\n=== TOP {len(opportunities)} FUNDING OPPORTUNIES (wird in DB gespeichert) ===")
    
    opportunities.sort(key=lambda x: x['profit_7_day_usd'], reverse=True)
    
    # --- Paper-Trading-Logik (Nur 1 Trade gleichzeitig) ---
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM paper_trades WHERE status = 'OPEN'")
    any_open_trade = cursor.fetchone()
    
    if not any_open_trade and opportunities:
        top_opp = opportunities[0]
        open_paper_trade(top_opp, top_opp['trade_prices'], conn)

    for opp in opportunities:
        print_opportunity(opp)
        log_opportunity_to_db(opp, conn) 

# --- HAUPTSCHLEIFE (MIT DB) ---
if __name__ == "__main__":
    setup_database()
    print(f"Datenbank '{DB_FILE}' ist bereit.")
    
    while True: 
        try:
            os.system('cls')
            
            conn = sqlite3.connect(DB_FILE)
            asyncio.run(run_monitor_async(conn)) 
            conn.commit()
            conn.close()
            
            print(f"\nScan erfolgreich. Daten in '{DB_FILE}' gespeichert.")
            print(f"Warte {REFRESH_DELAY_SECONDS} Sekunden (5 Minuten) bis zum nächsten Scan.")
            time.sleep(REFRESH_DELAY_SECONDS)
            
        except KeyboardInterrupt:
            print("\nMonitor gestoppt. Auf Wiedersehen!")
            sys.exit(0)
        except Exception as e:
            print(f"Ein Fehler ist aufgetreten: {e}")
            if 'conn' in locals():
                conn.close() 
            print("Warte 60 Sekunden und versuche es erneut...")
            time.sleep(60)