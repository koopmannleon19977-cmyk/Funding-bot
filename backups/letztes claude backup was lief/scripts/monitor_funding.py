# monitor_funding.py - TEIL 1
FAILED_COINS = {}
OPPORTUNITY_LOG_CACHE = {}  # ✅ NEU
OPPORTUNITY_LOG_COOLDOWN = 60  # Sekunden

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

############################################################
# EXECUTION LOCK SYSTEM (Race Condition Prevention)
############################################################
EXECUTION_LOCKS: Dict[str, asyncio.Lock] = {}
LOCK_MANAGER_LOCK = asyncio.Lock()  # Meta-Lock für das Dict selbst

async def get_execution_lock(symbol: str) -> asyncio.Lock:
    """
    Thread-safe Lock-Getter für Symbol-Level Execution.
    
    Args:
        symbol: Trading pair (e.g. "BTC-USD")
    
    Returns:
        asyncio.Lock für das spezifische Symbol
    """
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction import predict_next_funding_rates, calculate_smart_size

logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = config.setup_logging()
config.validate_runtime_config(logger)

SHUTDOWN_FLAG = False

def is_tradfi_or_fx(symbol: str) -> bool:
    """Erkennt TradFi/Forex automatisch"""
    s = symbol.upper().replace("-USD", "").replace("/", "")
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")):
        return True
    fx_currencies = ("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")
    if s.startswith(fx_currencies) and "EUROC" not in s:
        return True
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")):
        return True
    return False

async def setup_database():
    """DB-Setup mit allen Tabellen"""
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("""
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
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_time TIMESTAMP,
                exit_time TIMESTAMP NOT NULL,
                hold_duration_hours REAL,
                close_reason TEXT,
                final_pnl_usd REAL,
                funding_pnl_usd REAL,
                spread_pnl_usd REAL,
                fees_usd REAL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS funding_history (
                symbol TEXT,
                timestamp INTEGER,
                lighter_rate REAL,
                x10_rate REAL,
                PRIMARY KEY (symbol, timestamp)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fee_stats (
                exchange TEXT,
                symbol TEXT,
                avg_fee_rate REAL DEFAULT 0.0005,
                sample_size INTEGER DEFAULT 0,
                last_updated TIMESTAMP,
                PRIMARY KEY (exchange, symbol)
            )
        """)
        await conn.commit()
        logger.info("✅ DB-Setup bereit.")

async def update_fee_stats(exchange_name: str, symbol: str, fee_rate: float):
    """Fee-Statistiken mit EMA updaten"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            async with conn.execute(
                "SELECT avg_fee_rate, sample_size FROM fee_stats WHERE exchange = ? AND symbol = ?",
                (exchange_name, symbol)
            ) as cursor:
                row = await cursor.fetchone()
            
            if row:
                old_avg, count = row
                new_avg = (old_avg * 0.7) + (fee_rate * 0.3)
                new_count = count + 1
            else:
                new_avg = fee_rate
                new_count = 1
            
            await conn.execute("""
                INSERT OR REPLACE INTO fee_stats 
                (exchange, symbol, avg_fee_rate, sample_size, last_updated) 
                VALUES (?, ?, ?, ?, ?)
            """, (exchange_name, symbol, float(new_avg), int(new_count), datetime.utcnow()))
            await conn.commit()
            # ✅ IMPROVED LOGGING
            if fee_rate > 0:
                logger.info(
                    f"💰 FEE UPDATE: {exchange_name} {symbol} -> "
                    f"{fee_rate*100:.4f}% (Avg: {new_avg*100:.4f}%, n={new_count})"
                )
            else:
                logger.debug(
                    f"💰 FEE UPDATE: {exchange_name} {symbol} -> "
                    f"0.0000% (Maker/Free) (n={new_count})"
                )
            logger.info(f"💰 FEE UPDATE: {exchange_name} {symbol} -> {new_avg*100:.4f}% (#{new_count})")
    except Exception as e:
        logger.error(f"Fee DB Error: {e}")

async def process_fee_update(adapter, symbol, order_id):
    """
    ✅ OPTIMIZED: Background-Task für Fee-Tracking mit Smart Retry
    
    Flow:
    1. Warte 2s (Order Settlement)
    2. Versuche Fee zu holen
    3. Falls 0% und Order noch pending → Retry nach 5s
    4. Max 2 Retries
    """
    if not order_id:
        return
    
    # Initial delay (Order Settlement)
    await asyncio.sleep(2)
    
    max_retries = 2
    retry_delay = 5  # Sekunden
    
    for attempt in range(max_retries + 1):
        try:
            fee = await adapter.get_order_fee(order_id)
            
            # Erfolg: Fee > 0 oder Order definitiv 0% (Maker)
            if fee > 0 or attempt == max_retries:
                await update_fee_stats(adapter.name, symbol, fee)
                return
            
            # Fee ist 0%, könnte aber noch pending sein
            if attempt < max_retries:
                logger.debug(
                    f"🔄 {adapter.name} {symbol}: Fee noch 0%, "
                    f"Retry {attempt+1}/{max_retries} in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
                continue
            
            # Letzter Versuch: Akzeptiere 0%
            await update_fee_stats(adapter.name, symbol, 0.0)
            return
            
        except Exception as e:
            logger.error(f"❌ Fee Update Error {adapter.name} {symbol}: {e}")
            
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                # Letzter Versuch failed: Nutze config Fallback
                fallback_fee = (
                    config.FEES_LIGHTER if adapter.name == "Lighter" 
                    else config.TAKER_FEE_X10
                )
                await update_fee_stats(adapter.name, symbol, fallback_fee)
                return

async def save_current_funding_rates(lighter: LighterAdapter, x10: X10Adapter):
    """Speichert Funding-Rates für Historik"""
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
                await conn.executemany(
                    "INSERT OR IGNORE INTO funding_history (symbol, timestamp, lighter_rate, x10_rate) VALUES (?, ?, ?, ?)",
                    rows
                )
                await conn.commit()
    except Exception as e:
        logger.error(f"DB Error Save Rates: {e}")

async def get_open_trades_from_db() -> List[Dict]:
    """Lädt offene Trades aus DB"""
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
                            try:
                                trade[date_col] = datetime.fromisoformat(val.replace('Z', '+00:00'))
                            except:
                                try:
                                    trade[date_col] = datetime.strptime(val, '%Y-%m-%d %H:%M:%S.%f')
                                except:
                                    pass
                    trades.append(trade)
                return trades
    except:
        return []

async def write_detailed_trade_to_db(trade_data: Dict):
    """Speichert Trade robust in DB"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            td = trade_data.copy()
            if isinstance(td['entry_time'], datetime):
                td['entry_time'] = td['entry_time'].strftime('%Y-%m-%d %H:%M:%S.%f')
            
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
        raise e

