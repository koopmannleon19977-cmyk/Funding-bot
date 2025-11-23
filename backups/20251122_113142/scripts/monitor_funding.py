# monitor_funding.py - TEIL 1
FAILED_COINS = {}
state_manager = None  # Global state manager instance
OPPORTUNITY_LOG_CACHE = {}  # ✅ NEU
OPPORTUNITY_LOG_COOLDOWN = 60  # Sekunden

# Task management for non-blocking trade execution
ACTIVE_TASKS = {}
 

import sys
import os
import time
import asyncio
import aiosqlite
import logging
import random
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Set, Tuple, Any
import pandas as pd
############################################################
# POSITION CACHE (Race Condition + Rate Limit Prevention)
# ============================================================
POSITION_CACHE = {
    'x10': [],
    'lighter': [],
    'last_update': 0.0
}
POSITION_CACHE_TTL = 15  # Sekunden (15s = fresher data, schnellere reconciliation)


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


async def cleanup_finished_tasks():
    """Remove completed tasks from tracking"""
    lock = get_task_lock()
    async with lock:
        finished = [
            symbol for symbol, task in ACTIVE_TASKS.items()
            if task.done()
        ]
        for symbol in finished:
            del ACTIVE_TASKS[symbol]
        
        if finished:
            logger.debug(f"Cleaned {len(finished)} finished tasks")


async def execute_trade_task(opp: Dict, lighter, x10):
    """Wrapper for execute_trade with task tracking"""
    symbol = opp['symbol']
    try:
        return await execute_trade(opp, lighter, x10)
    finally:
        # Remove from active tasks when done
        lock = get_task_lock()
        async with lock:
            if symbol in ACTIVE_TASKS:
                del ACTIVE_TASKS[symbol]

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

async def quick_position_check():
    """Quick manual position check"""
    from src.adapters.lighter_adapter import LighterAdapter
    lighter = LighterAdapter()
    
    await lighter.load_market_cache(force=True)
    positions = await lighter.fetch_open_positions()
    
    print("\n=== LIGHTER POSITIONS ===")
    for p in positions:
        print(f"{p['symbol']}: {p['size']:.6f}")
    print("========================\n")
    
    await lighter.aclose()

# Uncomment to run:
# asyncio.run(quick_position_check())

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction import predict_next_funding_rates, calculate_smart_size
from src.state_manager import InMemoryStateManager

logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = config.setup_logging()
config.validate_runtime_config(logger)

SHUTDOWN_FLAG = False
# ✅ GLOBAL LOCKS - SINGLETON PATTERN
POSITION_CACHE_LOCK = None
TASK_LOCK = None

def get_position_cache_lock() -> asyncio.Lock:
    """Thread-safe position cache lock getter"""
    global POSITION_CACHE_LOCK
    if POSITION_CACHE_LOCK is None:
        POSITION_CACHE_LOCK = asyncio.Lock()
    return POSITION_CACHE_LOCK

def get_task_lock() -> asyncio.Lock:
    """Thread-safe task lock getter"""
    global TASK_LOCK
    if TASK_LOCK is None:
        TASK_LOCK = asyncio.Lock()
    return TASK_LOCK

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
    timeout = 15  # Max total wait time
    start_time = time.time()
    
    for attempt in range(max_retries + 1):
        try:
            if time.time() - start_time > timeout:
                break
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

async def get_open_trades() -> List[Dict]:
    """Lädt offene Trades aus In-Memory State (falls vorhanden), sonst aus DB"""
    global state_manager
    # Prefer in-memory state for instant reads
    if state_manager:
        res = state_manager.get_open_trades()
        if asyncio.iscoroutine(res):
            return await res
        return res

    # Fallback to DB
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
                        if val is None or val == "":
                            trade[date_col] = None
                        elif isinstance(val, (int, float)):
                            try:
                                trade[date_col] = datetime.utcfromtimestamp(float(val))
                            except Exception:
                                trade[date_col] = None
                        elif isinstance(val, str):
                            try:
                                trade[date_col] = datetime.fromisoformat(val.replace('Z', '+00:00'))
                            except Exception:
                                try:
                                    trade[date_col] = datetime.strptime(val, '%Y-%m-%d %H:%M:%S.%f')
                                except Exception:
                                    trade[date_col] = None
                    trades.append(trade)
                return trades
    except Exception:
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
    logger.info(f"DB: Trade {symbol} status updated to {status}")

async def add_trade_to_state(trade_data: Dict):
    """Adds trade to in-memory state"""
    global state_manager
    if state_manager:
        # Ensure field exists
        if 'initial_funding_rate_hourly' not in trade_data:
            trade_data['initial_funding_rate_hourly'] = trade_data.get('net_funding_hourly', 0.0)
        maybe = state_manager.add_trade(trade_data)
        if asyncio.iscoroutine(maybe):
            await maybe
    else:
        await write_detailed_trade_to_db(trade_data)

async def close_trade_in_state(symbol: str):
    """Close trade in state manager"""
    global state_manager
    if state_manager:
        maybe = state_manager.close_trade(symbol)
        if asyncio.iscoroutine(maybe):
            await maybe
    else:
        await update_trade_status(symbol, 'CLOSED')

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

async def execute_trade_parallel(opp: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    """
    🚀 PARALLEL EXECUTION with Optimistic Rollback

    Flow:
    1. Acquire symbol lock
    2. Pre-checks (blacklist, balance, sizing)
    3. Fire BOTH legs simultaneously
    4. If both succeed → DB write
    5. If one fails → Rollback successful leg
    6. Release lock
    """
    if SHUTDOWN_FLAG:
        return False

    symbol = opp['symbol']
    lock = await get_execution_lock(symbol)

    if lock.locked():
        logger.debug(f"{symbol} already executing, skip duplicate")
        return False

    async with lock:
        logger.debug(f"{symbol} execution lock acquired")

        # Re-check DB/state after lock
        existing_trades = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing_trades):
            logger.info(f"{symbol} already open (post-lock check)")
            return False

        # Check blacklist
        if symbol in FAILED_COINS:
            if time.time() - FAILED_COINS[symbol] < 300:
                logger.debug(f"{symbol} blacklisted")
                return False
            else:
                del FAILED_COINS[symbol]

        if 'is_farm_trade' not in opp:
            opp['is_farm_trade'] = False

        # Balance check
        try:
            bal_x10 = await x10.get_real_available_balance()
            bal_lit = await lighter.get_real_available_balance()
            total_balance = bal_x10 + bal_lit
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return False

        if total_balance < 1.0:
            logger.warning("Total balance too low")
            return False

        # BTC Trend & Confidence
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        _, _, confidence = await predict_next_funding_rates(symbol, btc_trend_pct=btc_trend)

        # Calculate min requirements
        min_x10 = x10.min_notional_usd(symbol)
        min_lit = lighter.min_notional_usd(symbol)
        absolute_min_required = max(min_x10, min_lit)

        if absolute_min_required > config.MAX_TRADE_SIZE_USD:
            logger.debug(f"Skip {symbol}: Min ${absolute_min_required:.2f} > Max ${config.MAX_TRADE_SIZE_USD:.2f}")
            return False

        # Calculate size
        if opp.get('is_farm_trade'):
            target_usd = opp.get('notional_usd', config.FARM_NOTIONAL_USD)
        else:
            target_usd = calculate_smart_size(confidence, total_balance)

        final_usd = max(target_usd, absolute_min_required)
        final_usd = min(final_usd, config.MAX_TRADE_SIZE_USD)

        # Determine exchanges
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        bal1 = bal_x10 if ex1.name == 'X10' else bal_lit
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        bal2 = bal_lit if ex2.name == 'Lighter' else bal_x10

        min_required_per_exchange = final_usd * 1.10

        if bal1 < min_required_per_exchange or bal2 < min_required_per_exchange:
            logger.debug(f"{symbol}: Insufficient balance")
            return False

        # Get prices
        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l:
            logger.warning(f"{symbol}: Missing price data")
            return False

        # Prepare trade data
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
            f"⚡ PARALLEL EXEC {symbol} ${final_usd:.2f} | "
            f"{ex1.name} {side1} + {ex2.name} {side2} | "
            f"Confidence: {confidence:.2f}"
        )

        # 🚀 FIRE BOTH LEGS SIMULTANEOUSLY
        try:
            results = await asyncio.gather(
                ex1.open_live_position(symbol, side1, final_usd, post_only=(ex1.name == "X10")),
                ex2.open_live_position(symbol, side2, final_usd, post_only=False),
                return_exceptions=True
            )
        except Exception as e:
            logger.error(f"{symbol} Parallel execution exception: {e}")
            FAILED_COINS[symbol] = time.time()
            return False

        # Parse results
        res1, res2 = results

        # Check if exception
        if isinstance(res1, Exception):
            logger.error(f"{symbol} LEG1 ({ex1.name}) exception: {res1}")
            res1 = (False, None)
        if isinstance(res2, Exception):
            logger.error(f"{symbol} LEG2 ({ex2.name}) exception: {res2}")
            res2 = (False, None)

        success1, order_id1 = res1 if isinstance(res1, tuple) else (False, None)
        success2, order_id2 = res2 if isinstance(res2, tuple) else (False, None)

        # 🎯 HANDLE RESULTS
        if success1 and success2:
            logger.info(f"✅✅ {symbol} BOTH LEGS SUCCESS")

            # DB write
            try:
                await add_trade_to_state(trade)
                logger.info(f"{symbol} saved to DB/state")
                POSITION_CACHE['last_update'] = 0.0
            except Exception as db_err:
                logger.critical(f"{symbol} DB WRITE FAILED AFTER SUCCESS!")
                logger.critical(f"   {ex1.name}: {side1} ${final_usd:.2f}")
                logger.critical(f"   {ex2.name}: {side2} ${final_usd:.2f}")
                logger.critical(f"   Error: {db_err}")
                return False

            # Fee tracking (background)
            if order_id1:
                asyncio.create_task(process_fee_update(ex1, symbol, order_id1))
            if order_id2:
                asyncio.create_task(process_fee_update(ex2, symbol, order_id2))

            return True

        elif success1 and not success2:
            logger.error(f"❌ {symbol} LEG2 ({ex2.name}) FAILED - Rolling back LEG1")

            try:
                # ⚠️ CRITICAL: Wait for order settlement before rollback
                await asyncio.sleep(2)

                # Verify if LEG1 actually went through despite error
                positions = await ex1.fetch_open_positions()
                has_position = any(
                    p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8
                    for p in (positions or [])
                )

                if not has_position:
                    logger.info(f"✅ {symbol} LEG1 never executed - no rollback needed")
                    FAILED_COINS[symbol] = time.time()
                    return False

                rollback_success, _ = await ex1.close_live_position(symbol, side1, final_usd)
                if rollback_success:
                    logger.info(f"✅ {symbol} Rollback successful")
                else:
                    logger.error(f"❌ {symbol} Rollback FAILED - MANUAL INTERVENTION!")
                    logger.error(f"   Open position on {ex1.name}: {side1} ${final_usd:.2f}")
            except Exception as e:
                logger.error(f"{symbol} Rollback exception: {e}")
                logger.error(f"   Open position on {ex1.name}: {side1} ${final_usd:.2f}")

            FAILED_COINS[symbol] = time.time()
            return False

        elif not success1 and success2:
            logger.error(f"❌ {symbol} LEG1 ({ex1.name}) FAILED - Rolling back LEG2")

            try:
                # ⚠️ CRITICAL: Wait for order settlement before rollback
                await asyncio.sleep(2)
                
                # Verify if LEG2 actually went through despite error
                positions = await ex2.fetch_open_positions()
                has_position = any(
                    p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8
                    for p in (positions or [])
                )
                
                if not has_position:
                    logger.info(f"✅ {symbol} LEG2 never executed - no rollback needed")
                    FAILED_COINS[symbol] = time.time()
                    return False
                
                rollback_success, _ = await ex2.close_live_position(symbol, side2, final_usd)
                if rollback_success:
                    logger.info(f"✅ {symbol} Rollback successful")
                else:
                    logger.error(f"❌ {symbol} Rollback FAILED - MANUAL INTERVENTION!")
                    logger.error(f"   Open position on {ex2.name}: {side2} ${final_usd:.2f}")
            except Exception as e:
                logger.error(f"{symbol} Rollback exception: {e}")
                logger.error(f"   Open position on {ex2.name}: {side2} ${final_usd:.2f}")

            FAILED_COINS[symbol] = time.time()
            return False

        else:
            logger.warning(f"⚠️ {symbol} BOTH LEGS FAILED")
            FAILED_COINS[symbol] = time.time()
            return False

# Backwards-compatible alias: keep using `execute_trade(...)` throughout the codebase
execute_trade = execute_trade_parallel


async def get_cached_positions(lighter, x10, force_refresh: bool = False):
    """Return cached positions for X10 and Lighter.

    Args:
        lighter: Lighter Adapter
        x10: X10 Adapter
        force_refresh: Force cache refresh (for startup/reconciliation)

    Returns:
        tuple: (x10_positions, lighter_positions)
    """
    async with get_position_cache_lock():
        now = time.time()

        # Check if cache is fresh
        if not force_refresh and (now - POSITION_CACHE['last_update']) < POSITION_CACHE_TTL:
            logger.debug(
                f"Using cached positions (age: {now - POSITION_CACHE['last_update']:.1f}s)"
            )
            return POSITION_CACHE['x10'], POSITION_CACHE['lighter']

        # Cache expired or force refresh -> Fetch new data
        try:
            logger.debug("Refreshing position cache...")

            # Fetch with error handling
            try:
                x10_positions = await x10.fetch_open_positions()
            except Exception as e:
                logger.warning(f"X10 position fetch error: {e}. Using cached data.")
                x10_positions = POSITION_CACHE.get('x10', [])

            try:
                lighter_positions = await lighter.fetch_open_positions()
            except Exception as e:
                logger.warning(f"Lighter position fetch error: {e}. Using cached data.")
                lighter_positions = POSITION_CACHE.get('lighter', [])

            # Update cache
            POSITION_CACHE['x10'] = x10_positions or []
            POSITION_CACHE['lighter'] = lighter_positions or []
            POSITION_CACHE['last_update'] = now

            logger.debug(
                f"Position cache updated: X10={len(POSITION_CACHE['x10'])}, Lighter={len(POSITION_CACHE['lighter'])}"
            )

            return POSITION_CACHE['x10'], POSITION_CACHE['lighter']

        except Exception as e:
            logger.error(f"Position cache refresh error: {e}")
            # Return old cache as fallback
            return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])

async def manage_open_trades(lighter, x10):
    """
    Managed offene Trades mit Position Reconciliation
    """
    # No global needed - we only read POSITION_CACHE through get_cached_positions()

    trades = await get_open_trades()
    if not trades:
        return

    # ===== 1. HOLE CACHED POSITIONS =====
    try:
        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )

        # ===== 🆕 CHECK CACHE FRESHNESS =====
        cache_age = time.time() - POSITION_CACHE['last_update']

        # ===== 🆕 NUR RECONCILE WENN CACHE FRESH (<5s) =====
        should_reconcile = cache_age < 5.0

        if not should_reconcile:
            logger.debug(
                f"⏭️ Skipping reconciliation (cache age: {cache_age:.1f}s > 5s). "
                f"Will check on next refresh."
            )
            exchange_symbols = None  # Skip reconciliation
        else:
            exchange_symbols = set()
            for p in x10_positions + lighter_positions:
                if abs(p.get('size', 0)) > 1e-8:
                    exchange_symbols.add(p['symbol'])

    except Exception as e:
        logger.warning(f"⚠️ Position fetch error in manage_open_trades: {e}")
        exchange_symbols = None

    # ===== 2. RECONCILE NUR WENN CACHE FRESH =====
    manually_closed = []

    if exchange_symbols is not None and should_reconcile:
        for t in trades:
            sym = t['symbol']

            # Check ob Position noch auf BEIDEN Exchanges existiert
            if sym not in exchange_symbols:
                logger.warning(
                    f"🔍 RECONCILE: {sym} in DB aber nicht auf Exchange! "
                    f"(Cache age: {cache_age:.1f}s)"
                )
                manually_closed.append(t)
    
    # ===== 3. CLOSE MANUELL GESCHLOSSENE TRADES IN DB =====
    for t in manually_closed:
        sym = t['symbol']
        
        logger.info(f"🔄 Closing manually closed position in DB: {sym}")
        
        # Update DB/state Status
        await close_trade_in_state(sym)
        
        # Archive mit Zero PnL (wir kennen echten PnL nicht)
        await archive_trade_to_history(
            t,
            close_reason="Manual Close (detected by reconciliation)",
            pnl_data={
                'total_net_pnl': 0.0,
                'funding_pnl': 0.0,
                'spread_pnl': 0.0,
                'fees': 0.0
            }
        )
        
        logger.info(f"✅ {sym} DB cleanup complete. Slot freed for new trade.")
    
    # ===== 4. MANAGE REMAINING OPEN TRADES =====
    active_trades = [t for t in trades if t not in manually_closed]
    
    for t in active_trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        rx, rl = x10.fetch_funding_rate(sym), lighter.fetch_funding_rate(sym)
        
        if not all([px, pl, rx is not None, rl is not None]):
            continue
        
        # ===== 5. CALCULATE PNL =====
        net = rl - rx
        hold_hours = (datetime.utcnow() - t['entry_time']).total_seconds() / 3600
        
        # Funding PnL
        funding_pnl = net * hold_hours * t['notional_usd']
        
        # Spread PnL (Entry vs Current)
        entry_spread = abs(t['entry_price_x10'] - t['entry_price_lighter'])
        current_spread = abs(px - pl)
        spread_change = entry_spread - current_spread
        spread_pnl = spread_change / t['entry_price_x10'] * t['notional_usd']
        
        # Estimate Fees (konservativ)
        estimated_fees = t['notional_usd'] * 0.0012  # 2x Taker (0.025% + 0.025% + buffer)
        
        # Total PnL
        total_pnl = funding_pnl + spread_pnl - estimated_fees
        
        # ===== 6. CHECK EXIT SIGNALS =====
        reason = None
        
        # Farm Trade Check
        if t.get('is_farm_trade'):
            hold_seconds = (datetime.utcnow() - t['entry_time']).total_seconds()
            if hold_seconds > config.FARM_HOLD_SECONDS:
                reason = "Farm Done"
        
        # Stop Loss (2% loss)
        elif total_pnl < -t['notional_usd'] * 0.02:
            reason = "Stop Loss"
            logger.warning(
                f"🛑 {sym} Stop Loss triggered! "
                f"PnL: ${total_pnl:.2f} ({total_pnl/t['notional_usd']*100:.2f}%)"
            )
        
        # Take Profit (3% gain)
        elif total_pnl > t['notional_usd'] * 0.03:
            reason = "Take Profit"
            logger.info(
                f"💰 {sym} Take Profit triggered! "
                f"PnL: ${total_pnl:.2f} ({total_pnl/t['notional_usd']*100:.2f}%)"
            )
        
        # Funding Rate Flip Check
        elif t.get('initial_funding_rate_hourly') is not None:
            initial_net = t['initial_funding_rate_hourly']
            
            # Check if funding flipped sign
            if (initial_net > 0 and net < 0) or (initial_net < 0 and net > 0):
                # Start tracking flip
                if not t.get('funding_flip_start_time'):
                    logger.info(
                        f"⚠️ {sym} Funding Flipped! "
                        f"Initial: {initial_net:.6f} → Current: {net:.6f}"
                    )
                    
                    # Update DB
                    async with aiosqlite.connect(config.DB_FILE) as conn:
                        await conn.execute(
                            "UPDATE trades SET funding_flip_start_time = ? WHERE symbol = ?",
                            (datetime.utcnow(), sym)
                        )
                        await conn.commit()
                    
                    t['funding_flip_start_time'] = datetime.utcnow()
                
                # Check if flipped for too long
                elif t.get('funding_flip_start_time'):
                    flip_hours = (datetime.utcnow() - t['funding_flip_start_time']).total_seconds() / 3600
                    
                    if flip_hours > config.FUNDING_FLIP_HOURS_THRESHOLD:
                        reason = f"Funding Flipped {flip_hours:.1f}h"
                        logger.info(
                            f"🔄 {sym} Closing due to prolonged funding flip "
                            f"({flip_hours:.1f}h > {config.FUNDING_FLIP_HOURS_THRESHOLD}h)"
                        )
        
        # Max Hold Time
        if not reason and hold_hours > (config.DYNAMIC_HOLD_MAX_DAYS * 24):
            reason = f"Max Hold ({hold_hours/24:.1f}d)"
        
        # ===== 7. EXECUTE CLOSE IF SIGNAL =====
        if reason:
            logger.info(f"📉 Closing {sym} (Reason: {reason})")
            
            if await close_trade(t, lighter, x10):
                await close_trade_in_state(sym)
                await archive_trade_to_history(
                    t, 
                    reason, 
                    {
                        'total_net_pnl': total_pnl,
                        'funding_pnl': funding_pnl,
                        'spread_pnl': spread_pnl,
                        'fees': estimated_fees
                    }
                )
                
                # ===== FORCE CACHE REFRESH AFTER CLOSE =====
                POSITION_CACHE['last_update'] = 0.0
                logger.debug(f"🔄 Position cache invalidated after {sym} close")

                # ===== 8. CHECK RE-ENTRY OPPORTUNITY =====
                if not t.get('is_farm_trade'):
                    logger.debug(f"🔍 Checking re-entry opportunity for {sym}...")
                    re = await check_reentry_opportunity(sym, lighter, x10)
                    if re:
                        logger.info(f"🔄 Re-entry opportunity found for {sym}!")
                        await execute_trade(re, lighter, x10)

async def cleanup_zombie_positions(lighter, x10):
    """
    🧹 UNIVERSAL ZOMBIE CLEANUP - FIXED VERSION
    
    Detects and fixes:
    1. Half-Open Zombies: Position nur auf EINER Exchange (kritisch!)
    2. Full Zombies: Position auf beiden Exchanges, aber NICHT in DB
    3. DB Zombies: In DB, aber NICHT auf Exchanges
    
    Läuft alle 5 Minuten im Logic Loop.
    """
    try:
        logger.debug("🧹 Running zombie cleanup...")

        # ===== 1. HOLE EXCHANGE POSITIONEN =====
        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )

        # Build Per-Exchange Maps
        x10_symbols = set()
        lighter_symbols = set()
        x10_pos_map = {}
        lighter_pos_map = {}

        for p in x10_positions:
            if abs(p.get('size', 0)) > 1e-8:
                sym = p['symbol']
                x10_symbols.add(sym)
                x10_pos_map[sym] = p

        for p in lighter_positions:
            if abs(p.get('size', 0)) > 1e-8:
                sym = p['symbol']
                lighter_symbols.add(sym)
                lighter_pos_map[sym] = p

        # ===== 2. HOLE DB TRADES =====
        db_trades = await get_open_trades()
        db_symbols = {t['symbol'] for t in db_trades}

        logger.debug(
            f"📊 Status: DB={len(db_symbols)}, X10={len(x10_symbols)}, Lighter={len(lighter_symbols)}"
        )

        # ===== 3. DETECT DIFFERENT ZOMBIE TYPES =====
        
        # Type 1: HALF-OPEN ZOMBIES (kritischste!)
        # Position nur auf EINER Exchange (andere Seite fehlt oder ist zu)
        half_open_x10_only = x10_symbols - lighter_symbols  # Nur X10, Lighter fehlt
        half_open_lighter_only = lighter_symbols - x10_symbols  # Nur Lighter, X10 fehlt
        
        half_open_zombies = half_open_x10_only | half_open_lighter_only
        
        # Type 2: FULL EXCHANGE ZOMBIES
        # Position auf BEIDEN Exchanges, aber NICHT in DB
        both_exchanges = x10_symbols & lighter_symbols
        full_zombies = both_exchanges - db_symbols
        
        # Type 3: DB ZOMBIES
        # In DB, aber NICHT auf BEIDEN Exchanges
        db_zombies = db_symbols - both_exchanges

        # ===== 4. HANDLE HALF-OPEN ZOMBIES (Priority 1!) =====
        if half_open_zombies:
            logger.warning(f"⚠️ HALF-OPEN ZOMBIES DETECTED: {half_open_zombies}")
            
            for symbol in half_open_zombies:
                logger.info(f"🧹 Cleaning half-open zombie: {symbol}")
                
                # Close X10 if exists
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
                
                # Close Lighter if exists
                if symbol in lighter_pos_map:
                    pos = lighter_pos_map[symbol]
                    size = pos['size']
                    price = lighter.fetch_mark_price(symbol) or 0
                    notional = abs(size * price)

                    if notional > 0:
                        original_side = "BUY" if size > 0 else "SELL"
                        logger.info(f"   Closing Lighter {symbol}: {size:.6f} ({original_side}), Notional: ${notional:.2f}")

                        try:
                            success, oid = await lighter.close_live_position(
                                symbol, original_side, notional
                            )
                            if success:
                                logger.info(f"   ✅ Lighter {symbol} close submitted: {oid or 'OK'}")
                                if oid:
                                    asyncio.create_task(process_fee_update(lighter, symbol, oid))
                                
                                # ===== 🆕 VERIFY CLOSE SUCCESS (wait 3s) =====
                                await asyncio.sleep(3)
                                
                                # Re-fetch positions
                                try:
                                    lighter_verify_positions = await lighter.fetch_open_positions()
                                    still_open = any(
                                        p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 
                                        for p in (lighter_verify_positions or [])
                                    )
                                    
                                    if still_open:
                                        logger.warning(
                                            f"   ⚠️ Lighter {symbol} still open after close attempt! "
                                            f"Order may have failed. Check UI."
                                        )
                                    else:
                                        logger.info(f"   ✅✅ Lighter {symbol} verified closed")
                                        
                                except Exception as verify_err:
                                    logger.warning(f"   ⚠️ Could not verify {symbol} close: {verify_err}")
                            else:
                                logger.warning(f"   ⚠️ Lighter {symbol} close failed")
                        except Exception as e:
                            logger.error(f"   ❌ Lighter {symbol} close error: {e}")
                
                # Rate limiting
                await asyncio.sleep(0.5)

                # 🆕 VERIFY ALL POSITIONS CLOSED
                await asyncio.sleep(3)
                x10_verify, lighter_verify = await get_cached_positions(
                    lighter, x10, force_refresh=True
                )

                remaining = set()
                for p in (x10_verify or []) + (lighter_verify or []):
                    if abs(p.get('size', 0)) > 1e-8:
                        remaining.add(p['symbol'])

                if remaining & half_open_zombies:
                    logger.error(f"❌ Still open after cleanup: {remaining & half_open_zombies}")

            logger.info(f"✅ Half-open zombie cleanup: {len(half_open_zombies)} cleaned")

        # ===== 5. HANDLE FULL EXCHANGE ZOMBIES =====
        if full_zombies:
            logger.warning(f"🧟 FULL EXCHANGE ZOMBIES (not in DB): {full_zombies}")
            
            for symbol in full_zombies:
                logger.info(f"🧹 Cleaning full zombie: {symbol}")

                # Close both sides
                # X10
                if symbol in x10_pos_map:
                    pos = x10_pos_map[symbol]
                    size = pos['size']
                    price = x10.fetch_mark_price(symbol) or 0
                    notional = abs(size * price)

                    if notional > 0:
                        original_side = "BUY" if size > 0 else "SELL"
                        try:
                            success, oid = await x10.close_live_position(
                                symbol, original_side, notional
                            )
                            if success:
                                logger.info(f"   ✅ X10 {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(x10, symbol, oid))
                        except Exception as e:
                            logger.error(f"   ❌ X10 {symbol}: {e}")
                
                # Lighter
                if symbol in lighter_pos_map:
                    pos = lighter_pos_map[symbol]
                    size = pos['size']
                    price = lighter.fetch_mark_price(symbol) or 0
                    notional = abs(size * price)

                    if notional > 0:
                        original_side = "BUY" if size > 0 else "SELL"
                        try:
                            success, oid = await lighter.close_live_position(
                                symbol, original_side, notional
                            )
                            if success:
                                logger.info(f"   ✅ Lighter {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(lighter, symbol, oid))
                        except Exception as e:
                            logger.error(f"   ❌ Lighter {symbol}: {e}")
                
                await asyncio.sleep(0.5)
            
            logger.info(f"✅ Full zombie cleanup: {len(full_zombies)} cleaned")

        # ===== 6. HANDLE DB ZOMBIES =====
        if db_zombies:
            logger.warning(f"💾 DB ZOMBIES (closed on exchanges): {db_zombies}")

            async with aiosqlite.connect(config.DB_FILE) as conn:
                for symbol in db_zombies:
                    async with conn.execute(
                        "SELECT entry_time, notional_usd FROM trades WHERE symbol = ? AND status = 'OPEN'",
                        (symbol,)
                    ) as cursor:
                        row = await cursor.fetchone()

                    if row:
                        entry_time, notional = row
                        logger.info(
                            f"   Closing DB zombie: {symbol} "
                            f"(${notional:.2f}, entered {entry_time})"
                        )

                    await conn.execute(
                        "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'",
                        (symbol,)
                    )

                await conn.commit()

            logger.info(f"✅ DB zombie cleanup: {len(db_zombies)} marked CLOSED")

            # Archive
            for symbol in db_zombies:
                trade_data = next((t for t in db_trades if t['symbol'] == symbol), None)
                if trade_data:
                    await archive_trade_to_history(
                        trade_data,
                        close_reason="Zombie Cleanup (closed manually on exchanges)",
                        pnl_data={
                            'total_net_pnl': 0.0,
                            'funding_pnl': 0.0,
                            'spread_pnl': 0.0,
                            'fees': 0.0
                        }
                    )

        # ===== 7. SUMMARY =====
        if not half_open_zombies and not full_zombies and not db_zombies:
            logger.debug("✅ No zombies found - all clean")
        else:
            total = len(half_open_zombies) + len(full_zombies) + len(db_zombies)
            logger.info(
                f"🎉 Zombie cleanup complete: {total} total "
                f"(Half-Open: {len(half_open_zombies)}, "
                f"Full: {len(full_zombies)}, "
                f"DB: {len(db_zombies)})"
            )

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
        db_trades = await get_open_trades()
        if not db_trades:
            logger.info("✅ No open trades in DB - sync not needed")
            return
    
        db_symbols = {t['symbol'] for t in db_trades}
        logger.info(f"📚 DB claims {len(db_symbols)} open: {db_symbols}")
    
        # ===== USE CACHED POSITIONS MIT FORCE REFRESH =====
        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )
    
        exchange_symbols = set()
        for p in x10_positions + lighter_positions:
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
    
    open_farm = [t for t in await get_open_trades() if t.get('is_farm_trade')]
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

        # Skip if min_notional > MAX_TRADE_SIZE
        if lighter.min_notional_usd(s) > config.MAX_TRADE_SIZE_USD:
            continue

        # ✅ BLACKLIST CHECK
        if s in config.BLACKLIST_SYMBOLS:
            reasons["blacklist"] += 1
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
    """
    🔄 PRODUCTION-GRADE: Funding Rate Poller mit Exponential Backoff
    
    Flow:
    1. Load Markets (mit Retry)
    2. Load Funding Rates (mit Retry)
    3. Save zu DB
    4. Bei Erfolg → Sleep 300s
    5. Bei 429 Error → Exponential Backoff (5s → 10s → 20s → 60s → 300s)
    """
    logger.info("🢢 Slow Poller (Funding Rates) aktiv.")
    
    retry_delay = 5  # Start bei 5s
    max_delay = 300  # Cap bei 5 Min (= normaler Poll Interval)
    success_sleep = 300  # Normal Poll Interval
    
    while True:
        try:
            # ===== 1. LOAD MARKETS (mit Retry) =====
            markets_loaded = False
            for market_retry in range(3):
                try:
                    await x10.load_market_cache(force=True)
                    await lighter.load_market_cache(force=True)
                    markets_loaded = True
                    break
                except Exception as market_err:
                    if "429" in str(market_err) and market_retry < 2:
                        wait = (market_retry + 1) * 2  # 2s, 4s
                        logger.warning(f"⚠️ Market Cache 429. Retry {market_retry+1}/3 in {wait}s...")
                        await asyncio.sleep(wait)
                    elif market_retry == 2:
                        logger.error(f"❌ Market Cache failed after 3 retries: {market_err}")
                        raise market_err
            
            if not markets_loaded:
                raise Exception("Market Cache Load failed")
            
            # ===== 2. LOAD FUNDING RATES (mit Smart Retry) =====
            await asyncio.sleep(2.0)  # Rate Limit Safety Gap
            
            funding_loaded = False
            for funding_retry in range(3):
                try:
                    await lighter.load_funding_rates_and_prices()
                    funding_loaded = True
                    break
                except Exception as funding_err:
                    error_str = str(funding_err).lower()
                    
                    if "429" in error_str or "rate limit" in error_str:
                        if funding_retry < 2:
                            # Exponential Backoff für Funding
                            wait = (funding_retry + 1) * 5  # 5s, 10s
                            logger.warning(
                                f"⚠️ Lighter Funding Rate Limit. "
                                f"Retry {funding_retry+1}/3 in {wait}s..."
                            )
                            await asyncio.sleep(wait)
                        else:
                            # Nach 3 Versuchen → Nutze Cache
                            logger.warning(
                                f"⚠️ Lighter Funding fetch failed after 3 retries. "
                                f"Using cached rates."
                            )
                            funding_loaded = True  # Setze auf True, um fortzufahren
                            break
                    else:
                        # Anderer Error → Propagate
                        raise funding_err
            
            # ===== 3. SAVE TO DB =====
            if funding_loaded:
                await save_current_funding_rates(lighter, x10)
            
            # ===== 4. SUCCESS → RESET BACKOFF =====
            if markets_loaded and funding_loaded:
                retry_delay = 5  # Reset Backoff
                logger.debug(f"✅ Funding Poll Success. Next poll in {success_sleep}s")
                await asyncio.sleep(success_sleep)
            else:
                # Partial Success → Kurzer Sleep
                await asyncio.sleep(30)
        
        except asyncio.CancelledError:
            logger.info("🛑 Funding Poller stopped (CancelledError)")
            break
        
        except Exception as e:
            error_str = str(e).lower()
            
            # ===== 5. ERROR HANDLING MIT BACKOFF =====
            if "429" in error_str or "rate limit" in error_str:
                # Exponential Backoff
                retry_delay = min(retry_delay * 2, max_delay)
                logger.warning(
                    f"⏸️ Rate Limited! Backing off: {retry_delay}s "
                    f"(max: {max_delay}s)"
                )
            else:
                # Anderer Error → Moderate Backoff
                retry_delay = min(30, max_delay)
                logger.error(f"❌ Funding Poll Error: {e}")
            
            await asyncio.sleep(retry_delay)

async def logic_loop(lighter, x10, price_event: asyncio.Event):
    """Event-Driven Logic Loop"""
    logger.info("🧠 Logic Loop (Event-Driven) aktiv.")
    
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0
    last_opportunity_scan = 0
    last_max_trades_warning = 0  # ✅ NEU
    
    SCAN_COOLDOWN_SECONDS = 0.5  # Faster scanning with non-blocking execution

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
            trades = await get_open_trades()
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

            # 7. Task Cleanup
            lock = get_task_lock()
            if lock:
                await cleanup_finished_tasks()

            trade_executed = False

            # Skip execution if no opportunities
            if not opps:
                continue

            for opp in opps:
                if current_open_count >= config.MAX_OPEN_TRADES:
                    break
                
                symbol = opp['symbol']
                
                lock = get_task_lock()
                    
                async with lock:
                    if symbol in ACTIVE_TASKS:
                        logger.debug(f"{symbol} already has active task, skipping")
                        continue
                
                # Create non-blocking task
                task = asyncio.create_task(execute_trade_task(opp, lighter, x10))
                
                async with lock:
                    ACTIVE_TASKS[symbol] = task
                
                trade_executed = True
                current_open_count += 1
                open_symbols.add(symbol)
                
                logger.info(f"🚀 {symbol} execution started (non-blocking)")
                await asyncio.sleep(0.1)  # Small gap between launches
            
            # ✅ RATE LIMIT PROTECTION
            if trade_executed:
                await asyncio.sleep(2)

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
    global SHUTDOWN_FLAG, state_manager
    logger.info("🚀 BOT V3 (Event-Driven Edition) START")
    # Initialize in-memory state manager
    state_manager = InMemoryStateManager(config.DB_FILE)
    await state_manager.start()
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_update_event = asyncio.Event()

    x10.price_update_event = price_update_event
    lighter.price_update_event = price_update_event

    await setup_database()
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    
    logger.info("🔄 Syncing DB with exchanges...")
    await sync_db_with_exchanges(lighter, x10)
    
    logger.info("📡 Starting WebSockets and Logic Loop...")
    # ✅ LOAD FUNDING RATES BEFORE STARTING LOOPS
    await lighter.load_funding_rates_and_prices()
    
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
        logger.info("💾 Flushing state to DB...")
        if state_manager:
            await state_manager.stop()
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
        db_trades = await get_open_trades()
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
    
    open_farm = [t for t in await get_open_trades() if t.get('is_farm_trade')]
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
        
        # ✅ BLACKLIST CHECK
        if s in config.BLACKLIST_SYMBOLS:
            reasons["blacklist"] += 1
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
    """
    🔄 PRODUCTION-GRADE: Funding Rate Poller mit Exponential Backoff
    
    Flow:
    1. Load Markets (mit Retry)
    2. Load Funding Rates (mit Retry)
    3. Save zu DB
    4. Bei Erfolg → Sleep 300s
    5. Bei 429 Error → Exponential Backoff (5s → 10s → 20s → 60s → 300s)
    """
    logger.info("🢢 Slow Poller (Funding Rates) aktiv.")
    
    retry_delay = 5  # Start bei 5s
    max_delay = 300  # Cap bei 5 Min (= normaler Poll Interval)
    success_sleep = 300  # Normal Poll Interval
    
    while True:
        try:
            # ===== 1. LOAD MARKETS (mit Retry) =====
            markets_loaded = False
            for market_retry in range(3):
                try:
                    await x10.load_market_cache(force=True)
                    await lighter.load_market_cache(force=True)
                    markets_loaded = True
                    break
                except Exception as market_err:
                    if "429" in str(market_err) and market_retry < 2:
                        wait = (market_retry + 1) * 2  # 2s, 4s
                        logger.warning(f"⚠️ Market Cache 429. Retry {market_retry+1}/3 in {wait}s...")
                        await asyncio.sleep(wait)
                    elif market_retry == 2:
                        logger.error(f"❌ Market Cache failed after 3 retries: {market_err}")
                        raise market_err
            
            if not markets_loaded:
                raise Exception("Market Cache Load failed")
            
            # ===== 2. LOAD FUNDING RATES (mit Smart Retry) =====
            await asyncio.sleep(2.0)  # Rate Limit Safety Gap
            
            funding_loaded = False
            for funding_retry in range(3):
                try:
                    await lighter.load_funding_rates_and_prices()
                    funding_loaded = True
                    break
                except Exception as funding_err:
                    error_str = str(funding_err).lower()
                    
                    if "429" in error_str or "rate limit" in error_str:
                        if funding_retry < 2:
                            # Exponential Backoff für Funding
                            wait = (funding_retry + 1) * 5  # 5s, 10s
                            logger.warning(
                                f"⚠️ Lighter Funding Rate Limit. "
                                f"Retry {funding_retry+1}/3 in {wait}s..."
                            )
                            await asyncio.sleep(wait)
                        else:
                            # Nach 3 Versuchen → Nutze Cache
                            logger.warning(
                                f"⚠️ Lighter Funding fetch failed after 3 retries. "
                                f"Using cached rates."
                            )
                            funding_loaded = True  # Setze auf True, um fortzufahren
                            break
                    else:
                        # Anderer Error → Propagate
                        raise funding_err
            
            # ===== 3. SAVE TO DB =====
            if funding_loaded:
                await save_current_funding_rates(lighter, x10)
            
            # ===== 4. SUCCESS → RESET BACKOFF =====
            if markets_loaded and funding_loaded:
                retry_delay = 5  # Reset Backoff
                logger.debug(f"✅ Funding Poll Success. Next poll in {success_sleep}s")
                await asyncio.sleep(success_sleep)
            else:
                # Partial Success → Kurzer Sleep
                await asyncio.sleep(30)
        
        except asyncio.CancelledError:
            logger.info("🛑 Funding Poller stopped (CancelledError)")
            break
        
        except Exception as e:
            error_str = str(e).lower()
            
            # ===== 5. ERROR HANDLING MIT BACKOFF =====
            if "429" in error_str or "rate limit" in error_str:
                # Exponential Backoff
                retry_delay = min(retry_delay * 2, max_delay)
                logger.warning(
                    f"⏸️ Rate Limited! Backing off: {retry_delay}s "
                    f"(max: {max_delay}s)"
                )
            else:
                # Anderer Error → Moderate Backoff
                retry_delay = min(30, max_delay)
                logger.error(f"❌ Funding Poll Error: {e}")
            
            await asyncio.sleep(retry_delay)

async def logic_loop(lighter, x10, price_event: asyncio.Event):
    """Event-Driven Logic Loop"""
    logger.info("🧠 Logic Loop (Event-Driven) aktiv.")
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0
    last_opportunity_scan = 0
    last_max_trades_warning = 0  # ✅ NEU
    SCAN_COOLDOWN_SECONDS = 0.5  # Faster scanning with non-blocking execution

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

            trades = await get_open_trades()
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

            # 7. Execute Trades (Non-Blocking)
            await cleanup_finished_tasks()
            
            trade_executed = False
            for opp in opps:
                if current_open_count >= config.MAX_OPEN_TRADES:
                    break
                
                symbol = opp['symbol']
                
                # Check if already executing
                async with TASK_LOCK:
                    if symbol in ACTIVE_TASKS:
                        logger.debug(f"{symbol} already has active task, skipping")
                        continue
                
                # Create non-blocking task
                task = asyncio.create_task(execute_trade_task(opp, lighter, x10))
                
                async with TASK_LOCK:
                    ACTIVE_TASKS[symbol] = task
                
                trade_executed = True
                current_open_count += 1
                open_symbols.add(symbol)
                
                logger.info(f"🚀 {symbol} execution started (non-blocking)")
                await asyncio.sleep(0.1)  # Small gap between launches

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
    global SHUTDOWN_FLAG, state_manager
    logger.info("🚀 BOT V3 (Event-Driven Edition) START")
    # Initialize in-memory state manager
    state_manager = InMemoryStateManager(config.DB_FILE)
    await state_manager.start()
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
        logger.info("💾 Flushing state to DB...")
        if state_manager:
            await state_manager.stop()
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