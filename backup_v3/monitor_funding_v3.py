import sys
import os
import time
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional  # hinzugefügt

# --- Pfad-Setup ---
# (Stellt sicher, dass 'config' und 'src' gefunden werden, wenn monitor_funding.py in einem Unterordner liegt)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter

# --- Logger einrichten ---\
logger = config.setup_logging()
config.validate_runtime_config(logger)

# --- KONFIGURATION (aus config.py) ---\
POSITION_SIZE_USD = config.POSITION_SIZE_USD
MAX_OPEN_TRADES = config.MAX_OPEN_TRADES
MIN_DAILY_PROFIT_FILTER = config.MIN_DAILY_PROFIT_FILTER
MAX_SPREAD_FILTER_PERCENT = config.MAX_SPREAD_FILTER_PERCENT
TAKE_PROFIT_USD = config.TAKE_PROFIT_USD
STOP_LOSS_USD = config.STOP_LOSS_USD
DB_FILE = config.DB_FILE
TOTAL_FEES_PERCENT = (config.MAKER_FEE_X10 + config.FEES_LIGHTER) * 100
LIVE_TRADING = config.LIVE_TRADING

# --- Datenbank-Funktionen ---\
def setup_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Tabelle 'trades' (umbenannt von opportunities für Klarheit)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        symbol TEXT NOT NULL,
        notional_usd REAL NOT NULL,
        status TEXT DEFAULT 'OPEN',
        open_spread_pct REAL,
        open_funding_annual_pct REAL,
        open_prices TEXT,
        close_timestamp DATETIME,
        close_pnl_usd REAL,
        close_reason TEXT
    )
    """)
    # Tabelle für reine Funding-Daten (optional)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS funding_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        symbol TEXT NOT NULL,
        x10_rate REAL,
        lighter_rate REAL,
        spread_pct REAL
    )
    """)
    conn.commit()
    conn.close()

def save_funding_data(opportunities, conn):
    cursor = conn.cursor()
    now = datetime.now()
    data = [
        (now, opp['symbol'], opp['x10_rate_hourly'], opp['lighter_rate_hourly'], opp['spread_pct'])
        for opp in opportunities
    ]
    cursor.executemany(
        "INSERT INTO funding_data (timestamp, symbol, x10_rate, lighter_rate, spread_pct) VALUES (?, ?, ?, ?, ?)",
        data
    )

def open_paper_trade(opportunity, prices_str, conn):
    logger.info(f"PAPER-TRADE: Eröffne {opportunity['symbol']}...")
    # (Logik für Papiertrades - Hier nur DB-Eintrag)
    open_live_trade(opportunity, prices_str, conn)

def open_live_trade(opportunity, prices_str, conn):
    logger.info(f"LIVE-TRADE: Schreibe Trade für {opportunity['symbol']} in DB...")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO trades (symbol, notional_usd, open_spread_pct, open_funding_annual_pct, open_prices, status)
        VALUES (?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            opportunity['symbol'],
            opportunity['notional_usd'],
            opportunity['spread_pct'],
            opportunity['funding_annual_pct'],
            prices_str
        )
    )

def close_trade_in_db(conn, symbol: str, pnl: float, reason: str):
    logger.info(f"DB-UPDATE: Schließe Trade für {symbol}. Grund: {reason}, PnL: ${pnl:.4f}")
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE trades 
        SET status = 'CLOSED', 
            close_pnl_usd = ?, 
            close_reason = ?, 
            close_timestamp = ?
        WHERE symbol = ? AND status = 'OPEN'
        """,
        (pnl, reason, datetime.now(timezone.utc), symbol)
    )
    conn.commit()

def get_notional_from_db(conn, symbol: str) -> Optional[float]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT notional_usd FROM trades WHERE symbol = ? AND status = 'OPEN' ORDER BY timestamp DESC LIMIT 1",
        (symbol,)
    )
    result = cursor.fetchone()
    return float(result[0]) if result else None

# NEU: Synchronisation DB <-> Börsenpositionen
async def synchronize_db_with_exchanges(conn, lighter_adapter: LighterAdapter, x10_adapter: X10Adapter):
    """
    Gleicht lokale DB mit echten Börsenpositionen ab und schließt Geister-Trades.
    """
    logging.info("Synchronisiere lokale DB mit echten Börsenpositionen...")
    try:
        real_lighter_pos, real_x10_pos = await asyncio.gather(
            lighter_adapter.fetch_open_positions(),
            x10_adapter.fetch_open_positions()
        )
    except Exception as e:
        logging.error(f"Fehler beim Abrufen der echten Positionen: {e}")
        real_lighter_pos, real_x10_pos = [], []

    real_open_symbols = {p['symbol'] for p in real_lighter_pos} | {p['symbol'] for p in real_x10_pos}

    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM trades WHERE status = 'OPEN'")
    db_open = cursor.fetchall()

    for (symbol,) in db_open:
        if symbol not in real_open_symbols:
            logging.warning(f"Geister-Trade gefunden: {symbol}. In DB 'OPEN', aber nicht auf Börsen. Schließe in DB.")
            close_trade_in_db(conn, symbol, 0.0, "GHOST_CLEANUP")

    conn.commit()

# --- PnL BERECHNUNG ---

def get_matched_delta_neutral_positions(
    x10_positions: list[dict], 
    lighter_positions: list[dict], 
    lighter_adapter_prices: dict
) -> list[dict]:
    
    matched = []
    x10_map = {p['symbol']: p for p in x10_positions}
    
    for l_pos in lighter_positions:
        symbol = l_pos['symbol']
        if symbol in x10_map:
            x_pos = x10_map[symbol]
            
            # Prüfe auf Delta-Neutralität (Größen müssen entgegengesetzt sein)
            if (l_pos['size'] > 0 and x_pos['size'] < 0) or (l_pos['size'] < 0 and x_pos['size'] > 0):
                
                # Berechne Lighter PnL manuell (da von API nicht geliefert)
                lighter_pnl_usd = Decimal("0.0")
                try:
                    mark_price = lighter_adapter_prices.get(symbol)
                    if mark_price and l_pos['entry_price'] > 0:
                        # PnL = (Aktueller Preis - Einstiegspreis) * Größe
                        pnl = (Decimal(str(mark_price)) - Decimal(str(l_pos['entry_price']))) * Decimal(str(l_pos['size']))
                        lighter_pnl_usd = pnl
                except (InvalidOperation, TypeError):
                    logger.warning(f"PnL-Berechnung für Lighter {symbol} fehlgeschlagen (Preis: {mark_price}).")
                    pass # PnL bleibt 0.0
                
                # Addiere X10 PnL (wird von API geliefert)
                try:
                    total_pnl = float(Decimal(str(x_pos['unrealized_pnl'])) + lighter_pnl_usd)
                except InvalidOperation:
                    total_pnl = 0.0

                matched.append({
                    'symbol': symbol,
                    'total_pnl_usd': total_pnl,
                    'x10_pos': x_pos,
                    'lighter_pos': l_pos
                })
    return matched

# --- HAUPT-PHASEN ---

async def manage_open_positions(conn, lighter_adapter: LighterAdapter, x10_adapter: X10Adapter) -> set:
    """
    PHASE 1: Verwaltet alle offenen Positionen.
    Prüft PnL-Trigger und führt einen Guardian Close (Lighter zuerst) durch.
    Gibt ein Set der Symbole zurück, die nach dieser Phase noch offen sind.
    """
    logger.info("PHASE 1: Verwalte offene Positionen...")
    
    # 1. Positionen von Börsen abrufen
    try:
        x10_positions, lighter_positions = await asyncio.gather(
            x10_adapter.fetch_open_positions(),
            lighter_adapter.fetch_open_positions()
        )
    except Exception as e:
        logger.error(f"Kritischer Fehler beim Abrufen der Positionen: {e}", exc_info=True)
        # Im Fehlerfall geben wir die offenen Symbole aus der DB zurück, um eine Neueröffnung zu blockieren
        return get_open_symbols_from_db(conn)

    logger.info(f"Position-Sync: {len(x10_positions)} X10-Positionen, {len(lighter_positions)} Lighter-Positionen gefunden.")
    
    # 2. Matchen und PnL berechnen
    lighter_prices = lighter_adapter.price_cache 
    matched_positions = get_matched_delta_neutral_positions(x10_positions, lighter_positions, lighter_prices)
    
    open_symbols = set()

    for pos in matched_positions:
        symbol = pos['symbol']
        pnl = pos['total_pnl_usd']
        
        x10_pnl = pos['x10_pos']['unrealized_pnl']
        lighter_pnl = pnl - x10_pnl
        
        logger.info(f"OFFENE POSITION: {symbol} | Gesamt-PnL (geschätzt): ${pnl:.4f} "
                    f"(X10: ${x10_pnl:.4f}, Lighter: ${lighter_pnl:.4f})")
        
        # 3. Trigger-Check
        close_reason = None
        if pnl >= TAKE_PROFIT_USD > 0: # (Sicherstellen, dass TP > 0 ist)
            close_reason = f"TAKE PROFIT (${TAKE_PROFIT_USD})"
        elif pnl <= -STOP_LOSS_USD < 0: # (Sicherstellen, dass SL < 0 ist)
            close_reason = f"STOP LOSS (${-STOP_LOSS_USD})"
        
        if LIVE_TRADING and close_reason:
            logger.warning(f"CLOSE TRIGGER: {symbol} | Grund: {close_reason} | PnL: ${pnl:.4f}")
            
            # 4. Guardian Close Logic (Umgekehrte Reihenfolge)
            
            # Ermittle Schließ-Seiten
            # X10 war Leg 1 (z.B. BUY, size > 0) -> close_side = SELL
            x10_close_side = "SELL" if pos['x10_pos']['size'] > 0 else "BUY"
            # Lighter war Leg 2 (z.B. SELL, size < 0) -> close_side = BUY
            lighter_close_side = "BUY" if pos['lighter_pos']['size'] < 0 else "SELL"
            
            # Hole den ursprünglichen Notional-Wert aus der DB für die Schließung
            db_notional = get_notional_from_db(conn, symbol)
            
            if not db_notional:
                logger.error(f"Konnte {symbol} nicht schließen, da Notional in DB fehlt! Blockiere Neukauf.")
                open_symbols.add(symbol) # Verhindert Neueröffnung
                continue

            logger.info(f"GUARDIAN CLOSE: Starte Schließung für {symbol} (Notional ~${db_notional})...")

            # LEG 2 (Lighter) zuerst schließen
            leg2_closed = await lighter_adapter.close_live_position(symbol, lighter_close_side, db_notional)
            
            if leg2_closed:
                logger.info(f"GUARDIAN CLOSE: Leg 2 (Lighter) für {symbol} erfolgreich geschlossen.")
                
                # LEG 1 (X10) als Rollback/Abschluss schließen
                leg1_closed = await x10_adapter.close_live_position(symbol, x10_close_side, db_notional)
                
                if leg1_closed:
                    logger.info(f"✅ GUARDIAN CLOSE: {symbol} erfolgreich Delta-neutral geschlossen.")
                    close_trade_in_db(conn, symbol, pnl, close_reason)
                else:
                    logger.critical(f"❌ GUARDIAN CLOSE FEHLER: Leg 2 (Lighter) geschlossen, aber Leg 1 (X10) FEHLGESCHLAGEN!")
                    logger.critical(f"Manuelle Intervention für {symbol} auf X10 erforderlich!")
                    open_symbols.add(symbol) # Blockiert Neukauf
            else:
                logger.error(f"❌ GUARDIAN CLOSE FEHLER: Leg 2 (Lighter) konnte für {symbol} nicht geschlossen werden. Breche Schließung ab.")
                open_symbols.add(symbol) # Position bleibt offen
        
        else:
            # Kein Trigger, oder Paper-Trading
            open_symbols.add(symbol)
            
    return open_symbols

def get_open_symbols_from_db(conn) -> set:
    """Holt alle Symbole, die in der DB als 'OPEN' markiert sind."""
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM trades WHERE status = 'OPEN'")
    return {row[0] for row in cursor.fetchall()}

def find_opportunities(lighter_adapter: LighterAdapter, x10_adapter: X10Adapter) -> list[dict]:
    # ... (Restliche Logik bleibt gleich wie im Original-Skript)
    logger.info("Vergleiche Märkte...")
    opportunities = []
    
    common_symbols = set(lighter_adapter.market_info.keys()) & set(x10_adapter.market_info.keys())
    
    for symbol in common_symbols:
        x10_rate = x10_adapter.fetch_funding_rate(symbol)
        lighter_rate = lighter_adapter.fetch_funding_rate(symbol)
        x10_price = x10_adapter.fetch_mark_price(symbol)
        lighter_price = lighter_adapter.fetch_mark_price(symbol)
        
        if all(v is not None for v in [x10_rate, lighter_rate, x10_price, lighter_price]):
            if x10_price == 0 or lighter_price == 0:
                continue

            spread_pct = ((x10_price - lighter_price) / lighter_price) * 100
            
            # Funding-Spread (Stündlich)
            funding_spread = x10_rate - lighter_rate 
            
            # Jährliche Funding-Rate (positiv = gut für X10 Long / Lighter Short)
            funding_annual_pct = (lighter_rate - x10_rate) * 24 * 365 * 100
            
            opp = {
                "symbol": symbol,
                "x10_rate_hourly": x10_rate,
                "lighter_rate_hourly": lighter_rate,
                "funding_spread_hourly": funding_spread,
                "funding_annual_pct": funding_annual_pct,
                "x10_price": x10_price,
                "lighter_price": lighter_price,
                "spread_pct": spread_pct,
            }
            opportunities.append(opp)
            
    logger.info(f"{len(opportunities)} gemeinsame Märkte mit vollen Daten gefunden.")
    return opportunities

def filter_opportunities(opportunities: list[dict], open_symbols: set) -> list[dict]:
    # ... (Restliche Logik bleibt gleich wie im Original-Skript)
    filtered = []
    logger.info(f"Filtere {len(opportunities)} Opportunities (Min Profit: {MIN_DAILY_PROFIT_FILTER*100:.3f}%/Tag)...")

    for opp in opportunities:
        symbol = opp['symbol']
        
        if symbol in open_symbols:
            continue

        daily_profit_pct = (opp['funding_annual_pct'] / 365)
        
        # 1. Profit-Filter
        if daily_profit_pct < (MIN_DAILY_PROFIT_FILTER * 100):
            continue
            
        # 2. Spread-Filter (um zu teuren Einstieg zu vermeiden)
        # (Positiver Spread bedeutet X10 ist teurer als Lighter)
        entry_cost_pct = opp['spread_pct'] + TOTAL_FEES_PERCENT
        
        if entry_cost_pct > MAX_SPREAD_FILTER_PERCENT:
            logger.debug(f"Überspringe {symbol}: Einstiegskosten {entry_cost_pct:.4f}% > Limit {MAX_SPREAD_FILTER_PERCENT}%")
            continue
            
        # 3. Bestimme Handelsrichtung basierend auf Funding
        # Wir wollen Funding erhalten.
        # Szenario 1: Lighter zahlt mehr (lighter_rate > x10_rate) -> Long X10, Short Lighter
        # Szenario 2: X10 zahlt mehr (x10_rate > lighter_rate) -> Long Lighter, Short X10
        
        if opp['funding_annual_pct'] > 0: # Szenario 1
            opp['leg1_exchange'] = 'X10'
            opp['leg1_side'] = 'BUY'
            opp['leg2_exchange'] = 'Lighter'
            opp['leg2_side'] = 'SELL'
        else: # Szenario 2
            opp['leg1_exchange'] = 'Lighter'
            opp['leg1_side'] = 'BUY'
            opp['leg2_exchange'] = 'X10'
            opp['leg2_side'] = 'SELL'
            
        # Notional USD bestimmen
        opp['notional_usd'] = POSITION_SIZE_USD # Später ggf. MinNotional prüfen
            
        filtered.append(opp)
        
    # Sortieren: Bester Profit zuerst
    filtered.sort(key=lambda x: x['funding_annual_pct'], reverse=True)
    return filtered

async def open_trades_atomically(
    opportunities: list[dict], 
    lighter_adapter: LighterAdapter, 
    x10_adapter: X10Adapter, 
    conn,
    open_symbols: set
):
    # ... (Restliche Logik bleibt gleich wie im Original-Skript)
    logger.info(f"{len(opportunities)} profitable Trades gefunden. Prüfe {MAX_OPEN_TRADES} Slots...")
    
    opened_this_scan = 0
    
    for opp in opportunities:
        if len(open_symbols) >= MAX_OPEN_TRADES:
            logger.info("MAX_OPEN_TRADES Limit erreicht. Keine weiteren Eröffnungen.")
            break
            
        if opp['symbol'] in open_symbols:
            logger.debug(f"Symbol {opp['symbol']} ist bereits offen. Überspringe.")
            continue
            
        logger.warning(f"Arbitrage-Signal gefunden für {opp['symbol']}! "
                       f"Erw. Jahres-ROI: {opp['funding_annual_pct']:.4f}% "
                       f"({opp['leg1_exchange']} {opp['leg1_side']} / {opp['leg2_exchange']} {opp['leg2_side']})")
        
        # Bestimme Adapter für Leg 1 und Leg 2
        adapter1 = x10_adapter if opp['leg1_exchange'] == 'X10' else lighter_adapter
        adapter2 = lighter_adapter if opp['leg2_exchange'] == 'Lighter' else x10_adapter

        # (Hier fehlt die MinNotional-Prüfung, wird in Adaptern gehandhabt)
        notional_usd = opp['notional_usd']
        
        opp['trade_prices'] = (
            f"X10: ${opp['x10_price']:.4f}, Lighter: ${opp['lighter_price']:.4f}, "
            f"Spread: {opp['spread_pct']:.4f}%"
        )
        
        if LIVE_TRADING:
            logger.info(f"GUARDIAN: Starte Leg 1 ({opp['leg1_exchange']} {opp['leg1_side']}) mit {notional_usd} USD.")
            leg1_ok = await adapter1.open_live_position(opp['symbol'], opp['leg1_side'], notional_usd)
            
            if leg1_ok:
                logger.info("GUARDIAN: Leg 1 erfolgreich. Starte Leg 2...")
                leg2_ok = await adapter2.open_live_position(opp['symbol'], opp['leg2_side'], notional_usd)
                
                if leg2_ok:
                    logger.info(f"✅ GUARDIAN ERFOLG: Trade {opp['symbol']} atomar ausgeführt.")
                    open_live_trade(opp, opp['trade_prices'], conn)
                    open_symbols.add(opp['symbol'])
                    opened_this_scan += 1
                else:
                    logger.critical(f"❌ GUARDIAN ROLLBACK: Leg 2 ({opp['leg2_exchange']}) fehlgeschlagen! Schließe Leg 1...")
                    # Rollback: Schließe Leg 1
                    rollback_ok = await adapter1.close_live_position(opp['symbol'], opp['leg2_side'], notional_usd)
                    if rollback_ok:
                        logger.warning(f"GUARDIAN ROLLBACK: Leg 1 erfolgreich geschlossen. Position ist sicher.")
                    else:
                        logger.critical(f"❌❌ KATASTROPHE: Rollback von Leg 1 FEHLGESCHLAGEN! Manuelle Intervention erforderlich!")
            else:
                 logger.error(f"GUARDIAN: Leg 1 ({opp['leg1_exchange']}) fehlgeschlagen. Breche Trade ab.")
            
        else: # Paper Trading
            logger.info(f"PAPER-TRADE: {opp['symbol']} (Notional: {notional_usd} USD)")
            open_paper_trade(opp, opp['trade_prices'], conn)
            open_symbols.add(opp['symbol'])
            opened_this_scan += 1


async def run_monitor_async(conn):
    """
    Führt den gesamten Zyklus aus:
    1. Caches laden
    2. Offene Positionen verwalten (Schließen, PnL-Prüfung)
    3. Neue Positionen suchen (Finden, Filtern)
    4. Neue Positionen eröffnen (Guardian Open)
    """
    lighter = LighterAdapter()
    x10 = X10Adapter()
    
    # NEU: Synchronisation gleich zu Zyklusbeginn
    await synchronize_db_with_exchanges(conn, lighter, x10)

    try:
        # Caches parallel laden
        await asyncio.gather(
            lighter.load_market_cache(),
            x10.load_market_cache()
        )
        
        # SCHRITT 1: Offene Positionen verwalten (PnL-Check & Closing)
        # Holt Positionen von APIs, prüft PnL und schließt bei Bedarf.
        # Gibt die Symbole zurück, die NACH der Verwaltung noch offen sind.
        open_symbols_set = await manage_open_positions(conn, lighter, x10)
        
        # SCHRITT 2: Neue Positionen suchen und eröffnen
        logger.info("PHASE 2: Suche nach neuen Arbitrage-Möglichkeiten...")
        
        # 2a. Opportunities finden
        opportunities = find_opportunities(lighter, x10)
        
        if not opportunities:
            logger.info("Keine gemeinsamen Märkte oder Daten gefunden.")
            return

        # 2b. Opportunities filtern (gegen DB, Profit, Spread)
        # Wir übergeben die Liste der (noch) offenen Symbole, um Duplikate zu verhindern
        filtered_opportunities = filter_opportunities(opportunities, open_symbols_set)
        
        if not filtered_opportunities:
            logger.info("Keine profitablen Opportunities nach Filterung gefunden.")
        
        # 2c. Logging der Funding-Daten (auch wenn nicht gehandelt wird)
        save_funding_data(opportunities, conn)

        # 2d. Atomares Eröffnen (wenn Slots frei sind)
        await open_trades_atomically(
            filtered_opportunities, 
            lighter, 
            x10, 
            conn,
            open_symbols_set # (Set wird 'by reference' modifiziert)
        )

    except Exception as e:
        logger.error(f"Fehler in run_monitor_async: {e}", exc_info=True)
    finally:
        await lighter.aclose()
        await x10.aclose()
        logger.info("Adapter geschlossen. Scan-Zyklus beendet.")

# --- HAUPTSCHLEIFE ---\
if __name__ == "__main__":
    setup_database()
    logger.info(f"Datenbank '{DB_FILE}' ist bereit.")
    
    while True:
        try:
            # Stelle sicher, dass LIVE_TRADING-Status bei jedem Lauf aktuell ist
            LIVE_TRADING = getattr(config, "LIVE_TRADING", False)
            logger.info(f"Starte Scan-Zyklus... (LIVE_TRADING={LIVE_TRADING})")
            
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row # Ermöglicht Spaltenzugriff nach Name
            
            asyncio.run(run_monitor_async(conn)) 
            
            conn.commit()
            conn.close()
            
            delay = config.REFRESH_DELAY_SECONDS
            logger.info(f"Scan erfolgreich. Warte {delay} Sekunden ({delay / 60:.1f} Minuten) bis zum nächsten Scan.")
            time.sleep(delay)
            
        except KeyboardInterrupt:
            logger.info("\nMonitor gestoppt. Auf Wiedersehen!")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Ein schwerwiegender Fehler ist in der Hauptschleife aufgetreten: {e}", exc_info=True)
            logger.error("Warte 60 Sekunden und starte neu...")
            time.sleep(60)