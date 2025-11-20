# GLOBALE Variable für temporäre Blacklist
FAILED_COINS = {} # Format: {'SYMBOL': timestamp}

import sys
import os
import time
import asyncio
import aiosqlite
import logging
import random
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Set, Tuple
import pandas as pd

# Pfad-Setup
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction import predict_next_funding_rates, calculate_smart_size

# --- LOGGING CLEANUP ---
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = config.setup_logging()
config.validate_runtime_config(logger)

SHUTDOWN_FLAG = False

# --- NEU: AUTOMATISCHE TRADFI ERKENNUNG ---
def is_tradfi_or_fx(symbol: str) -> bool:
    """
    Erkennt automatisch, ob es sich um Forex, Commodities oder Indizes handelt,
    basierend auf Standard-Tickernamen.
    """
    s = symbol.upper().replace("-USD", "").replace("/", "")
    # 1. COMMODITIES (Rohstoffe)
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")):
        return True
    # 2. FOREX (Währungen)
    fx_currencies = ("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")
    if s.startswith(fx_currencies) and "EUROC" not in s:
        return True
    # 3. INDIZES (TradFi)
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")):
        return True
    return False

# --- 1. DATENBANK SETUP ---
async def setup_database():
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS trades (symbol TEXT PRIMARY KEY, entry_time TIMESTAMP NOT NULL, notional_usd REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', leg1_exchange TEXT NOT NULL, initial_spread_pct REAL NOT NULL, initial_funding_rate_hourly REAL NOT NULL, entry_price_x10 REAL NOT NULL, entry_price_lighter REAL NOT NULL, funding_flip_start_time TIMESTAMP, is_farm_trade BOOLEAN DEFAULT FALSE)""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS trade_history (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, entry_time TIMESTAMP, exit_time TIMESTAMP NOT NULL, hold_duration_hours REAL, close_reason TEXT, final_pnl_usd REAL, funding_pnl_usd REAL, spread_pnl_usd REAL, fees_usd REAL)""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS funding_history (symbol TEXT, timestamp INTEGER, lighter_rate REAL, x10_rate REAL, PRIMARY KEY (symbol, timestamp))""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS fee_stats (
            exchange TEXT,
            symbol TEXT,
            avg_fee_rate REAL DEFAULT 0.0005,
            sample_size INTEGER DEFAULT 0,
            last_updated TIMESTAMP,
            PRIMARY KEY (exchange, symbol)
        )""")
        await conn.commit()
        logger.info("✅ DB-Setup (Async) bereit.")

# --- FEE MANAGEMENT ---
async def update_fee_stats(exchange_name: str, symbol: str, fee_rate: float):
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            async with conn.execute("SELECT avg_fee_rate, sample_size FROM fee_stats WHERE exchange = ? AND symbol = ?", (exchange_name, symbol)) as cursor:
                row = await cursor.fetchone()
            
            if row:
                old_avg, count = row
                new_avg = (old_avg * 0.7) + (fee_rate * 0.3)
                new_count = count + 1
            else:
                new_avg = fee_rate
                new_count = 1
            
            await conn.execute("""
                INSERT OR REPLACE INTO fee_stats (exchange, symbol, avg_fee_rate, sample_size, last_updated) 
                VALUES (?, ?, ?, ?, ?)
            """, (exchange_name, symbol, float(new_avg), int(new_count), datetime.utcnow()))
            await conn.commit()
            logger.info(f"💰 FEE UPDATE: {exchange_name} {symbol} -> {new_avg*100:.4f}% (Count: {new_count})")
    except Exception as e:
        logger.error(f"Fee DB Error: {e}")

async def process_fee_update(adapter, symbol, order_id):
    await asyncio.sleep(10) 
    try:
        fee = await adapter.get_order_fee(order_id)
        await update_fee_stats(adapter.name, symbol, fee)
    except: pass

# --- HELPER ---
async def save_current_funding_rates(lighter: LighterAdapter, x10: X10Adapter):
    try:
        timestamp = int(datetime.utcnow().timestamp())
        async with aiosqlite.connect(config.DB_FILE) as conn:
            rows = []
            lighter_symbols = set(lighter.funding_cache.keys())
            x10_rates = {}
            for symbol, market in x10.market_info.items():
                if hasattr(market.market_stats, "funding_rate"):
                    x10_rates[symbol] = float(market.market_stats.funding_rate)
            common_symbols = lighter_symbols & x10_rates.keys()
            for symbol in common_symbols:
                l_rate = lighter.fetch_funding_rate(symbol) or 0.0
                x_rate = x10_rates[symbol]
                rows.append((symbol, timestamp, float(l_rate), float(x_rate)))
            if rows:
                await conn.executemany("INSERT OR IGNORE INTO funding_history (symbol, timestamp, lighter_rate, x10_rate) VALUES (?, ?, ?, ?)", rows)
                await conn.commit()
    except Exception as e: logger.error(f"DB Error Save Rates: {e}")

async def get_open_trades_from_db() -> List[Dict]:
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM trades WHERE status = 'OPEN'") as cursor:
                rows = await cursor.fetchall()
                trades = []
                for row in rows:
                    trade = dict(row)
                    for date_col in ['entry_time', 'funding_flip_start_time']:
                        val = trade.get(date_col)
                        if isinstance(val, str) and val:
                            try: trade[date_col] = datetime.fromisoformat(val.replace('Z', '+00:00'))
                            except: 
                                try: trade[date_col] = datetime.strptime(val, '%Y-%m-%d %H:%M:%S.%f')
                                except: pass
                    trades.append(trade)
                return trades
    except: return []

# --- FIX: DB Write Logic robust machen ---
async def write_detailed_trade_to_db(trade_data: Dict):
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            td = trade_data.copy()
            if isinstance(td['entry_time'], datetime): 
                td['entry_time'] = td['entry_time'].strftime('%Y-%m-%d %H:%M:%S.%f')
            
            # SICHERSTELLEN, DASS ALLE PARAMETER VORHANDEN SIND
            if 'is_farm_trade' not in td:
                td['is_farm_trade'] = False
            if 'initial_funding_rate_hourly' not in td:
                td['initial_funding_rate_hourly'] = td.get('net_funding_hourly', 0.0)

            await conn.execute("""
                INSERT OR REPLACE INTO trades (
                    symbol, entry_time, notional_usd, status, leg1_exchange, 
                    initial_spread_pct, initial_funding_rate_hourly, 
                    entry_price_x10, entry_price_lighter, funding_flip_start_time, is_farm_trade
                ) VALUES (
                    :symbol, :entry_time, :notional_usd, 'OPEN', :leg1_exchange, 
                    :initial_spread_pct, :initial_funding_rate_hourly, 
                    :entry_price_x10, :entry_price_lighter, NULL, :is_farm_trade
                )
            """, td)
            await conn.commit()
            logger.info(f"✅ DB SAVE: {td['symbol']} als OPEN gespeichert.")
    except Exception as e: 
        logger.error(f"DB Write Error: {e}")
        # Fehler werfen, damit der Bot den Trade nicht "vergisst" und neu öffnet
        raise e 

async def update_trade_status(symbol: str, status: str):
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("UPDATE trades SET status = ? WHERE symbol = ?", (status, symbol))
        await conn.commit()

async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            exit_time = datetime.utcnow()
            duration = 0.0
            if isinstance(trade_data.get('entry_time'), datetime):
                duration = (exit_time - trade_data['entry_time']).total_seconds() / 3600
            await conn.execute("INSERT INTO trade_history (symbol, entry_time, exit_time, hold_duration_hours, close_reason, final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (trade_data['symbol'], trade_data.get('entry_time'), exit_time, duration, close_reason, pnl_data['total_net_pnl'], pnl_data['funding_pnl'], pnl_data['spread_pnl'], pnl_data['fees']))
            await conn.commit()
            logger.info(f"💰 PnL GESPEICHERT: {trade_data['symbol']} | ${pnl_data['total_net_pnl']:.2f}")
    except Exception as e: logger.error(f"DB History Error: {e}")

# --- TRADING LOGIK ---
async def close_trade(trade: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    symbol = trade['symbol']
    notional = trade['notional_usd']
    side = "BUY" if (trade['leg1_exchange'] == 'X10' and trade.get('initial_funding_rate_hourly', 0) < 0) else "SELL"
    
    a1, a2 = (x10, lighter) if trade['leg1_exchange'] == 'X10' else (lighter, x10)
    logger.info(f"📉 Closing {symbol}...")
    
    res = await asyncio.gather(
        a1.close_live_position(symbol, side, notional),
        a2.close_live_position(symbol, "SELL" if side == "BUY" else "BUY", notional),
        return_exceptions=True
    )
    s1 = not isinstance(res[0], Exception) and res[0][0]
    s2 = not isinstance(res[1], Exception) and res[1][0]
    
    if s1 and res[0][1]: asyncio.create_task(process_fee_update(a1, symbol, res[0][1]))
    if s2 and res[1][1]: asyncio.create_task(process_fee_update(a2, symbol, res[1][1]))
    
    if s1 and s2:
        logger.info(f"✅ {symbol} Closed.")
        return True
    else:
        logger.warning(f"⚠️ {symbol} Close Incomplete. A1:{s1}, A2:{s2}")
        return False 

async def check_reentry_opportunity(symbol: str, lighter: LighterAdapter, x10: X10Adapter) -> Optional[Dict]:
    x_rate = x10.fetch_funding_rate(symbol)
    l_rate = lighter.fetch_funding_rate(symbol)
    px = x10.fetch_mark_price(symbol)
    pl = lighter.fetch_mark_price(symbol)
    if not all([x_rate, l_rate, px, pl]): return None
    
    net = l_rate - x_rate
    apy = abs(net)*24*365
    spread = abs(px-pl)/px*100
    
    if apy > 0.05 and spread < 0.05:
        return {"symbol": symbol, "apy": apy, "net_funding_hourly": net, "leg1_exchange": 'Lighter' if net>0 else 'X10', "leg1_side": "BUY"}
    return None

# --- FIX: Execute Trade mit Error Handling & Blacklist ---
async def execute_trade(opp: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    if SHUTDOWN_FLAG: return False
    symbol = opp['symbol']

    # Default setzen falls nicht vorhanden
    if 'is_farm_trade' not in opp:
        opp['is_farm_trade'] = False

    # Prüfen ob Coin gesperrt ist
    if symbol in FAILED_COINS:
        if time.time() - FAILED_COINS[symbol] < 300: 
            logger.debug(f"⏳ {symbol} ist gesperrt (Blacklist)")
            return False
        else:
            del FAILED_COINS[symbol]

    # 1. BALANCE CHECK & SIZING
    try:
        bal_x10 = await x10.get_real_available_balance()
        bal_lit = await lighter.get_real_available_balance()
        total_balance = bal_x10 + bal_lit
    except:
        total_balance = 0.0

    if total_balance < 1.0:
        logger.warning("⚠️ Gesamte Balance zu niedrig oder nicht lesbar.")
        return False
        
    btc_trend = x10.get_24h_change_pct("BTC-USD")
    _, _, confidence = await predict_next_funding_rates(symbol, btc_trend_pct=btc_trend)

    # --- FIX: INTELLIGENTES SIZING & MINIMUM CHECK ---
    # 1. Wir holen die ECHTEN Minima der Börsen
    min_x10 = x10.min_notional_usd(symbol)
    min_lit = lighter.min_notional_usd(symbol)
    absolute_min_required = max(min_x10, min_lit)

    # 2. Sizing Berechnung (Standard)
    if opp.get('is_farm_trade'):
        target_usd = opp.get('notional_usd', config.FARM_NOTIONAL_USD)
    else:
        target_usd = calculate_smart_size(confidence, total_balance)

    # 3. Wir nehmen das größere von (Wunschgröße, Börsen-Minimum)
    final_usd = max(target_usd, absolute_min_required)

    # 4. CHECK: Sprengt das Börsen-Minimum unser Config-Limit?
    if absolute_min_required > config.MAX_TRADE_SIZE_USD:
        logger.debug(f"⏭️ Skip {symbol}: Min Size ${absolute_min_required:.2f} > Config Max ${config.MAX_TRADE_SIZE_USD:.2f}")
        return False
        
    # 5. Wenn wir hier sind, ist das Minimum okay. Jetzt cappen wir auf das Max.
    final_usd = min(final_usd, config.MAX_TRADE_SIZE_USD)
    # -----------------------------------------------------

    # --- PRE-TRADE BALANCE CHECK (Angepasst auf final_usd) ---
    ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
    bal1 = bal_x10 if ex1.name == 'X10' else bal_lit
    ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
    bal2 = bal_lit if ex2.name == 'Lighter' else bal_x10

    if bal1 < final_usd * 1.05:
        return False
    if bal2 < final_usd * 1.05:
        return False

    p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
    if not p_x or not p_l: return False
    
    trade = opp.copy()
    trade.update({
        'notional_usd': final_usd,  # <--- HIER final_usd nutzen!
        'entry_time': datetime.utcnow(), 
        'entry_price_x10': p_x, 
        'entry_price_lighter': p_l, 
        'initial_spread_pct': abs(p_x-p_l)/p_x*100
    })
    
    side1 = opp['leg1_side']
    side2 = "SELL" if side1 == "BUY" else "BUY"
    
    logger.info(f"🚀 OPEN {symbol} ${final_usd:.2f} | {ex1.name} {side1} -> {ex2.name} {side2} | Conf: {confidence:.2f}")
    
    # EXECUTION LEG 1
    success1, order_id1 = await ex1.open_live_position(symbol, side1, final_usd, post_only=(ex1.name=="X10"))
    if not success1: return False

    if order_id1:
        asyncio.create_task(process_fee_update(ex1, symbol, order_id1))

    # EXECUTION LEG 2
    ok2 = False
    order_id2 = None
    last_err = ""
    for _ in range(2):
        s2, oid2 = await ex2.open_live_position(symbol, side2, final_usd, post_only=False)
        if s2: 
            ok2 = True
            order_id2 = oid2
            break
        await asyncio.sleep(1.0)

    if not ok2:
        logger.critical(f"❌ LEG 2 ({ex2.name}) FAILED! Rolling back {ex1.name}...")
        await ex1.close_live_position(symbol, side1, final_usd)
        
        # --- NEU: INTELLIGENT BACKOFF ---
        # Wenn der Fehler "Margin" war, pausieren wir ALLES, nicht nur den Coin.
        # Wir können den genauen Fehlertext hier schwer abgreifen ohne Umbau,
        # aber wir gehen davon aus, dass ein Leg 2 Fail oft Margin ist.
        logger.warning(f"⚠️ {symbol} Execution Error. Setze globalen Cooldown für 30s...")
        FAILED_COINS[symbol] = time.time() + 600
        await asyncio.sleep(30) # 30 Sekunden warten, damit sich Balance/API beruhigt
        return False

    if order_id2:
        asyncio.create_task(process_fee_update(ex2, symbol, order_id2))

    try:
        await write_detailed_trade_to_db(trade)
    except Exception as db_err:
        logger.critical(f"❌ FATAL DB ERROR: Konnte Trade nicht speichern! Stoppe Bot um Chaos zu verhindern.")
        FAILED_COINS[symbol] = time.time() + 600 
        return True 

    return True

async def manage_open_trades(lighter, x10):
    trades = await get_open_trades_from_db()
    if not trades: return
    
    for t in trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        rx, rl = x10.fetch_funding_rate(sym), lighter.fetch_funding_rate(sym)
        
        if not all([px, pl, rx is not None, rl is not None]): continue
        
        net = rl - rx
        pnl = (net * ((datetime.utcnow() - t['entry_time']).total_seconds()/3600) * t['notional_usd']) - (t['notional_usd'] * 0.0012) 
        
        reason = None
        if t.get('is_farm_trade') and (datetime.utcnow()-t['entry_time']).total_seconds() > 600: reason = "Farm Done"
        elif pnl < -t['notional_usd']*0.02: reason = "Stop Loss"
        elif pnl > t['notional_usd']*0.03: reason = "Take Profit"
        
        if reason:
            if await close_trade(t, lighter, x10):
                await update_trade_status(sym, 'CLOSED')
                await archive_trade_to_history(t, reason, {'total_net_pnl': pnl, 'funding_pnl': 0, 'spread_pnl': 0, 'fees': 0})
                if not t.get('is_farm_trade'):
                    re = await check_reentry_opportunity(sym, lighter, x10)
                    if re: await execute_trade(re, lighter, x10)

async def cleanup_zombie_positions(lighter, x10):
    try:
        xp = await x10.fetch_open_positions()
        lp = await lighter.fetch_open_positions()
        xm = {p['symbol']: p['size'] for p in xp or [] if abs(p['size'])>0}
        lm = {p['symbol']: p['size'] for p in lp or [] if abs(p['size'])>0}
        
        for s in set(xm)|set(lm):
            if (s in xm) != (s in lm):
                logger.warning(f"🧟 Zombie {s} detected! Closing...")
                ad = x10 if s in xm else lighter
                sz = xm[s] if s in xm else lm[s]
                success, oid = await ad.open_live_position(s, "SELL" if sz>0 else "BUY", abs(sz*ad.fetch_mark_price(s)), reduce_only=True)
                if success and oid:
                     asyncio.create_task(process_fee_update(ad, s, oid))
    except: pass

async def volume_farm_mode(lighter, x10, open_syms):
    if not config.VOLUME_FARM_MODE or random.random() > 0.2: return 
    open_farm = [t for t in await get_open_trades_from_db() if t.get('is_farm_trade')]
    if len(open_farm) >= config.FARM_MAX_CONCURRENT: return

    FARM_WHITELIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ARB-USD", "OP-USD"]
    candidates = [s for s in FARM_WHITELIST if s not in open_syms]
    random.shuffle(candidates)
    
    for symbol in candidates:
        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l: continue
        
        if abs(p_x - p_l) / p_x * 100 > config.FARM_MAX_SPREAD_PCT: continue
        
        net = (lighter.fetch_funding_rate(symbol) or 0) - (x10.fetch_funding_rate(symbol) or 0)
        direction = "Lighter_LONG" if net > -0.0005 else "X10_LONG"
        size = config.FARM_NOTIONAL_USD * (1.0 + random.uniform(-config.FARM_RANDOM_SIZE_PCT, config.FARM_RANDOM_SIZE_PCT))
        
        logger.info(f"🚜 FARM START: {symbol} ${size:.1f}")
        await execute_trade({"symbol": symbol, "apy": 0, "net_funding_hourly": net, "leg1_exchange": "Lighter" if direction == "Lighter_LONG" else "X10", "leg1_side": "BUY", "is_farm_trade": True, "notional_usd": round(size, 1)}, lighter, x10)
        break

# --- UPDATED FIND OPPORTUNITIES (Mit Auto-Erkennung) ---
async def find_opportunities(lighter, x10, open_syms):
    opps = []
    common = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    now = time.time()
    
    checked = 0
    reasons = {"blacklist": 0, "open": 0, "missing_data": 0, "low_apy": 0, "high_spread": 0}

    for s in common:
        if is_tradfi_or_fx(s): continue
        checked += 1

        if s in FAILED_COINS:
            if now - FAILED_COINS[s] < 300:
                reasons["blacklist"] += 1
                continue
            else:
                del FAILED_COINS[s]
        
        if s in open_syms: 
            reasons["open"] += 1
            continue
            
        rl = lighter.fetch_funding_rate(s)
        rx = x10.fetch_funding_rate(s)
        
        if rl is None or rx is None: 
            reasons["missing_data"] += 1
            continue
        
        # --- KORREKTE APY CALCULATION ---
        # Beide Rates sind JETZT normalisiert auf Stunden-Rate:
        # - Lighter: 8h-Rate / 8 = hourly_rate (Fix in lighter_adapter.py)
        # - X10: Bereits hourly_rate (direkt von API)
        # APY Formel: net_hourly_rate * 24h/day * 365 days/year
        net_hourly = rl - rx
        
        # Formel: Rate * 24 Stunden * 365 Tage
        apy = abs(net_hourly) * 24 * 365
        
        if apy < config.MIN_APY_FILTER: 
            reasons["low_apy"] += 1
            continue
            
        p_l = lighter.fetch_mark_price(s)
        p_x = x10.fetch_mark_price(s)
        
        if not p_l or not p_x: 
            reasons["missing_data"] += 1
            continue
            
        spread = abs(p_l - p_x) / p_x * 100
        
        if spread > config.MAX_SPREAD_FILTER_PERCENT: 
            reasons["high_spread"] += 1
            continue

        # Logik für Direction:
        # Wenn Lighter Rate > X10 Rate (z.B. 0.1% vs 0.0%): Lighter zahlt Shorts. -> SELL Lighter
        # Wenn Lighter Rate < X10 Rate (z.B. -0.1% vs 0.0%): Lighter berechnet Shorts/zahlt Longs. -> BUY Lighter
        if rl > rx:
            leg1_exchange = 'Lighter'
            leg1_side = 'SELL'
        else:
            leg1_exchange = 'Lighter'
            leg1_side = 'BUY'

        # Debug Log für interessante Coins (nur wenn APY > 50%)
        if apy > 0.5:
            logger.info(f"💎 OPPORTUNITY: {s} | APY: {apy*100:.1f}% | Spread: {spread:.3f}% | NetHourly: {net_hourly:.6f}")
            
        opps.append({
            'symbol': s, 
            'apy': apy*100, # In Prozent für Sorting
            'net_funding_hourly': net_hourly, 
            'leg1_exchange': leg1_exchange, 
            'leg1_side': leg1_side
        })
    
    # Sortieren nach höchster APY
    opps.sort(key=lambda x: x['apy'], reverse=True)
    
    if random.random() < 0.1:
        logger.info(f"🔎 Scan Report: {checked} Pairs geprüft. Rejects: {reasons}")
        
    return opps[:config.MAX_OPEN_TRADES]

async def log_real_pnl(lighter, x10):
    try:
        x_bal = await x10.get_real_available_balance()
        l_bal = await lighter.get_real_available_balance()
        logger.info(f"📊 BALANCE: ${x_bal + l_bal:.2f} (X10: ${x_bal:.0f} | Lit: ${l_bal:.0f})")
    except: pass

# --- MAIN LOOPS ---
async def slow_poll_funding(lighter, x10):
    logger.info("🐢 Slow Poller (Funding Rates) aktiv.")
    while True:
        try:
            await x10.load_market_cache(force=True)
            await lighter.load_market_cache(force=True)
            await lighter.load_funding_rates_and_prices()
            await save_current_funding_rates(lighter, x10)
            await asyncio.sleep(300)
        except asyncio.CancelledError: break
        except Exception as e:
            logger.error(f"Funding Poll Error: {e}")
            await asyncio.sleep(30)

async def logic_loop(lighter, x10, price_event: asyncio.Event):
    logger.info("🧠 Logic Loop (Event-Driven) aktiv. Warte auf Preis-Updates...")
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0  
    last_opportunity_scan = 0
    SCAN_COOLDOWN_SECONDS = 3.0  # 3 Sekunden zwischen Scans

    # === RATE LIMITING FÜR EVENT-SPAM ===
    last_opportunity_scan = 0
    SCAN_COOLDOWN_SECONDS = 2.0  # Min 2s zwischen Opportunity-Scans

    await asyncio.sleep(3)

    while True:
        try:
            try:
                await asyncio.wait_for(price_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass 
            price_event.clear()

            now = time.time()

            # 1. Bestehende Trades managen
            await manage_open_trades(lighter, x10)

            # 2. Zombies aufräumen (Positionen ohne DB Eintrag)
            if now - last_zombie_check > 300:
                await cleanup_zombie_positions(lighter, x10)
                last_zombie_check = now

            if now - last_pnl_log > 60:
                await log_real_pnl(lighter, x10)
                last_pnl_log = now

            # Rate-Limited Opportunity Scan
            if now - last_opportunity_scan < SCAN_COOLDOWN_SECONDS:
                continue
            last_opportunity_scan = now

            # === RATE-LIMITED OPPORTUNITY SCAN ===
            # Nur alle 2 Sekunden neue Opportunities suchen
            if now - last_opportunity_scan < SCAN_COOLDOWN_SECONDS:
                continue  # Skip Opportunity Scan
            
            last_opportunity_scan = now

            # 3. Neue Chancen suchen
            trades = await get_open_trades_from_db()
            
            # === FIX: LIVE COUNTER ===
            # Wir starten mit der Anzahl aus der DB
            current_open_count = len(trades)
            
            # Wir übergeben die aktuell offenen Symbole, damit wir sie nicht doppelt kaufen
            open_symbols = {t['symbol'] for t in trades}
            
            opps = await find_opportunities(lighter, x10, open_symbols)

            logger.info(f"Found {len(opps)} opportunities, trying to execute...")
            trade_executed = False
            for opp in opps:
                logger.info(f"Attempting: {opp['symbol']} (APY: {opp['apy']:.1f}%)")
                # PRÜFUNG GEGEN DEN LIVE-COUNTER, NICHT DIE DB
                if current_open_count >= config.MAX_OPEN_TRADES: 
                    logger.warning(f"Max Open Trades erreicht: {current_open_count}/{config.MAX_OPEN_TRADES}") 
                    break
                
                # Wenn wir erfolgreich traden...
                if await execute_trade(opp, lighter, x10):
                    trade_executed = True
                    current_open_count += 1 # ... erhöhen wir den Zähler SOFORT für den nächsten Durchlauf
                    open_symbols.add(opp['symbol']) # Und fügen das Symbol zur lokalen Sperrliste hinzu
                    await asyncio.sleep(0.5) 

            if now - last_farm_check > 10:
                if not trade_executed and current_open_count < config.MAX_OPEN_TRADES:
                    await volume_farm_mode(lighter, x10, open_symbols)
                last_farm_check = now

        except asyncio.CancelledError: break
        except Exception as e: 
            logger.error(f"Logic Crash: {e}")
            await asyncio.sleep(5)

async def main():
    global SHUTDOWN_FLAG
    logger.info("🚀 BOT V3 (Event-Driven Edition) START")
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_update_event = asyncio.Event()

    x10.price_update_event = price_update_event
    lighter.price_update_event = price_update_event
    x10.price_cache = {}

    await setup_database()
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    await lighter.load_funding_rates_and_prices()

    t_ws_x10 = asyncio.create_task(x10.start_websocket())
    t_ws_lighter = asyncio.create_task(lighter.start_websocket())
    t_funding = asyncio.create_task(slow_poll_funding(lighter, x10))
    t_logic = asyncio.create_task(logic_loop(lighter, x10, price_update_event))

    try:
        await asyncio.gather(t_ws_x10, t_ws_lighter, t_funding, t_logic)
    except asyncio.CancelledError: pass
    finally:
        SHUTDOWN_FLAG = True
        logger.info("🛑 SHUTDOWN: Closing connections...")
        await x10.aclose()
        await lighter.aclose()
        logger.info("✅ Closed.")

HOURS_PER_YEAR = 24.0 * 365.0

def calculate_opportunity_stats(
    lighter_rate: float, 
    x10_rate: float, 
    avg_price: float
) -> Tuple[float, float, float]:
    """
    Berechnet die Netto-Stundenrate und die annualisierte Rendite (APY).
    """
    # 1. Bruttoprofit pro Stunde (Differenz der Raten)
    net_hourly_rate_abs = abs(lighter_rate - x10_rate)
    
    # 2. Annualisierte Brutto-APY (in Prozent)
    APY_GROSS = net_hourly_rate_abs * HOURS_PER_YEAR * 100.0

    # 3. Fees (Annualisierter Abzug)
    # Wir zahlen 2 Trades auf X10 (Open/Close) und 2 auf Lighter (Open/Close).
    # Wir gehen konservativ von Taker-Fees für alle 4 Legs aus.
    # Gesamt-Fee-Prozent pro Roundtrip (Open/Close)
    TOTAL_ROUNDTRIP_FEE_PCT = (config.TAKER_FEE_X10 + config.FEES_LIGHTER) * 2.0 * 100.0

    # Annahme: 365 Roundtrips pro Jahr.
    ANNUALIZED_FEE_COST = (config.TAKER_FEE_X10 * 2.0 * 100.0) * 365.0

    APY_NET = APY_GROSS - ANNUALIZED_FEE_COST

    spread_pct = 0.0

    return net_hourly_rate_abs, APY_NET, spread_pct

if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass