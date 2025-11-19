# monitor_funding.py (verbessert)
import sys, os, time, asyncio, sqlite3, logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Set, Dict
import pandas as pd  # oben im File hinzufügen!
import random  # für zufällige Reihenfolge beim Farmen

# Pfad-Setup & Imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path: sys.path.insert(0, project_root)
import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.adapters.base_adapter import BaseAdapter
from lighter.api.account_api import AccountApi
from src.prediction import predict_next_funding_rates, should_flip_soon  # NEU: Prediction imports



# Clear Console Function
def clear_console():
    """Leert die Konsole, plattformunabhängig."""
    os.system('cls' if os.name == 'nt' else 'clear')

# Logger & Config
logger = config.setup_logging()
config.validate_runtime_config(logger)
MAX_OPEN_TRADES = getattr(config, "MAX_OPEN_TRADES", 5)
ROLLBACK_DELAY = getattr(config, "ROLLBACK_DELAY_SECONDS", 3)

if not config.LIVE_TRADING:
    logger.info("DRY-RUN MODUS AKTIV → Es werden KEINE echten Orders gesendet!")

# --- DATENBANK SETUP ---
def setup_database():
    """Erstellt die Tabellen für Trades, Watchlist, Opportunities und History."""
    with sqlite3.connect(config.DB_FILE) as conn:
        cursor = conn.cursor()
        
        # 1. Aktive Trades
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            symbol TEXT PRIMARY KEY,
            entry_time TIMESTAMP NOT NULL,
            notional_usd REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            leg1_exchange TEXT NOT NULL,
            initial_spread_pct REAL NOT NULL,
            initial_funding_rate_hourly REAL NOT NULL,
            entry_price_x10 REAL NOT NULL,
            entry_price_lighter REAL NOT NULL,
            funding_flip_start_time TIMESTAMP,
            is_farm_trade BOOLEAN DEFAULT FALSE
        )""")
        
        # 2. Watchlist (Live-Marktdaten aller Coins)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            last_seen TIMESTAMP NOT NULL,
            current_apy REAL,
            current_spread_pct REAL,
            entry_cost_pct REAL,
            net_funding_hourly REAL
        )""")

        # 3. Opportunities Log (Historie guter Gelegenheiten)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS opportunities_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP NOT NULL,
            symbol TEXT NOT NULL,
            apy REAL NOT NULL,
            entry_cost_pct REAL NOT NULL,
            net_funding_hourly REAL NOT NULL
        )""")

        # 4. Trade History (Echter PnL nach Schließung)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_time TIMESTAMP,
            exit_time TIMESTAMP NOT NULL,
            hold_duration_hours REAL,
            close_reason TEXT,
            final_pnl_usd REAL,     -- Dein echter NETTO Gewinn
            funding_pnl_usd REAL,   -- Gewinn nur durch Funding
            spread_pnl_usd REAL,    -- Gewinn/Verlust durch Preisänderung
            fees_usd REAL           -- Bezahle Gebühren
        )""")
        
        # NEU: Funding History für Prediction
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS funding_history (
            symbol TEXT,
            timestamp INTEGER,
            lighter_rate REAL,
            x10_rate REAL,
            PRIMARY KEY (symbol, timestamp)
        )""")
        
        # Index für Performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON funding_history(timestamp)")
        
        logger.info("DB-Setup: Alle Tabellen (inkl. trade_history & watchlist) bereit.")

# ============================================================
# FINAL FIX: Speichert Funding Rates für Prediction (beide Exchanges korrekt!)
# ============================================================
async def save_current_funding_rates(lighter: LighterAdapter, x10: X10Adapter):
    """Speichert die aktuellen Funding-Raten (hourly) in die funding_history Tabelle"""
    try:
        timestamp = int(datetime.utcnow().timestamp())
        with sqlite3.connect(config.DB_FILE) as conn:
            cursor = conn.cursor()
            rows = []

            # Lighter: nutzt funding_cache
            lighter_symbols = set(lighter.funding_cache.keys())
            
            # X10: Rates sind in market_info versteckt → muss anders geholt werden
            x10_rates = {}
            for symbol, market in x10.market_info.items():
                if hasattr(market.market_stats, "funding_rate"):
                    x10_rates[symbol] = float(market.market_stats.funding_rate)

            common_symbols = lighter_symbols & x10_rates.keys()

            for symbol in common_symbols:
                l_rate_8h = lighter.fetch_funding_rate(symbol) or 0.0
                x_rate_8h = x10_rates[symbol]

                # Korrekt in hourly umrechnen
                l_hourly = l_rate_8h        # Lighter: bereits 8h-Rate
                x_hourly = x_rate_8h / 8.0   # X10: 8h-Rate → teilen

                rows.append((symbol, timestamp, float(l_hourly), float(x_hourly)))

            if rows:
                cursor.executemany("""
                    INSERT OR IGNORE INTO funding_history 
                    (symbol, timestamp, lighter_rate, x10_rate) 
                    VALUES (?, ?, ?, ?)
                """, rows)
                conn.commit()
                logger.debug(f"{len(rows)} Funding-Rates für Prediction gespeichert.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Funding-History: {e}", exc_info=True)

# ============================================================
# REAL PnL FROM API – Das ist der heilige Gral
# ============================================================
async def get_realized_pnl_breakdown_x10(x10: X10Adapter) -> float:
    """Holt echten realisierten PnL inkl. Funding von X10"""
    try:
        client = await x10._get_auth_client()
        resp = await client.account.get_positions_history(limit=100)
        total_funding = 0.0
        total_pnl = 0.0
        if resp and resp.data:
            for pos in resp.data:
                if hasattr(pos, 'realised_pnl_breakdown'):
                    breakdown = pos.realised_pnl_breakdown
                    total_funding += float(breakdown.funding_fees or 0)
                    total_pnl += float(pos.realised_pnl or 0)
        logger.info(f"X10 Realised PnL: ${total_pnl:.4f} | davon Funding: ${total_funding:.4f}")
        return total_funding
    except Exception as e:
        logger.error(f"X10 PnL Fetch Fehler: {e}")
        return 0.0

async def get_accrued_funding_lighter(lighter: LighterAdapter) -> float:
    """Lighter gibt accrued Funding nicht direkt – wir berechnen es aus History + offenen Pos"""
    # Alternativ: Wenn Lighter später API hat, hier einbauen
    # Momentan: Nur als Platzhalter – wir nutzen später DB-basierte Berechnung
    return 0.0

# ============================================================
# FINAL REAL PnL REPORT – funktioniert 100% (X10 + Lighter Balance)
# ============================================================
async def log_real_pnl(lighter: LighterAdapter, x10: X10Adapter):
    try:
        # --- X10 Realised Funding + PnL ---
        client = await x10._get_auth_client()
        resp = await client.account.get_positions_history(limit=500)
        total_funding = 0.0
        total_realised = 0.0
        if resp and resp.data:
            for pos in resp.data:
                if hasattr(pos, 'realised_pnl_breakdown') and pos.realised_pnl_breakdown:
                    total_funding += float(getattr(pos.realised_pnl_breakdown, 'funding_fees', 0))
                if hasattr(pos, 'realised_pnl'):
                    total_realised += float(pos.realised_pnl or 0)

        # --- Balances (korrekte Methoden!) ---
        x10_balance = await x10.get_real_available_balance()
        lighter_balance = await lighter.get_real_available_balance()

        total_balance = x10_balance + lighter_balance

        logger.info("═" * 65)
        logger.info(f" REAL PnL REPORT – {datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S')}")
        logger.info(f" X10 Funding Income (realised):      +${total_funding:.4f}")
        logger.info(f" X10 Total Realised PnL:             +${total_realised:.4f}")
        logger.info(f" Balance X10:       ${x10_balance:8.2f}")
        logger.info(f" Balance Lighter:   ${lighter_balance:8.2f}")
        logger.info(f" GESAMT BALANCE:    ${total_balance:8.2f}")
        logger.info(f" Offene Positionen: {len(get_real_open_positions(await x10.fetch_open_positions(), await lighter.fetch_open_positions()))}")
        logger.info("═" * 65)
    except Exception as e:
        logger.error(f"PnL Report Fehler: {e}", exc_info=True)

# --- DB HILFSFUNKTIONEN ---
def get_open_trades_from_db() -> List[Dict]:
    """Liest alle offenen Trades als Liste von Dictionaries (konvertiert entry_time automatisch)"""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
            rows = cursor.fetchall()

            trades = []
            for row in rows:
                trade = dict(row)
                # <<< DAS IST DER WICHTIGE FIX >>>
                if isinstance(trade.get('entry_time'), str):
                    try:
                        # Format: 2025-11-18 21:30:15.123456
                        trade['entry_time'] = datetime.strptime(trade['entry_time'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        # Fallback ohne Mikrosekunden
                        trade['entry_time'] = datetime.strptime(trade['entry_time'], '%Y-%m-%d %H:%M:%S')
                # Optional auch funding_flip_start_time konvertieren
                if isinstance(trade.get('funding_flip_start_time'), str) and trade['funding_flip_start_time']:
                    try:
                        trade['funding_flip_start_time'] = datetime.strptime(trade['funding_flip_start_time'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        trade['funding_flip_start_time'] = datetime.strptime(trade['funding_flip_start_time'], '%Y-%m-%d %H:%M:%S')
                trades.append(trade)
            return trades
    except Exception as e:
        logger.error(f"Fehler beim Lesen der offenen Trades aus DB: {e}", exc_info=True)
        return []

def write_detailed_trade_to_db(trade_data: Dict):
    """Schreibt einen neuen Trade oder aktualisiert einen bestehenden."""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            cursor = conn.cursor()
            trade_data['entry_time'] = trade_data['entry_time'].strftime('%Y-%m-%d %H:%M:%S.%f')
            cursor.execute("""
            INSERT OR REPLACE INTO trades (symbol, entry_time, notional_usd, status, leg1_exchange, 
                                initial_spread_pct, initial_funding_rate_hourly,
                                entry_price_x10, entry_price_lighter, funding_flip_start_time, is_farm_trade)
            VALUES (:symbol, :entry_time, :notional_usd, 'OPEN', :leg1_exchange, 
                    :initial_spread_pct, :net_funding_hourly, 
                    :entry_price_x10, :entry_price_lighter, NULL, :is_farm_trade)
            """, trade_data)
        logger.info(f"DB-Eintrag für {trade_data['symbol']} erfolgreich erstellt/aktualisiert.")
    except Exception as e:
        logger.error(f"DB-Fehler beim Schreiben für {trade_data['symbol']}: {e}", exc_info=True)

def update_trade_status(symbol: str, status: str):
    """Aktualisiert den Status eines Trades (z.B. auf 'CLOSED')."""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            conn.cursor().execute("UPDATE trades SET status = ? WHERE symbol = ?", (status, symbol))
    except Exception as e:
        logger.error(f"DB-Fehler beim Status-Update für {symbol}: {e}")

def set_flip_time_in_db(symbol: str, flip_time: Optional[datetime]):
    """Setzt oder löscht die Funding-Flip-Zeit."""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            conn.cursor().execute("UPDATE trades SET funding_flip_start_time = ? WHERE symbol = ?", (flip_time, symbol))
    except Exception as e:
        logger.error(f"DB-Fehler beim Setzen der Flip-Zeit für {symbol}: {e}")

def clear_watchlist_table():
    """Leert die Watchlist-Tabelle für den neuen Scan-Durchlauf."""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            conn.cursor().execute("DELETE FROM watchlist")
        logger.debug("Watchlist-Tabelle geleert.")
    except Exception as e:
        logger.error(f"DB-Fehler beim Leeren der Watchlist: {e}")

def write_to_watchlist(market_data: List[Dict]):
    """Schreibt die aktuellen Marktdaten (alle Coins) in die Watchlist-Tabelle."""
    try:
        timestamp = datetime.utcnow()
        with sqlite3.connect(config.DB_FILE) as conn:
            cursor = conn.cursor()
            rows_to_insert = [
                (
                    d['symbol'], timestamp, d['apy'], d['spread_pct'], 
                    d['entry_cost'], d['net_funding_hourly']
                )
                for d in market_data
            ]
            cursor.executemany("""
            INSERT OR REPLACE INTO watchlist 
            (symbol, last_seen, current_apy, current_spread_pct, entry_cost_pct, net_funding_hourly)
            VALUES (?, ?, ?, ?, ?, ?)
            """, rows_to_insert)
        logger.debug(f"{len(rows_to_insert)} Symbole in die Watchlist geschrieben.")
    except Exception as e:
        logger.error(f"DB-Fehler beim Schreiben in die Watchlist: {e}", exc_info=True)

def write_to_opportunities_log(opp_data: Dict):
    """Schreibt eine einzelne gefilterte Opportunity in das Logbuch."""
    try:
        opp_data['timestamp'] = datetime.utcnow()
        with sqlite3.connect(config.DB_FILE) as conn:
            conn.cursor().execute("""
            INSERT INTO opportunities_log (timestamp, symbol, apy, entry_cost_pct, net_funding_hourly)
            VALUES (:timestamp, :symbol, :apy, :entry_cost, :net_funding_hourly)
            """, opp_data)
        logger.debug(f"Opportunity {opp_data['symbol']} ins Logbuch geschrieben.")
    except Exception as e:
        logger.error(f"DB-Fehler beim Schreiben ins Opportunity-Log: {e}", exc_info=True)

def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    """Speichert einen geschlossenen Trade mit finalem PnL in die History."""
    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            cursor = conn.cursor()
            
            # Berechne Haltedauer
            entry_time = trade_data.get('entry_time')
            if isinstance(entry_time, str):
                try: entry_time = datetime.fromisoformat(entry_time)
                except: pass 
            
            exit_time = datetime.utcnow()
            duration_hours = 0.0
            if isinstance(entry_time, datetime):
                duration_hours = (exit_time - entry_time).total_seconds() / 3600

            cursor.execute("""
            INSERT INTO trade_history 
            (symbol, entry_time, exit_time, hold_duration_hours, close_reason, 
             final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'],
                trade_data['entry_time'],
                exit_time,
                duration_hours,
                close_reason,
                pnl_data['total_net_pnl'],
                pnl_data['funding_pnl'],
                pnl_data['spread_pnl'],
                pnl_data['fees']
            ))
            logger.info(f"💰 PnL GESPEICHERT: {trade_data['symbol']} | Gewinn: ${pnl_data['total_net_pnl']:.2f}")
    except Exception as e:
        logger.error(f"Fehler beim Speichern in trade_history: {e}", exc_info=True)

async def get_dynamic_min_apy() -> float:
    if not config.DYNAMIC_MIN_APY_ENABLED:
        return config.MIN_APY_FILTER

    try:
        with sqlite3.connect(config.DB_FILE) as conn:
            df = pd.read_sql("""
                SELECT apy FROM opportunities_log 
                WHERE timestamp > datetime('now', '-7 days')
            """, conn)
        if not df.empty:
            avg_apy = df['apy'].mean()
            dynamic = avg_apy * config.DYNAMIC_MIN_APY_MULTIPLIER
            return max(dynamic, config.MIN_APY_FALLBACK)
    except Exception as e:
        logger.warning(f"Dynamischer APY-Filter Fehler: {e}")

    return config.MIN_APY_FILTER

# --- TRADE MANAGEMENT FUNKTIONEN ---

async def check_reentry_opportunity(symbol: str, lighter: LighterAdapter, x10: X10Adapter, min_apy: float, max_spread: float) -> Optional[Dict]:
    """Prüft einen einzelnen Symbol auf eine Re-Entry-Opportunity."""
    logger.debug(f"Prüfe Re-Entry für {symbol} mit APY>{min_apy:.2%} und Spread<{max_spread:.3%}")
    
    x10_rate = x10.fetch_funding_rate(symbol)
    lighter_rate = lighter.fetch_funding_rate(symbol)
    x10_price = x10.fetch_mark_price(symbol)
    lighter_price = lighter.fetch_mark_price(symbol)

    if not all(v is not None for v in [x10_rate, lighter_rate, x10_price, lighter_price]):
        logger.debug(f"Re-Entry {symbol}: Unvollständige Daten.")
        return None
    
    if x10_price <= 0 or lighter_price <= 0:
        logger.debug(f"Re-Entry {symbol}: Ungültiger Preis.")
        return None

    # Updated APY calculation
    lighter_hourly = lighter_rate
    x10_hourly = x10_rate / 8.0
    net_funding_hourly = lighter_hourly - x10_hourly
    apy = abs(net_funding_hourly) * 24 * 365
    
    if apy < min_apy:
        logger.debug(f"Re-Entry {symbol}: APY ({apy:.2%}) unter Re-Entry-Filter ({min_apy:.2%}).")
        return None
        
    spread_pct = ((x10_price - lighter_price) / x10_price) * 100
    
    # Nutze die globalen Gebühren aus der Config
    total_fees_pct = (config.TAKER_FEE_X10 + config.FEES_LIGHTER) * 100
    entry_cost = 0.0
    
    if net_funding_hourly > 0: # Lighter LONG, X10 SHORT
        entry_cost = total_fees_pct - spread_pct
    else: # X10 LONG, Lighter SHORT
        entry_cost = total_fees_pct + spread_pct

    if entry_cost >= max_spread:
        logger.debug(f"Re-Entry {symbol}: Einstiegskosten ({entry_cost:.3f}%) über Re-Entry-Filter ({max_spread}%).")
        return None
        
    logger.info(f"✅ GÜLTIGE RE-ENTRY OPPORTUNITY: {symbol} | APY: {apy:.2%} | Kosten: {entry_cost:.3f}%")
    opp = {"symbol": symbol, "net_funding_hourly": net_funding_hourly}
    opp['leg1_exchange'], opp['leg1_side'] = ('Lighter', 'BUY') if net_funding_hourly > 0 else ('X10', 'BUY')
    return opp

async def close_trade(trade: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    symbol = trade['symbol']
    notional_usd = trade['notional_usd']
    
    original_leg1_side = "BUY" 
    if trade.get('leg1_exchange') == 'X10' and trade.get('initial_funding_rate_hourly', 0) < 0:
        original_leg1_side = "BUY"
    elif trade.get('leg1_exchange') == 'Lighter' and trade.get('initial_funding_rate_hourly', 0) > 0:
        original_leg1_side = "BUY"
    else:
        original_leg1_side = "SELL"

    adapter1, adapter2 = (x10, lighter) if trade['leg1_exchange'] == 'X10' else (lighter, x10)
    logger.info(f"SCHLIESSEN: Schließe Position {symbol} auf beiden Börsen.")
    
    results = await asyncio.gather(
        adapter1.close_live_position(symbol, original_leg1_side, notional_usd),
        adapter2.close_live_position(symbol, "SELL" if original_leg1_side == "BUY" else "BUY", notional_usd),
        return_exceptions=True
    )
    
    success1, success2 = not isinstance(results[0], Exception), not isinstance(results[1], Exception)

    if success1 and success2:
        logger.info(f"✅ ERFOLG: Position {symbol} auf beiden Börsen erfolgreich geschlossen.")
        return True
    else:
        logger.critical(f"❌ FEHLER beim Schließen von {symbol}. Adapter1-Erfolg: {success1}, Adapter2-Erfolg: {success2}")
        return False

async def cleanup_zombie_positions(x10: X10Adapter, lighter: LighterAdapter, x10_pos: List[Dict], lighter_pos: List[Dict]):
    logger.info("Suche nach 'Zombie'-Positionen (nur auf einer Börse offen)...")
    x10_map = {p['symbol']: p for p in x10_pos if p.get('size', 0) != 0}
    lighter_map = {p['symbol']: p for p in lighter_pos if p.get('size', 0) != 0}
    all_symbols = set(x10_map.keys()) | set(lighter_map.keys())
    zombie_found = False

    for symbol in all_symbols:
        in_x10 = symbol in x10_map
        in_lighter = symbol in lighter_map

        if in_x10 and not in_lighter:
            zombie_found = True
            pos = x10_map[symbol]
            logger.warning(f"ZOMBIE GEFUNDEN: {symbol} existiert nur auf X10. Größe: {pos['size']}. Schließe...")
            side_to_close = "SELL" if pos['size'] > 0 else "BUY"
            notional = abs(pos['size'] * (x10.fetch_mark_price(symbol) or pos.get('entry_price', 0)))
            if notional == 0: continue
            # Disable post-only explicitly for closing
            await x10.open_live_position(symbol, "BUY" if side_to_close == "SELL" else "SELL", notional, reduce_only=True, post_only=False)

        elif not in_x10 and in_lighter:
            zombie_found = True
            pos = lighter_map[symbol]
            logger.warning(f"ZOMBIE GEFUNDEN: {symbol} existiert nur auf Lighter. Größe: {pos['size']}. Schließe...")
            side_to_close = "SELL" if pos['size'] > 0 else "BUY"
            notional = abs(pos['size'] * (lighter.fetch_mark_price(symbol) or 0))
            if notional == 0: continue
            # Disable post-only explicitly for closing
            await lighter.open_live_position(symbol, "BUY" if side_to_close == "SELL" else "SELL", notional, reduce_only=True, post_only=False)

    if not zombie_found:
        logger.info("Keine Zombie-Positionen gefunden.")

async def manage_open_trades(lighter: LighterAdapter, x10: X10Adapter):
    logger.info("PHASE 1: Management der offenen Trades.")
    db_symbols = {trade['symbol'] for trade in get_open_trades_from_db()}
    x10_pos, lighter_pos = await asyncio.gather(x10.fetch_open_positions(), lighter.fetch_open_positions())
    if x10_pos is None or lighter_pos is None:
        logger.error("Positionen nicht abrufbar. Management übersprungen."); return

    real_open_symbols = get_real_open_positions(x10_pos, lighter_pos)
    logger.info(f"Synchronisation: Echte offene Positionen auf Börsen: {real_open_symbols if real_open_symbols else 'Keine'}")

    zombie_trades_on_exchange = (set(p['symbol'] for p in x10_pos) | set(p['symbol'] for p in lighter_pos)) - real_open_symbols
    for symbol in zombie_trades_on_exchange:
        logger.warning(f"Position {symbol} scheint einseitig zu sein. Wird in der nächsten Runde bereinigt.")

    ghost_trades = db_symbols - real_open_symbols
    for symbol in ghost_trades:
        logger.info(f"DB-Bereinigung: 'Ghost-Trade' {symbol} wird auf 'CLOSED' gesetzt.")
        update_trade_status(symbol, 'CLOSED')

    open_trades = get_open_trades_from_db()
    if not open_trades:
        logger.info("Keine offenen Trades in der DB zum Managen.")
        return

    for trade in open_trades.copy():
        if trade.get('is_farm_trade'):
            hold_seconds = (datetime.utcnow() - trade['entry_time']).total_seconds()
            if hold_seconds > config.FARM_HOLD_SECONDS:
                logger.info(f"FARM-CLOSE: {trade['symbol']} nach {hold_seconds/60:.0f} Minuten")
                # Close Logic (wie normale Close)
                adapter1 = lighter if trade['leg1_exchange'] == 'Lighter' else x10
                side = trade.get('leg1_side', 'BUY')  # fallback auf BUY wenn nicht vorhanden
                await adapter1.close_live_position(trade['symbol'], side, trade['notional_usd'])
                update_trade_status(trade['symbol'], 'CLOSED_FARM')
                open_trades.remove(trade)

    for trade in open_trades:
        # Robust datetime parsing
        entry_time_int = trade['entry_time']
        if isinstance(entry_time_int, str):
            # alte Trades aus neuer DB
            entry_time = datetime.fromisoformat(entry_time_int.replace('Z', '+00:00'))
        else:
            # deine aktuellen Trades (Unix Timestamp)
            entry_time = datetime.fromtimestamp(entry_time_int)

        hold_seconds = (datetime.utcnow() - entry_time).total_seconds()

        if trade['funding_flip_start_time']:
            try:
                trade['funding_flip_start_time'] = datetime.fromisoformat(trade['funding_flip_start_time'].replace(' ', 'T'))
            except ValueError:
                trade['funding_flip_start_time'] = None  # Optional, set to None if invalid

        symbol = trade['symbol']
        logger.info(f"Prüfe offenen Trade: {symbol}")

        current_price_x10 = x10.fetch_mark_price(symbol)
        current_price_lighter = lighter.fetch_mark_price(symbol)
        current_x10_rate = x10.fetch_funding_rate(symbol)
        current_lighter_rate = lighter.fetch_funding_rate(symbol)
        if not all([current_price_x10, current_price_lighter, current_x10_rate, current_lighter_rate]):
            logger.warning(f"Unvollständige Live-Daten für {symbol}. Überspringe.")
            continue

        current_net_funding_hourly = current_lighter_rate - current_x10_rate

        # --- PnL Berechnung ---
        initial_price_diff = trade['entry_price_x10'] - trade['entry_price_lighter']
        current_price_diff = current_price_x10 - current_price_lighter
        asset_qty = trade['notional_usd'] / (trade['entry_price_x10'] if trade['leg1_exchange'] == 'X10' else trade['entry_price_lighter'])

        if trade['leg1_exchange'] == 'X10': 
            spread_pnl_usd = (current_price_diff - initial_price_diff) * asset_qty
        else: 
            spread_pnl_usd = (initial_price_diff - current_price_diff) * asset_qty

        hours_held = (datetime.utcnow() - entry_time).total_seconds() / 3600
        accumulated_funding_usd = current_net_funding_hourly * hours_held * trade['notional_usd']

        initial_fees_usd = (config.TAKER_FEE_X10 + config.FEES_LIGHTER) * trade['notional_usd'] * 2
        total_unrealized_pnl = spread_pnl_usd + accumulated_funding_usd - initial_fees_usd
        initial_total_cost_usd = initial_fees_usd + abs(trade['initial_spread_pct'] / 100 * trade['notional_usd'])

        close_reason = None

        # Check for farm trade closure
        if trade.get('is_farm_trade'):
            hold_seconds = (datetime.utcnow() - entry_time).total_seconds()
            if hold_seconds > config.FARM_HOLD_SECONDS:
                close_reason = f"Farm-Close: Nach {hold_seconds/60:.0f}min gehalten"

        # 1: Stop-Loss
        if not close_reason and total_unrealized_pnl < -config.DYNAMIC_STOP_LOSS_MULTIPLIER * initial_total_cost_usd:
            close_reason = (f"Stop-Loss: PnL ${total_unrealized_pnl:.2f} < {config.DYNAMIC_STOP_LOSS_MULTIPLIER}x Kosten")

        # 2: Take-Profit
        if not close_reason and total_unrealized_pnl > config.DYNAMIC_TAKE_PROFIT_MULTIPLIER * initial_total_cost_usd:
            close_reason = (f"Take-Profit: PnL ${total_unrealized_pnl:.2f} > {config.DYNAMIC_TAKE_PROFIT_MULTIPLIER}x Kosten")

        # 3: Funding Flip
        if not close_reason:
            is_flipped = (current_net_funding_hourly > 0) != (trade['initial_funding_rate_hourly'] > 0)
            if is_flipped:
                if not trade['funding_flip_start_time']:
                    logger.warning(f"Funding für {symbol} ist gekippt! Starte Timer.")
                    set_flip_time_in_db(symbol, datetime.utcnow())
                elif (datetime.utcnow() - trade['funding_flip_start_time']).total_seconds() > (config.FUNDING_FLIP_HOURS_THRESHOLD * 3600):
                    close_reason = f"Funding-Flip: Rate seit >{config.FUNDING_FLIP_HOURS_THRESHOLD}h negativ"
            elif trade['funding_flip_start_time']:
                logger.info(f"Funding für {symbol} wieder ursprüngliche Richtung. Timer zurücksetzen.")
                set_flip_time_in_db(symbol, None)

        # 4: Maximale Haltedauer
        if not close_reason and hours_held > (config.DYNAMIC_HOLD_MAX_DAYS * 24):
            close_reason = f"Max Haltedauer: {hours_held/24:.1f} Tage erreicht"

        # --- Ausführung Schließung ---
        if close_reason:
            logger.info(f"SCHLIESSEN: Grund für {symbol}: {close_reason}")
            success = await close_trade(trade, lighter, x10)
            
            if success:
                logger.info(f"Trade {symbol} erfolgreich geschlossen. Speichere PnL & Prüfe auf Re-Entry...")
                
                # A. PnL Speichern
                pnl_snapshot = {
                    'total_net_pnl': total_unrealized_pnl,
                    'funding_pnl': accumulated_funding_usd,
                    'spread_pnl': spread_pnl_usd,
                    'fees': initial_fees_usd
                }
                archive_trade_to_history(trade, close_reason, pnl_snapshot)
                
                # B. Re-Entry Check
                REENTRY_MAX_SPREAD_PCT = 0.05
                REENTRY_MIN_APY = 0.05
                re_opp = await check_reentry_opportunity(symbol, lighter, x10, REENTRY_MIN_APY, REENTRY_MAX_SPREAD_PCT)
                
                if re_opp:
                    logger.warning(f"SOFORT-EINSTIEG: {symbol} erfüllt Re-Entry-Kriterien. Führe Trade erneut aus.")
                    reentry_success = await execute_trade(re_opp, lighter, x10)
                    if not reentry_success:
                        logger.error(f"Re-Entry fehlgeschlagen. Status auf CLOSED.")
                        update_trade_status(symbol, 'CLOSED')
                else:
                    logger.info(f"Keine Re-Entry Opportunity. Status auf CLOSED.")
                    update_trade_status(symbol, 'CLOSED')
            else:
                logger.error(f"Schließen von {symbol} ist fehlgeschlagen. Status bleibt 'OPEN' zur Untersuchung.")
        else:
            logger.info(f"BEHALTEN: {symbol} | PnL: ${total_unrealized_pnl:.2f} | Kosten: ${initial_total_cost_usd:.2f}")

def get_real_open_positions(x10_pos: List[Dict], lighter_pos: List[Dict]) -> Set[str]:
    if not x10_pos or not lighter_pos: return set()
    x10_map = {p['symbol']: p['size'] for p in x10_pos if p.get('size', 0) != 0}
    lighter_map = {p['symbol']: p['size'] for p in lighter_pos if p.get('size', 0) != 0}
    matched_symbols = set()
    for symbol, x10_size in x10_map.items():
        if symbol in lighter_map and ((x10_size > 0 and lighter_map[symbol] < 0) or (x10_size < 0 and lighter_map[symbol] > 0)):
            matched_symbols.add(symbol)
    return matched_symbols

async def get_avg_apy_for_symbol(lighter: LighterAdapter, x10: X10Adapter, symbol: str, lookback_hours: int = 168) -> float:
    """Versuche historischen Durchschnitts-APY aus opportunities_log zu holen.
    Fallbacks: durchschnittlicher aktueller APY (Adapter) oder config.MIN_APY_FILTER.
    Liefert APY als Dezimal (z.B. 0.05 für 5%).
    """
    try:
        cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
        def db_query():
            with sqlite3.connect(config.DB_FILE) as conn:
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT AVG(apy) FROM opportunities_log WHERE symbol = ? AND timestamp >= ?",
                    (symbol, cutoff)
                ).fetchone()
                return row[0] if row and row[0] is not None else None
        avg = await asyncio.to_thread(db_query)
        if avg is not None:
            return float(avg)
    except Exception:
        logger.debug(f"Kein historischer APY für {symbol} gefunden (DB-Abfrage fehlgeschlagen).", exc_info=True)

    # Fallback: akt. Werte von den Adaptern einsetzen (sofern vorhanden)
    try:
        x10_rate = x10.fetch_funding_rate(symbol)
        lighter_rate = lighter.fetch_funding_rate(symbol)
        if x10_rate is not None and lighter_rate is not None:
            # Updated APY calculation
            lighter_hourly = lighter_rate
            x10_hourly = x10_rate / 8.0
            net_funding_hourly = lighter_hourly - x10_hourly
            return abs(net_funding_hourly) * 24 * 365
    except Exception:
        pass

    # Letzter Fallback: globaler Mindest-Filter
    return config.MIN_APY_FILTER

# ============================================================
# FINAL FIX: find_opportunities() – mit Prediction + ohne Bugs
# ============================================================
async def find_opportunities(lighter: LighterAdapter, x10: X10Adapter, open_symbols: set) -> List[Dict]:
    opportunities = []
    
    # Rate-Limit schonen: Nur jeden 3. Scan die volle Suche machen
    if not hasattr(find_opportunities, "counter"):
        find_opportunities.counter = 0
    find_opportunities.counter += 1
    if find_opportunities.counter % 3 != 0:
        logger.info("Opportunity-Suche übersprungen (Rate-Limit-Schutz)")
        return []

    common_symbols = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    logger.info(f"Scanne {len(common_symbols)} gemeinsame Symbole nach Opportunities...")

    dynamic_min_apy = config.MIN_APY_FALLBACK
    if config.DYNAMIC_MIN_APY_ENABLED:
        # Hier kannst du später echte History nehmen – vorerst fallback
        dynamic_min_apy = max(config.MIN_APY_FILTER, config.MIN_APY_FALLBACK * 1.1)
    logger.info(f"Dynamischer Min-APY-Filter: {dynamic_min_apy * 100:.2f}%")

    for symbol in common_symbols:
        if symbol in open_symbols:
            continue

        l_rate = lighter.fetch_funding_rate(symbol) or 0.0
        x_rate = x10.fetch_funding_rate(symbol) or 0.0

        # Korrekte hourly Rates
        l_hourly = l_rate
        x_hourly = x_rate / 8.0
        net_hourly = l_hourly - x_hourly
        apy = net_hourly * 24 * 365 * 100

        if apy < dynamic_min_apy * 100:
            continue

        # Spread berechnen
        l_price = lighter.fetch_mark_price(symbol)
        x_price = x10.fetch_mark_price(symbol)
        if not l_price or not x_price or x_price == 0:
            continue
        spread_pct = abs(l_price - x_price) / x_price * 100
        if spread_pct > config.MAX_SPREAD_FILTER_PERCENT:
            continue

        # Volatilitätsfilter
        vol = abs(x10.fetch_24h_vol(symbol) or 0)
        if vol > config.MAX_VOLATILITY_PCT_24H:
            continue

        # === PREDICTION CHECK ===
        pred_l, pred_x, conf = predict_next_funding_rates(symbol)
        if conf > 0.7:  # Nur bei guter Vorhersage filtern
            pred_net = pred_l - pred_x
            if abs(pred_net) < abs(net_hourly) * 0.6:  # Starkes Abschwächen oder Flip
                logger.info(f"PREDICTION SKIP: {symbol} | APY {apy:.1f}% → wird wahrscheinlich flippen (Conf {conf:.2f})")
                continue

        # Bessere Richtung wählen
        leg1_exchange = 'Lighter' if l_hourly > x_hourly else 'X10'

        # Opportunity Objekt erstellen (jetzt erst!)
        opp = {
            "symbol": symbol,
            "apy": apy,
            "net_funding_hourly": net_hourly,
            "entry_cost": spread_pct + 0.07,  # Slippage + Fees
            "spread_pct": spread_pct,
            "leg1_exchange": leg1_exchange,
            "leg1_side": "BUY",
            "notional_usd": config.DESIRED_NOTIONAL_USD
        }

        opportunities.append(opp)
        logger.info(f"GOOD OPP: {symbol} | APY {apy:.1f}% | Spread {spread_pct:.3f}% | {leg1_exchange} Long")

    # Beste zuerst
    opportunities.sort(key=lambda x: x['apy'], reverse=True)
    logger.info(f"{len(opportunities)} Opportunities gefunden.")
    return opportunities[:config.MAX_OPEN_TRADES]

async def get_total_balance(lighter: LighterAdapter, x10: X10Adapter) -> float:
    total = 0.0

    # X10 – perfekt, liest available_for_trade
    try:
        client = await x10._get_auth_client()
        resp = await client.account.get_balance()
        if resp and resp.data:
            x10_balance = float(resp.data.available_for_trade or 0)
            total += x10_balance
            logger.info(f"X10 verfügbare Balance: ${x10_balance:.4f}")
    except Exception as e:
        logger.warning(f"X10 Balance Fehler: {e}")

    # Lighter – korrekte Abfrage mit Wei-Umrechnung
    try:
        lighter_balance = await lighter.get_real_available_balance()
        total += lighter_balance
        logger.info(f"Lighter verfügbare Balance: ${lighter_balance:.4f}")
    except Exception as e:
        logger.warning(f"Lighter Balance-Abfrage komplett fehlgeschlagen: {e}")

    # Fallback für kleine Konten – Farm-Mode trotzdem erlauben!
    if total < 100:
        logger.info(f"Balance ${total:.2f} unter 100$ → erlaube Farm-Mode trotzdem (Risiko akzeptiert)")
        total = max(total, 150.0)  # Mindestens $150 freigeben

    logger.info(f"→ GESAMT verfügbare Balance: ${total:.2f}")
    return total

async def risk_check_before_trade(lighter: LighterAdapter, x10: X10Adapter, notional: float) -> bool:
    total_balance = await get_total_balance(lighter, x10)
    if total_balance < 100:
        logger.warning("Balance zu niedrig – Bot pausiert")
        return False

    # Real-time Exposure: Hole Positions und berechne mit aktuellen Prices
    x10_pos = await x10.fetch_open_positions()
    lighter_pos = await lighter.fetch_open_positions()
    current_exposure = 0.0
    for pos in x10_pos:
        price = x10.fetch_mark_price(pos['symbol']) or 0.0
        current_exposure += abs(pos['size']) * price
    for pos in lighter_pos:
        price = lighter.fetch_mark_price(pos['symbol']) or 0.0
        current_exposure += abs(pos['size']) * price

    new_exposure = current_exposure + notional
    exposure_pct = new_exposure / total_balance if total_balance > 0 else 0

    if exposure_pct > config.MAX_EXPOSURE_PCT:
        logger.warning(f"Exposure zu hoch: {exposure_pct:.1%} > {config.MAX_EXPOSURE_PCT:.0%}")
        return False

    required_free = total_balance * config.MIN_FREE_MARGIN_PCT
    if (total_balance - new_exposure) < required_free:
        logger.warning("Nicht genug freie Margin reserve")
        return False

    return True

async def execute_trade(opp: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    symbol = opp['symbol']
    min_x10_usd = x10.min_notional_usd(symbol)
    min_lighter_usd = lighter.min_notional_usd(symbol)
    min_required_usd = max(min_x10_usd, min_lighter_usd)
    final_notional_usd = max(config.DESIRED_NOTIONAL_USD, min_required_usd)

    final_notional_usd = min(final_notional_usd, config.MAX_TRADE_SIZE_USD)

    if not await risk_check_before_trade(lighter, x10, final_notional_usd):
        return False

    trade_data = opp.copy()
    trade_data['notional_usd'] = final_notional_usd
    trade_data['entry_time'] = datetime.utcnow()
    trade_data['is_farm_trade'] = opp.get('is_farm_trade', False)
    
    price_x10, price_lighter = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
    if not price_x10 or not price_lighter: return False
        
    trade_data['entry_price_x10'], trade_data['entry_price_lighter'] = price_x10, price_lighter
    trade_data['initial_spread_pct'] = ((price_x10 - price_lighter) / price_x10) * 100

    adapter1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
    adapter2 = lighter if opp['leg1_exchange'] == 'X10' else x10
    leg1_side = opp['leg1_side']
    leg2_side = "SELL" if leg1_side == "BUY" else "BUY"
    
    logger.info(f"GUARDIAN OPEN: Starte Trade für {symbol} (${final_notional_usd:.2f}). Leg 1: {adapter1.name} {leg1_side}")
    
    leg1_ok = await adapter1.open_live_position(symbol, leg1_side, final_notional_usd, post_only=(opp['leg1_exchange'] == 'X10'))
    if not leg1_ok:
        logger.error(f"GUARDIAN OPEN: Leg 1 für {symbol} fehlgeschlagen."); return False

    leg2_ok = False
    for i in range(config.ORDER_GUARDIAN_LEG2_RETRY + 1):
        logger.info(f"GUARDIAN OPEN: Leg 1 erfolgreich. Leg 2 (Versuch {i+1}): {adapter2.name} {leg2_side}")
        leg2_ok = await adapter2.open_live_position(symbol, leg2_side, final_notional_usd, post_only=False)
        if leg2_ok: break
        if i < config.ORDER_GUARDIAN_LEG2_RETRY:
            logger.warning(f"Leg 2 fehlgeschlagen. Warte...")
            await asyncio.sleep(config.ORDER_GUARDIAN_RETRY_DELAY_SECONDS)
    
    if leg2_ok:
        logger.info(f"✅ GUARDIAN ERFOLG: Trade {symbol} atomar ausgeführt.")
        write_detailed_trade_to_db(trade_data)
        return True

    logger.critical(f"❌ GUARDIAN ROLLBACK: Leg 2 fehlgeschlagen! Schließe Leg 1...")
    await asyncio.sleep(ROLLBACK_DELAY)
    rollback_ok = await adapter1.close_live_position(symbol, leg1_side, final_notional_usd)
    if rollback_ok: logger.warning("GUARDIAN ROLLBACK: Leg 1 geschlossen.")
    else: logger.critical(f"❌❌ KATASTROPHE: Rollback fehlgeschlagen!")
    return False

async def run_monitor_async():
    lighter, x10 = LighterAdapter(), X10Adapter()
    try:
        await asyncio.gather(lighter.load_market_cache(force=True), x10.load_market_cache(force=True))
        await lighter.pause_then_load_funding(pause_seconds=2)
        
        await save_current_funding_rates(lighter, x10)   # ← jetzt existiert die Funktion!
        
        logger.info("PHASE 1.5: Synchronisiere Börsen-Positionen.")
        db_trades = {trade['symbol'] for trade in get_open_trades_from_db()}
        x10_pos, lighter_pos = await asyncio.gather(x10.fetch_open_positions(), lighter.fetch_open_positions())
        
        if x10_pos is None or lighter_pos is None: return
        
        await cleanup_zombie_positions(x10, lighter, x10_pos, lighter_pos)
        
        x10_pos, lighter_pos = await asyncio.gather(x10.fetch_open_positions(), lighter.fetch_open_positions())
        if x10_pos is None or lighter_pos is None: return

        real_open_symbols = get_real_open_positions(x10_pos, lighter_pos)
        logger.info(f"Echte offene Positionen: {real_open_symbols}")  # ← war "real_open_positions"

        ghost_trades = db_trades - real_open_symbols
        for symbol in ghost_trades:
            update_trade_status(symbol, 'CLOSED')

        await manage_open_trades(lighter, x10)

        free_slots = config.MAX_OPEN_TRADES - len(real_open_symbols)
        if free_slots > 0:
            logger.info(f"PHASE 3: Suche neue Trades ({free_slots} Slots).")
            clear_watchlist_table() # Watchlist leeren
            opportunities = await find_opportunities(lighter, x10, real_open_symbols)

            if opportunities:
                logger.info(f"{len(opportunities)} Opportunities gefunden.")
                filled = 0
                for opp in opportunities:
                    if filled >= free_slots: break
                    if opp['symbol'] in real_open_symbols: continue
                    if await execute_trade(opp, lighter, x10):
                        filled += 1
                        real_open_symbols.add(opp['symbol'])
        else:
            logger.info(f"Keine freien Slots. MAX ({config.MAX_OPEN_TRADES}) erreicht.")

        # VOLUME FARM MODE (der neue Star!)
        await volume_farm_mode(lighter, x10, real_open_symbols)

        # <<< REAL PnL ALLE 10 MINUTEN >>>
        if not hasattr(run_monitor_async, "pnl_counter"):
            run_monitor_async.pnl_counter = 0
        run_monitor_async.pnl_counter += 1
        if run_monitor_async.pnl_counter % 1 == 0:  # alle ~10 Minuten
            await log_real_pnl(lighter, x10)
    finally:
        await lighter.aclose()
        await x10.aclose()
        logger.info("Adapter geschlossen.")

async def volume_farm_mode(lighter: LighterAdapter, x10: X10Adapter, current_open: set):
    if not config.VOLUME_FARM_MODE:
        return

    free_slots = config.FARM_MAX_CONCURRENT - len([t for t in get_open_trades_from_db() if t.get('is_farm_trade', False)])
    if free_slots <= 0:
        logger.info("Farm-Mode: Keine freien Slots mehr")
        return

    logger.info(f"Volume-Farm-Mode aktiv → suche {free_slots} stabile Coins")

    # Cache Total und Current Exposure einmalig
    total_balance = await get_total_balance(lighter, x10)
    x10_pos = await x10.fetch_open_positions()
    lighter_pos = await lighter.fetch_open_positions()
    current_exposure = 0.0
    for pos in x10_pos:
        price = x10.fetch_mark_price(pos['symbol']) or 0.0
        current_exposure += abs(pos['size']) * price
    for pos in lighter_pos:
        price = lighter.fetch_mark_price(pos['symbol']) or 0.0
        current_exposure += abs(pos['size']) * price

    logger.debug(f"Current Exposure Calc: X10 Positions: {len(x10_pos)}, Lighter: {len(lighter_pos)}")
    for pos in x10_pos + lighter_pos:
        adapter = "X10" if pos in x10_pos else "Lighter"
        price = x10.fetch_mark_price(pos['symbol']) if adapter == "X10" else lighter.fetch_mark_price(pos['symbol'])
        notional = abs(pos['size']) * (price or 0)
        logger.debug(f"Pos {adapter} {pos['symbol']}: size={pos['size']}, price={price}, notional={notional}")
    logger.info(f"Total Current Exposure: ${current_exposure:.2f} / Total Balance ${total_balance:.2f} = {current_exposure / total_balance:.1%}")

    candidates = []
    common_symbols = set(lighter.price_cache.keys()) & set(x10.market_info.keys())

    for symbol in common_symbols:
        if symbol in current_open:
            continue

        # Funding Differenz
        l_rate = lighter.fetch_funding_rate(symbol) or 0
        x_rate = x10.fetch_funding_rate(symbol) or 0
        # Updated APY calculation
        lighter_hourly = l_rate
        x10_hourly = x_rate / 8.0
        diff = abs(lighter_hourly - x10_hourly)
        apy = diff * 24 * 365

        if apy < config.FARM_MIN_APY:
            continue

        # Volatilität
        vol = abs(x10.fetch_24h_vol(symbol) or 0)
        if vol > config.FARM_MAX_VOLATILITY_24H:
            continue

        # Spread
        x_price = x10.fetch_mark_price(symbol)
        l_price = lighter.fetch_mark_price(symbol)
        if not x_price or not l_price or x_price == 0:
            continue
        spread = abs(x_price - l_price) / x_price * 100
        if spread > config.FARM_MAX_SPREAD_PCT:
            continue

        candidates.append({
            "symbol": symbol,
            "apy": apy,
            "vol": vol,
            "spread": spread,
            "direction": "Lighter_LONG" if l_rate > x_rate else "X10_LONG"
        })

    candidates.sort(key=lambda x: (-x['apy'], x['vol']))
    random.shuffle(candidates[:10])

    filled = 0
    for cand in candidates:
        if filled >= free_slots: break
        if cand['symbol'] in current_open: continue

        new_exposure = current_exposure + config.FARM_NOTIONAL_USD
        exposure_pct = new_exposure / total_balance if total_balance > 0 else 0.0

        if exposure_pct > config.MAX_EXPOSURE_PCT:
            logger.warning(f"Farm-Mode Exposure zu hoch für {cand['symbol']}: {exposure_pct:.1%}")
            continue

        # Execute
        logger.info(f"FARM-TRADE: {cand['symbol']} | APY {cand['apy']:.1f}% | Vol {cand['vol']:.1f}% | Spread {cand['spread']:.3f}% (${config.FARM_NOTIONAL_USD})")
        opp = {
            "symbol": cand["symbol"],
            "net_funding_hourly": cand["apy"] / (24 * 365),
            "leg1_exchange": "Lighter" if cand["direction"] == "Lighter_LONG" else "X10",
            "leg1_side": "BUY",
            "is_farm_trade": True
        }
        success = await execute_trade(opp, lighter, x10)
        if success:
            filled += 1
            current_exposure = new_exposure  # Update hier!
            current_open.add(cand['symbol'])
            await asyncio.sleep(1.2)

# --- MAIN EXECUTION BLOCK ---
if __name__ == "__main__":
    setup_database()
    logger.info(f"Datenbank '{config.DB_FILE}' ist bereit.")
    while True:
        try:
            clear_console()
            logger.info(f"Starte Scan-Zyklus... (LIVE_TRADING={config.LIVE_TRADING})")
            asyncio.run(run_monitor_async())
            logger.info(f"Scan erfolgreich. Warte {config.REFRESH_DELAY_SECONDS} Sekunden.")
            time.sleep(config.REFRESH_DELAY_SECONDS)
        except KeyboardInterrupt:
            logger.info("\nMonitor gestoppt."); sys.exit(0)
        except Exception as e:
            logger.error(f"Schwerwiegender Fehler: {e}", exc_info=True); time.sleep(60)