async def update_trade_status(symbol: str, status: str):
    """Update Trade-Status"""
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("UPDATE trades SET status = ? WHERE symbol = ?", (status, symbol))
        await conn.commit()

async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    """Archiviert Trade in History"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            exit_time = datetime.utcnow()
            duration = 0.0
            if isinstance(trade_data.get('entry_time'), datetime):
                duration = (exit_time - trade_data['entry_time']).total_seconds() / 3600
            await conn.execute("""
                INSERT INTO trade_history 
                (symbol, entry_time, exit_time, hold_duration_hours, close_reason, 
                 final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'],
                trade_data.get('entry_time'),
                exit_time,
                duration,
                close_reason,
                pnl_data['total_net_pnl'],
                pnl_data['funding_pnl'],
                pnl_data['spread_pnl'],
                pnl_data['fees']
            ))
            await conn.commit()
            logger.info(f"💰 PnL: {trade_data['symbol']} | ${pnl_data['total_net_pnl']:.2f}")
    except Exception as e:
        logger.error(f"DB History Error: {e}")
# monitor_funding.py - TEIL 2 (Fortsetzung)

async def close_trade(trade: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    """Schließt einen Trade parallel auf beiden Exchanges"""
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
    
    if s1 and res[0][1]:
        asyncio.create_task(process_fee_update(a1, symbol, res[0][1]))
    if s2 and res[1][1]:
        asyncio.create_task(process_fee_update(a2, symbol, res[1][1]))
    
    if s1 and s2:
        logger.info(f"✅ {symbol} Closed.")
        return True
    else:
        logger.warning(f"⚠️ {symbol} Close Incomplete. A1:{s1}, A2:{s2}")
        return False

async def check_reentry_opportunity(symbol: str, lighter: LighterAdapter, x10: X10Adapter) -> Optional[Dict]:
    """Prüft ob Re-Entry möglich ist"""
    x_rate = x10.fetch_funding_rate(symbol)
    l_rate = lighter.fetch_funding_rate(symbol)
    px = x10.fetch_mark_price(symbol)
    pl = lighter.fetch_mark_price(symbol)
    if not all([x_rate, l_rate, px, pl]):
        return None
    
    net = l_rate - x_rate
    apy = abs(net) * 24 * 365
    spread = abs(px - pl) / px * 100
    
    if apy > 0.05 and spread < 0.05:
        return {
            "symbol": symbol,
            "apy": apy,
            "net_funding_hourly": net,
            "leg1_exchange": 'Lighter' if net > 0 else 'X10',
            "leg1_side": "BUY"
        }
    return None

async def execute_trade(opp: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    """
    ✅ PRODUCTION-GRADE EXECUTION mit Execution Lock
    
    Flow:
    1. Acquire Symbol-Lock (verhindert parallele Execution)
    2. Pre-checks (Blacklist, Balance, Sizing)
    3. Execute LEG 1
    4. Execute LEG 2 (mit Retry)
    5. Falls LEG 2 failed → Rollback LEG 1
    6. NUR bei Erfolg → DB Write
    7. Release Lock
    """
    if SHUTDOWN_FLAG:
        return False
    
    symbol = opp['symbol']

    # ===== 🔒 ACQUIRE EXECUTION LOCK =====
    lock = await get_execution_lock(symbol)
    
    # Try to acquire lock (non-blocking check)
    if lock.locked():
        logger.debug(f"⏸️  {symbol} already being executed, skipping duplicate")
        return False
    
    async with lock:  # 🔒 CRITICAL SECTION START
        logger.debug(f"🔒 {symbol} execution lock acquired")
        
        if 'is_farm_trade' not in opp:
            opp['is_farm_trade'] = False

        # ===== 1. BLACKLIST CHECK =====
        if symbol in FAILED_COINS:
            if time.time() - FAILED_COINS[symbol] < 300:
                logger.debug(f"⏳ {symbol} ist gesperrt (Blacklist)")
                return False
            else:
                del FAILED_COINS[symbol]

        # ===== 2. RE-CHECK DB (wichtig nach Lock Acquire!) =====
        # Ein anderer Task könnte den Trade schon opened haben, während wir warteten
        existing_trades = await get_open_trades_from_db()
        if any(t['symbol'] == symbol for t in existing_trades):
            logger.info(f"ℹ️  {symbol} already open (detected after lock acquire)")
            return False

        # ===== 3. BALANCE CHECK & SIZING =====
        try:
            bal_x10 = await x10.get_real_available_balance()
            bal_lit = await lighter.get_real_available_balance()
            total_balance = bal_x10 + bal_lit
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            total_balance = 0.0

        if total_balance < 1.0:
            logger.warning("⚠️ Gesamte Balance zu niedrig.")
            return False
            
        # BTC Trend & Confidence
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        _, _, confidence = await predict_next_funding_rates(symbol, btc_trend_pct=btc_trend)

        # Calculate Min Requirements
        min_x10 = x10.min_notional_usd(symbol)
        min_lit = lighter.min_notional_usd(symbol)
        absolute_min_required = max(min_x10, min_lit)

        # Skip if min too high
        if absolute_min_required > config.MAX_TRADE_SIZE_USD:
            logger.debug(f"⭐ Skip {symbol}: Min ${absolute_min_required:.2f} > Max ${config.MAX_TRADE_SIZE_USD:.2f}")
            return False

        # Calculate Target Size
        if opp.get('is_farm_trade'):
            target_usd = opp.get('notional_usd', config.FARM_NOTIONAL_USD)
        else:
            target_usd = calculate_smart_size(confidence, total_balance)

        final_usd = max(target_usd, absolute_min_required)
        final_usd = min(final_usd, config.MAX_TRADE_SIZE_USD)

        # Determine Exchanges
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        bal1 = bal_x10 if ex1.name == 'X10' else bal_lit
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        bal2 = bal_lit if ex2.name == 'Lighter' else bal_x10

        # Balance Safety Check (5% buffer)
        if bal1 < final_usd * 1.05:
            logger.debug(f"⚠️ {symbol}: {ex1.name} balance insufficient (${bal1:.2f} < ${final_usd*1.05:.2f})")
            return False
        if bal2 < final_usd * 1.05:
            logger.debug(f"⚠️ {symbol}: {ex2.name} balance insufficient (${bal2:.2f} < ${final_usd*1.05:.2f})")
            return False

        # Get Prices
        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l:
            logger.warning(f"⚠️ {symbol}: Missing price data")
            return False
        
        # ===== 4. PREPARE TRADE DATA =====
        trade = opp.copy()
        trade.update({
            'notional_usd': final_usd,
            'entry_time': datetime.utcnow(),
            'entry_price_x10': p_x,
            'entry_price_lighter': p_l,
            'initial_spread_pct': abs(p_x - p_l) / p_x * 100
        })
        
        side1 = opp['leg1_side']
        side2 = "SELL" if side1 == "BUY" else "BUY"
        
        logger.info(
            f"🚀 EXECUTE {symbol} ${final_usd:.2f} | "
            f"{ex1.name} {side1} → {ex2.name} {side2} | "
            f"Confidence: {confidence:.2f}"
        )
        
        # ===== 5. EXECUTION LEG 1 =====
        try:
            success1, order_id1 = await ex1.open_live_position(
                symbol, side1, final_usd, post_only=(ex1.name == "X10")
            )
        except Exception as e:
            logger.error(f"❌ {symbol} LEG 1 ({ex1.name}) Exception: {e}")
            FAILED_COINS[symbol] = time.time()
            return False
        
        if not success1:
            logger.warning(f"⚠️ {symbol} LEG 1 ({ex1.name}) failed")
            FAILED_COINS[symbol] = time.time()
            return False

        logger.info(f"✅ {symbol} LEG 1 ({ex1.name}) success: {order_id1 or 'OK'}")

        # ===== 6. EXECUTION LEG 2 (with Retry) =====
        ok2 = False
        order_id2 = None
        
        for retry_attempt in range(2):
            try:
                s2, oid2 = await ex2.open_live_position(
                    symbol, side2, final_usd, post_only=False
                )
                
                if s2:
                    ok2 = True
                    order_id2 = oid2
                    logger.info(f"✅ {symbol} LEG 2 ({ex2.name}) success: {order_id2 or 'OK'}")
                    break
                else:
                    logger.warning(f"⚠️ {symbol} LEG 2 ({ex2.name}) attempt {retry_attempt+1}/2 failed")
                    
            except Exception as e:
                logger.error(f"❌ {symbol} LEG 2 ({ex2.name}) Exception (attempt {retry_attempt+1}/2): {e}")
            
            if retry_attempt < 1:  # Don't sleep after last attempt
                await asyncio.sleep(1.0)

        # ===== 7. ROLLBACK IF LEG 2 FAILED =====
        if not ok2:
            logger.critical(
                f"⛔ {symbol} LEG 2 ({ex2.name}) FAILED after 2 attempts! "
                f"Rolling back LEG 1 ({ex1.name})..."
            )
            
            # Rollback LEG 1
            try:
                rollback_success, _ = await ex1.close_live_position(symbol, side1, final_usd)
                if rollback_success:
                    logger.info(f"✅ {symbol} Rollback successful")
                else:
                    logger.error(f"❌ {symbol} Rollback FAILED - MANUAL INTERVENTION REQUIRED!")
                    logger.error(f"   Open position on {ex1.name}: {side1} ${final_usd:.2f}")
            except Exception as e:
                logger.error(f"❌ {symbol} Rollback Exception: {e}")
                logger.error(f"   Open position on {ex1.name}: {side1} ${final_usd:.2f}")
            
            # Blacklist
            FAILED_COINS[symbol] = time.time()
            
            return False

        # ===== 8. DB WRITE (Only after BOTH legs successful) =====
        try:
            await write_detailed_trade_to_db(trade)
            logger.info(f"💾 {symbol} saved to DB as OPEN")
            
        except Exception as db_err:
            logger.critical(f"⛔ {symbol} DB WRITE FAILED AFTER SUCCESSFUL EXECUTION!")
            logger.critical(f"   Trade is LIVE on both exchanges but NOT in DB!")
            logger.critical(f"   {ex1.name}: {side1} ${final_usd:.2f}")
            logger.critical(f"   {ex2.name}: {side2} ${final_usd:.2f}")
            logger.critical(f"   Error: {db_err}")
            logger.critical(f"   🚨 ZOMBIE CLEANUP will handle this, but PnL tracking lost!")
            
            return False

        # ===== 9. FEE TRACKING (Background Tasks) =====
        if order_id1:
            asyncio.create_task(process_fee_update(ex1, symbol, order_id1))
        if order_id2:
            asyncio.create_task(process_fee_update(ex2, symbol, order_id2))

        logger.info(f"🎉 {symbol} Trade executed successfully!")
        logger.debug(f"🔓 {symbol} execution lock released")
        return True
    # 🔓 CRITICAL SECTION END (Lock auto-released by 'async with')

async def manage_open_trades(lighter, x10):
    """Managed offene Trades (Stop Loss, Take Profit, etc.)"""
    trades = await get_open_trades_from_db()
    if not trades:
        return
    
    for t in trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        rx, rl = x10.fetch_funding_rate(sym), lighter.fetch_funding_rate(sym)
        
        if not all([px, pl, rx is not None, rl is not None]):
            continue
        
        net = rl - rx
        pnl = (net * ((datetime.utcnow() - t['entry_time']).total_seconds() / 3600) * t['notional_usd']) - (t['notional_usd'] * 0.0012)
        
        reason = None
        if t.get('is_farm_trade') and (datetime.utcnow() - t['entry_time']).total_seconds() > 600:
            reason = "Farm Done"
        elif pnl < -t['notional_usd'] * 0.02:
            reason = "Stop Loss"
        elif pnl > t['notional_usd'] * 0.03:
            reason = "Take Profit"
        
        if reason:
            if await close_trade(t, lighter, x10):
                await update_trade_status(sym, 'CLOSED')
                await archive_trade_to_history(t, reason, {
                    'total_net_pnl': pnl,
                    'funding_pnl': 0,
                    'spread_pnl': 0,
                    'fees': 0
                })
                if not t.get('is_farm_trade'):
                    re = await check_reentry_opportunity(sym, lighter, x10)
                    if re:
                        await execute_trade(re, lighter, x10)

async def cleanup_zombie_positions(lighter, x10):
    """
    🧹 UNIVERSAL ZOMBIE CLEANUP
    
    Findet und behebt:
    1. Exchange Zombies: Position existiert, aber KEIN DB-Eintrag
       → Close auf BEIDEN Exchanges
    
    2. DB Zombies: DB-Eintrag existiert, aber KEINE Position
       → Set status='CLOSED' in DB + Archive
    
    Läuft alle 5 Minuten im Logic Loop.
    """
    try:
        logger.debug("🧹 Running zombie cleanup...")

        # ===== 1. HOLE EXCHANGE POSITIONEN =====
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()

        # Build Maps
        exchange_symbols = set()
        x10_pos_map = {}
        lighter_pos_map = {}

        for p in (x10_positions or []):
            if abs(p.get('size', 0)) > 1e-8:
                sym = p['symbol']
                exchange_symbols.add(sym)
                x10_pos_map[sym] = p

        for p in (lighter_positions or []):
            if abs(p.get('size', 0)) > 1e-8:
                sym = p['symbol']
                exchange_symbols.add(sym)
                lighter_pos_map[sym] = p

        # ===== 2. HOLE DB TRADES =====
        db_trades = await get_open_trades_from_db()
        db_symbols = {t['symbol'] for t in db_trades}

        # ===== 3. FINDE ZOMBIES =====
        exchange_zombies = exchange_symbols - db_symbols  # Auf Exchange aber nicht in DB
        db_zombies = db_symbols - exchange_symbols        # In DB aber nicht auf Exchange

        # ===== 4. HANDLE EXCHANGE ZOMBIES =====
        if exchange_zombies:
            logger.warning(f"🧟 EXCHANGE ZOMBIES DETECTED: {exchange_zombies}")

            for symbol in exchange_zombies:
                logger.info(f"🧹 Cleaning exchange zombie: {symbol}")

                # Close auf X10 (falls vorhanden)
                if symbol in x10_pos_map:
                    pos = x10_pos_map[symbol]
                    size = pos['size']
                    price = x10.fetch_mark_price(symbol) or 0
                    notional = abs(size * price)

                    if notional > 0:
                        original_side = "BUY" if size > 0 else "SELL"
                        logger.info(f"   Closing X10 {symbol}: {size:.6f} ({original_side})")

                        try:
                            success, oid = await x10.close_live_position(
                                symbol, original_side, notional
                            )
                            if success:
                                logger.info(f"   ✅ X10 {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(x10, symbol, oid))
                            else:
                                logger.warning(f"   ⚠️ X10 {symbol} close failed")
                        except Exception as e:
                            logger.error(f"   ❌ X10 {symbol} close error: {e}")

                # Close auf Lighter (falls vorhanden)
                if symbol in lighter_pos_map:
                    pos = lighter_pos_map[symbol]
                    size = pos['size']
                    price = lighter.fetch_mark_price(symbol) or 0
                    notional = abs(size * price)

                    if notional > 0:
                        original_side = "BUY" if size > 0 else "SELL"
                        logger.info(f"   Closing Lighter {symbol}: {size:.6f} ({original_side})")

                        try:
                            success, oid = await lighter.close_live_position(
                                symbol, original_side, notional
                            )
                            if success:
                                logger.info(f"   ✅ Lighter {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(lighter, symbol, oid))
                            else:
                                logger.warning(f"   ⚠️ Lighter {symbol} close failed")
                        except Exception as e:
                            logger.error(f"   ❌ Lighter {symbol} close error: {e}")

                # Rate limiting zwischen Closes
                await asyncio.sleep(0.5)

            logger.info(f"✅ Exchange zombie cleanup complete: {len(exchange_zombies)} cleaned")

        # ===== 5. HANDLE DB ZOMBIES =====
        if db_zombies:
            logger.warning(f"💾 DB ZOMBIES DETECTED: {db_zombies}")

            async with aiosqlite.connect(config.DB_FILE) as conn:
                for symbol in db_zombies:
                    # Hole Trade-Details für Logging
                    async with conn.execute(
                        "SELECT entry_time, notional_usd, leg1_exchange FROM trades WHERE symbol = ? AND status = 'OPEN'",
                        (symbol,)
                    ) as cursor:
                        row = await cursor.fetchone()

                    if row:
                        entry_time, notional, leg1_ex = row
                        logger.info(
                            f"   Closing DB zombie: {symbol} "
                            f"(${notional:.2f}, {leg1_ex}, entered {entry_time})"
                        )

                    # Setze status auf CLOSED
                    await conn.execute(
                        "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'",
                        (symbol,)
                    )

                await conn.commit()

            logger.info(f"✅ DB zombie cleanup complete: {len(db_zombies)} marked CLOSED")

            # Archive zu History
            for symbol in db_zombies:
                trade_data = next((t for t in db_trades if t['symbol'] == symbol), None)
                if trade_data:
                    await archive_trade_to_history(
                        trade_data,
                        close_reason="Zombie Cleanup (no exchange position)",
                        pnl_data={
                            'total_net_pnl': 0.0,
                            'funding_pnl': 0.0,
                            'spread_pnl': 0.0,
                            'fees': 0.0
                        }
                    )

        # ===== 6. SUCCESS MESSAGE =====
        if not exchange_zombies and not db_zombies:
            logger.debug("✅ No zombies found - all clean")
        else:
            total_cleaned = len(exchange_zombies) + len(db_zombies)
            logger.info(f"🎉 Zombie cleanup complete: {total_cleaned} total zombies handled")

    except Exception as e:
        logger.error(f"❌ Zombie cleanup error: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def sync_db_with_exchanges(lighter, x10):
    """
    🔄 SYNCHRONISIERT DB MIT EXCHANGE-POSITIONEN BEIM STARTUP
    
    Flow:
    1. Hole alle OPEN trades aus DB
    2. Hole echte Positionen von Exchanges
    3. Für jeden DB-Trade ohne Exchange-Position → Setze status='CLOSED'
    
    Dies verhindert "Ghost Trades" nach manuellem Schließen.
    """
    try:
        logger.info("🔄 Syncing DB with Exchange positions...")
    
        # 1. Hole DB Trades
        db_trades = await get_open_trades_from_db()
        if not db_trades:
            logger.info("✅ No open trades in DB - sync not needed")
            return
    
        db_symbols = {t['symbol'] for t in db_trades}
        logger.info(f"📚 DB claims {len(db_symbols)} open: {db_symbols}")
    
        # 2. Hole Exchange Positions
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
    
        exchange_symbols = set()
        for p in (x10_positions or []) + (lighter_positions or []):
            if abs(p.get('size', 0)) > 1e-8:
                exchange_symbols.add(p['symbol'])
    
        logger.info(f"📊 Exchanges have {len(exchange_symbols)} open: {exchange_symbols}")
    
        # 3. Finde Ghost Trades (in DB aber nicht auf Exchange)
        ghost_trades = db_symbols - exchange_symbols
    
        if not ghost_trades:
            logger.info("✅ DB and Exchanges are in sync")
            return
    
        # 4. Close Ghost Trades in DB
        logger.warning(f"👻 GHOST TRADES found (closed manually?): {ghost_trades}")
    
        async with aiosqlite.connect(config.DB_FILE) as conn:
            for symbol in ghost_trades:
                # Hole Trade-Details für Logging
                async with conn.execute(
                    "SELECT entry_time, notional_usd FROM trades WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                ) as cursor:
                    row = await cursor.fetchone()
            
                if row:
                    entry_time, notional = row
                    logger.info(f"   Closing ghost: {symbol} (${notional:.2f}, entered {entry_time})")
            
                # Setze status auf CLOSED
                await conn.execute(
                    "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                )
        
            await conn.commit()
    
        logger.info(f"✅ Closed {len(ghost_trades)} ghost trades in DB")
    
        # Optional: Archive zu History (mit Null-PnL)
        for symbol in ghost_trades:
            trade_data = next((t for t in db_trades if t['symbol'] == symbol), None)
            if trade_data:
                await archive_trade_to_history(
                    trade_data,
                    close_reason="Manual Close (synced at startup)",
                    pnl_data={
                        'total_net_pnl': 0.0,
                        'funding_pnl': 0.0,
                        'spread_pnl': 0.0,
                        'fees': 0.0
                    }
                )
    
    except Exception as e:
        logger.error(f"❌ DB Sync Error: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def volume_farm_mode(lighter, x10, open_syms):
    """Volume Farming Mode"""
    if not config.VOLUME_FARM_MODE or random.random() > 0.2:
        return
    
    open_farm = [t for t in await get_open_trades_from_db() if t.get('is_farm_trade')]
    if len(open_farm) >= config.FARM_MAX_CONCURRENT:
        return

    FARM_WHITELIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ARB-USD", "OP-USD"]
    candidates = [s for s in FARM_WHITELIST if s not in open_syms]
    random.shuffle(candidates)
    
    for symbol in candidates:
        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l:
            continue
        
        if abs(p_x - p_l) / p_x * 100 > config.FARM_MAX_SPREAD_PCT:
            continue
        
        net = (lighter.fetch_funding_rate(symbol) or 0) - (x10.fetch_funding_rate(symbol) or 0)
        direction = "Lighter_LONG" if net > -0.0005 else "X10_LONG"
        size = config.FARM_NOTIONAL_USD * (1.0 + random.uniform(-config.FARM_RANDOM_SIZE_PCT, config.FARM_RANDOM_SIZE_PCT))
        
        logger.info(f"🚜 FARM START: {symbol} ${size:.1f}")
        await execute_trade({
            "symbol": symbol,
            "apy": 0,
            "net_funding_hourly": net,
            "leg1_exchange": "Lighter" if direction == "Lighter_LONG" else "X10",
            "leg1_side": "BUY",
            "is_farm_trade": True,
            "notional_usd": round(size, 1)
        }, lighter, x10)
        break

async def find_opportunities(lighter, x10, open_syms):
    """Findet Arbitrage-Opportunities mit korrekter APY-Berechnung"""
    opps = []
    common = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    now = time.time()
    
    checked = 0
    reasons = {
        "blacklist": 0,
        "open": 0,
        "missing_data": 0,
        "low_apy": 0,
        "high_spread": 0
    }

    for s in common:
        if is_tradfi_or_fx(s):
            continue
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
        
        # KORREKTE APY CALCULATION
        net_hourly = rl - rx
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

        # Direction Logic
        if rl > rx:
            leg1_exchange = 'Lighter'
            leg1_side = 'SELL'
        else:
            leg1_exchange = 'Lighter'
            leg1_side = 'BUY'

            # ✅ FIXED: Smart Logging mit Cache
            if apy > 0.5:
                now = time.time()
                last_log = OPPORTUNITY_LOG_CACHE.get(s, 0)
                if now - last_log > OPPORTUNITY_LOG_COOLDOWN:
                    logger.info(
                        f"💎 OPPORTUNITY: {s} | APY: {apy*100:.1f}% | "
                        f"Spread: {spread:.3f}% | NetHourly: {net_hourly:.6f}"
                    )
                    OPPORTUNITY_LOG_CACHE[s] = now
                # Kein else-Block mehr! Kein DEBUG-Log!
            opps.append({
                'symbol': s,
                'apy': apy * 100,
                'net_funding_hourly': net_hourly,
                'leg1_exchange': leg1_exchange,
                'leg1_side': leg1_side
            })
    
    opps.sort(key=lambda x: x['apy'], reverse=True)
    
    if random.random() < 0.1:
        logger.info(f"🔎 Scan Report: {checked} Pairs geprüft. Rejects: {reasons}")
        
    return opps[:config.MAX_OPEN_TRADES]

async def log_real_pnl(lighter, x10):
    """Logged Balance"""
    try:
        x_bal = await x10.get_real_available_balance()
        l_bal = await lighter.get_real_available_balance()
        logger.info(f"📊 BALANCE: ${x_bal + l_bal:.2f} (X10: ${x_bal:.0f} | Lit: ${l_bal:.0f})")
    except:
        pass

async def slow_poll_funding(lighter, x10):
    """Slow Poller für Funding Rates"""
    logger.info("🐢 Slow Poller (Funding Rates) aktiv.")
    while True:
        try:
            await x10.load_market_cache(force=True)
            await lighter.load_market_cache(force=True)
            await asyncio.sleep(2.0)
            max_retries = 2
            for retry in range(max_retries + 1):
                try:
                    await lighter.load_funding_rates_and_prices()
                    break  # Success!
                except Exception as e:
                    if "429" in str(e) and retry < max_retries:
                        wait_time = (retry + 1) * 5  # 5s, 10s
                        logger.warning(
                            f"⚠️ Lighter Funding Rate Limit. "
                            f"Retry {retry+1}/{max_retries} in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                    elif retry == max_retries:
                        logger.warning(f"⚠️ Lighter Funding fetch failed after retries. Using cached rates.")
                        break
                    else:
                        raise e
            await save_current_funding_rates(lighter, x10)
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Funding Poll Error: {e}")
            await asyncio.sleep(30)

async def logic_loop(lighter, x10, price_event: asyncio.Event):
    """Event-Driven Logic Loop"""
    logger.info("🧠 Logic Loop (Event-Driven) aktiv.")
    
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0
    last_opportunity_scan = 0
    last_max_trades_warning = 0  # ✅ NEU
    
    SCAN_COOLDOWN_SECONDS = 2.0

    await asyncio.sleep(3)

    while True:
        try:
            try:
                await asyncio.wait_for(price_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            price_event.clear()

            now = time.time()

            # 1. Manage Trades
            await manage_open_trades(lighter, x10)

            # 2. Zombie Cleanup
            if now - last_zombie_check > 300:
                await cleanup_zombie_positions(lighter, x10)
                last_zombie_check = now

            # 3. Balance Log
            if now - last_pnl_log > 60:
                await log_real_pnl(lighter, x10)
                last_pnl_log = now

            # 4. Rate-Limited Opportunity Scan
            if now - last_opportunity_scan < SCAN_COOLDOWN_SECONDS:
                continue
            
            last_opportunity_scan = now

            # 5. Get Current Trades
            trades = await get_open_trades_from_db()
            current_open_count = len(trades)
            open_symbols = {t['symbol'] for t in trades}

            # ✅ FIXED: Early exit mit rate-limited warning
            if current_open_count >= config.MAX_OPEN_TRADES:
                if now - last_max_trades_warning > 60:  # Nur 1x pro Minute
                    logger.info(
                        f"⏸️  Max Open Trades: {current_open_count}/{config.MAX_OPEN_TRADES}. "
                        f"Waiting for exit signals..."
                    )
                    last_max_trades_warning = now
                continue  # Skip opportunity scan & farm mode

            opps = await find_opportunities(lighter, x10, open_symbols)

            # TEMPORÄRER TEST CODE (nach find_opportunities)
            if opps and len(opps) > 0:
                test_opp = opps[0]
                logger.info(f"🧪 TEST: Starte 2 parallele Executions für {test_opp['symbol']}")
                results = await asyncio.gather(
                    execute_trade(test_opp, lighter, x10),
                    execute_trade(test_opp, lighter, x10),
                    return_exceptions=True
                )
                logger.info(f"🧪 TEST RESULT: {results}")
                # Erwartung: [True, False] oder [False, False]
                # NIE: [True, True] ← das wäre Race Condition!

            # 7. Execute Trades
            trade_executed = False
            for opp in opps:
                if current_open_count >= config.MAX_OPEN_TRADES:
                    break  # ✅ KEIN LOG mehr hier (schon oben geloggt)
                
                if await execute_trade(opp, lighter, x10):
                    trade_executed = True
                    current_open_count += 1
                    open_symbols.add(opp['symbol'])
                    await asyncio.sleep(0.5)

            # 8. Farm Mode (nur wenn kein Trade executed wurde)
            if now - last_farm_check > 10:
                if not trade_executed and current_open_count < config.MAX_OPEN_TRADES:
                    await volume_farm_mode(lighter, x10, open_symbols)
                last_farm_check = now

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Logic Crash: {e}")
            await asyncio.sleep(5)

async def main():
    """Main Entry Point"""
    global SHUTDOWN_FLAG
    logger.info("🚀 BOT V3 (Event-Driven Edition) START")
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_update_event = asyncio.Event()

    x10.price_update_event = price_update_event
    lighter.price_update_event = price_update_event

    await setup_database()
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    await lighter.load_funding_rates_and_prices()
    
    # ...existing code...

    t_ws_x10 = asyncio.create_task(x10.start_websocket())
    t_ws_lighter = asyncio.create_task(lighter.start_websocket())
    t_funding = asyncio.create_task(slow_poll_funding(lighter, x10))
    t_logic = asyncio.create_task(logic_loop(lighter, x10, price_update_event))

    try:
        await asyncio.gather(t_ws_x10, t_ws_lighter, t_funding, t_logic)
    except asyncio.CancelledError:
        pass
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
    """Berechnet Netto-Stundenrate und APY"""
    net_hourly_rate_abs = abs(lighter_rate - x10_rate)
    APY_GROSS = net_hourly_rate_abs * HOURS_PER_YEAR * 100.0
    TOTAL_ROUNDTRIP_FEE_PCT = (config.TAKER_FEE_X10 + config.FEES_LIGHTER) * 2.0 * 100.0
    ANNUALIZED_FEE_COST = (config.TAKER_FEE_X10 * 2.0 * 100.0) * 365.0
    APY_NET = APY_GROSS - ANNUALIZED_FEE_COST
    spread_pct = 0.0
    return net_hourly_rate_abs, APY_NET, spread_pct

async def sync_db_with_exchanges(lighter, x10):
    """
    🔄 SYNCHRONISIERT DB MIT EXCHANGE-POSITIONEN BEIM STARTUP

    Flow:
    1. Hole alle OPEN trades aus DB
    2. Hole echte Positionen von Exchanges
    3. Für jeden DB-Trade ohne Exchange-Position → Setze status='CLOSED'

    Dies verhindert "Ghost Trades" nach manuellem Schließen.
    """
    try:
        logger.info("🔄 Syncing DB with Exchange positions...")

        # 1. Hole DB Trades
        db_trades = await get_open_trades_from_db()
        if not db_trades:
            logger.info("✅ No open trades in DB - sync not needed")
            return

        db_symbols = {t['symbol'] for t in db_trades}
        logger.info(f"📚 DB claims {len(db_symbols)} open: {db_symbols}")

        # 2. Hole Exchange Positions
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()

        exchange_symbols = set()
        for p in (x10_positions or []) + (lighter_positions or []):
            if abs(p.get('size', 0)) > 1e-8:
                exchange_symbols.add(p['symbol'])

        logger.info(f"📊 Exchanges have {len(exchange_symbols)} open: {exchange_symbols}")

        # 3. Finde Ghost Trades (in DB aber nicht auf Exchange)
        ghost_trades = db_symbols - exchange_symbols

        if not ghost_trades:
            logger.info("✅ DB and Exchanges are in sync")
            return

        # 4. Close Ghost Trades in DB
        logger.warning(f"👻 GHOST TRADES found (closed manually?): {ghost_trades}")

        async with aiosqlite.connect(config.DB_FILE) as conn:
            for symbol in ghost_trades:
                # Hole Trade-Details für Logging
                async with conn.execute(
                    "SELECT entry_time, notional_usd FROM trades WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                ) as cursor:
                    row = await cursor.fetchone()

                if row:
                    entry_time, notional = row
                    logger.info(f"   Closing ghost: {symbol} (${notional:.2f}, entered {entry_time})")

                # Setze status auf CLOSED
                await conn.execute(
                    "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                )

            await conn.commit()

        logger.info(f"✅ Closed {len(ghost_trades)} ghost trades in DB")

        # Optional: Archive zu History (mit Null-PnL)
        for symbol in ghost_trades:
            trade_data = next((t for t in db_trades if t['symbol'] == symbol), None)
            if trade_data:
                await archive_trade_to_history(
                    trade_data,
                    close_reason="Manual Close (synced at startup)",
                    pnl_data={
                        'total_net_pnl': 0.0,
                        'funding_pnl': 0.0,
                        'spread_pnl': 0.0,
                        'fees': 0.0
                    }
                )

    except Exception as e:
        logger.error(f"❌ DB Sync Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error(traceback.format_exc())

async def volume_farm_mode(lighter, x10, open_syms):
    """Volume Farming Mode"""
    if not config.VOLUME_FARM_MODE or random.random() > 0.2:
        return
    
    open_farm = [t for t in await get_open_trades_from_db() if t.get('is_farm_trade')]
    if len(open_farm) >= config.FARM_MAX_CONCURRENT:
        return

    FARM_WHITELIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ARB-USD", "OP-USD"]
    candidates = [s for s in FARM_WHITELIST if s not in open_syms]
    random.shuffle(candidates)
    
    for symbol in candidates:
        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l:
            continue
        
        if abs(p_x - p_l) / p_x * 100 > config.FARM_MAX_SPREAD_PCT:
            continue
        
        net = (lighter.fetch_funding_rate(symbol) or 0) - (x10.fetch_funding_rate(symbol) or 0)
        direction = "Lighter_LONG" if net > -0.0005 else "X10_LONG"
        size = config.FARM_NOTIONAL_USD * (1.0 + random.uniform(-config.FARM_RANDOM_SIZE_PCT, config.FARM_RANDOM_SIZE_PCT))
        
        logger.info(f"🚜 FARM START: {symbol} ${size:.1f}")
        await execute_trade({
            "symbol": symbol,
            "apy": 0,
            "net_funding_hourly": net,
            "leg1_exchange": "Lighter" if direction == "Lighter_LONG" else "X10",
            "leg1_side": "BUY",
            "is_farm_trade": True,
            "notional_usd": round(size, 1)
        }, lighter, x10)
        break

async def find_opportunities(lighter, x10, open_syms):
    """Findet Arbitrage-Opportunities"""
    opps = []
    common = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    now = time.time()
    
    checked = 0
    reasons = {
        "blacklist": 0,
        "open": 0,
        "missing_data": 0,
        "low_apy": 0,
        "high_spread": 0
    }

    for s in common:
        if is_tradfi_or_fx(s):
            continue
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
        
        net_hourly = rl - rx
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

        if rl > rx:
            leg1_exchange = 'Lighter'
            leg1_side = 'SELL'
        else:
            leg1_exchange = 'Lighter'
            leg1_side = 'BUY'

        # ✅ FIXED: Smart Logging mit Cache
        if apy > 0.5:
            last_log = OPPORTUNITY_LOG_CACHE.get(s, 0)
            
            if now - last_log > OPPORTUNITY_LOG_COOLDOWN:
                logger.info(
                    f"💎 OPPORTUNITY: {s} | APY: {apy*100:.1f}% | "
                    f"Spread: {spread:.3f}% | NetHourly: {net_hourly:.6f}"
                )
                OPPORTUNITY_LOG_CACHE[s] = now
            
        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net_hourly,
            'leg1_exchange': leg1_exchange,
            'leg1_side': leg1_side
        })
    
    opps.sort(key=lambda x: x['apy'], reverse=True)
    
    if random.random() < 0.1:
        logger.info(f"🔎 Scan Report: {checked} Pairs geprüft. Rejects: {reasons}")
        
    return opps[:config.MAX_OPEN_TRADES]

async def log_real_pnl(lighter, x10):
    """Logged Balance"""
    try:
        x_bal = await x10.get_real_available_balance()
        l_bal = await lighter.get_real_available_balance()
        logger.info(f"📊 BALANCE: ${x_bal + l_bal:.2f} (X10: ${x_bal:.0f} | Lit: ${l_bal:.0f})")
    except:
        pass

async def slow_poll_funding(lighter, x10):
    """Slow Poller für Funding Rates"""
    logger.info("🐢 Slow Poller (Funding Rates) aktiv.")
    while True:
        try:
            await x10.load_market_cache(force=True)
            await lighter.load_market_cache(force=True)
            await lighter.load_funding_rates_and_prices()
            await save_current_funding_rates(lighter, x10)
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Funding Poll Error: {e}")
            await asyncio.sleep(30)

async def logic_loop(lighter, x10, price_event: asyncio.Event):
    """Event-Driven Logic Loop"""
    logger.info("🧠 Logic Loop (Event-Driven) aktiv.")
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0
    last_opportunity_scan = 0
    last_max_trades_warning = 0  # ✅ NEU
    SCAN_COOLDOWN_SECONDS = 2.0

    await asyncio.sleep(3)

    while True:
        try:
            try:
                await asyncio.wait_for(price_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            price_event.clear()

            now = time.time()

            await manage_open_trades(lighter, x10)

            if now - last_zombie_check > 300:
                await cleanup_zombie_positions(lighter, x10)
                last_zombie_check = now

            if now - last_pnl_log > 60:
                await log_real_pnl(lighter, x10)
                last_pnl_log = now

            if now - last_opportunity_scan < SCAN_COOLDOWN_SECONDS:
                continue
            
            last_opportunity_scan = now

            trades = await get_open_trades_from_db()
            current_open_count = len(trades)
            open_symbols = {t['symbol'] for t in trades}

            # ✅ EARLY EXIT: Check BEFORE scanning
            if current_open_count >= config.MAX_OPEN_TRADES:
                if now - last_max_trades_warning > 60:
                    logger.info(
                        f"⏸️  Max Open Trades: {current_open_count}/{config.MAX_OPEN_TRADES}. "
                        f"Waiting for exit signals..."
                    )
                    last_max_trades_warning = now
                continue  # Skip opportunity scan & farm mode

            # Nur hier wenn < MAX_OPEN_TRADES
            opps = await find_opportunities(lighter, x10, open_symbols)

            trade_executed = False
            for opp in opps:
                if current_open_count >= config.MAX_OPEN_TRADES:
                    break  # Kein Log mehr hier!
                if await execute_trade(opp, lighter, x10):
                    trade_executed = True
                    current_open_count += 1
                    open_symbols.add(opp['symbol'])
                    await asyncio.sleep(0.5)

            if now - last_farm_check > 10:
                if not trade_executed and current_open_count < config.MAX_OPEN_TRADES:
                    await volume_farm_mode(lighter, x10, open_symbols)
                last_farm_check = now

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Logic Crash: {e}")
            await asyncio.sleep(5)

async def main():
    """Main Entry Point"""
    global SHUTDOWN_FLAG
    logger.info("🚀 BOT V3 (Event-Driven Edition) START")
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_update_event = asyncio.Event()

    x10.price_update_event = price_update_event
    lighter.price_update_event = price_update_event

    await setup_database()
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    await lighter.load_funding_rates_and_prices()
    
    await sync_db_with_exchanges(lighter, x10)

    t_ws_x10 = asyncio.create_task(x10.start_websocket())
    t_ws_lighter = asyncio.create_task(lighter.start_websocket())
    t_funding = asyncio.create_task(slow_poll_funding(lighter, x10))
    t_logic = asyncio.create_task(logic_loop(lighter, x10, price_update_event))

    try:
        await asyncio.gather(t_ws_x10, t_ws_lighter, t_funding, t_logic)
    except asyncio.CancelledError:
        pass
    finally:
        SHUTDOWN_FLAG = True
        logger.info("🛑 SHUTDOWN: Closing connections...")
        await x10.aclose()
        await lighter.aclose()
        logger.info("✅ Closed.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass