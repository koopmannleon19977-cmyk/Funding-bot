# monitor_funding.py
import sys
import os
import time
import asyncio
import aiosqlite
import logging
import random
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any
from decimal import Decimal

# ============================================================
# GLOBALS
# ============================================================
FAILED_COINS = {}
state_manager = None
parallel_exec = None
OPPORTUNITY_LOG_CACHE = {}
OPPORTUNITY_LOG_COOLDOWN = 60
ACTIVE_TASKS = {}
SHUTDOWN_FLAG = False

POSITION_CACHE = {
    'x10': [],
    'lighter': [],
    'last_update': 0.0
}
POSITION_CACHE_TTL = 15

EXECUTION_LOCKS: Dict[str, asyncio.Lock] = {}
LOCK_MANAGER_LOCK = asyncio.Lock()

POSITION_CACHE_LOCK = None
TASK_LOCK = None

# ============================================================
# IMPORTS
# ============================================================
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction import predict_next_funding_rates, calculate_smart_size
from src.prediction_v2 import get_predictor
from src.latency_arb import get_detector
from src.state_manager import InMemoryStateManager
from src.parallel_execution import ParallelExecutionManager

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = config.setup_logging()
config.validate_runtime_config(logger)

log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
file_handler = logging.FileHandler(str(log_dir / "bot.log"))
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(file_handler)

# ============================================================
# LOCK HELPERS
# ============================================================
def get_position_cache_lock() -> asyncio.Lock:
    global POSITION_CACHE_LOCK
    if POSITION_CACHE_LOCK is None:
        POSITION_CACHE_LOCK = asyncio.Lock()
    return POSITION_CACHE_LOCK

def get_task_lock() -> asyncio.Lock:
    global TASK_LOCK
    if TASK_LOCK is None:
        TASK_LOCK = asyncio.Lock()
    return TASK_LOCK

async def get_execution_lock(symbol: str) -> asyncio.Lock:
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]

# ============================================================
# HELPERS
# ============================================================
def is_tradfi_or_fx(symbol: str) -> bool:
    s = symbol.upper().replace("-USD", "").replace("/", "")
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")):
        return True
    fx_currencies = ("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")
    if s.startswith(fx_currencies) and "EUROC" not in s:
        return True
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")):
        return True
    return False

def cleanup_finished_tasks():
    finished = [sym for sym, task in ACTIVE_TASKS.items() if task.done()]
    for sym in finished:
        del ACTIVE_TASKS[sym]
    if finished:
        logger.debug(f"Cleaned up {len(finished)} finished tasks")

async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    symbol = opp['symbol']
    try:
        return await execute_trade(opp, lighter, x10)
    finally:
        lock = get_task_lock()
        async with lock:
            if symbol in ACTIVE_TASKS:
                del ACTIVE_TASKS[symbol]

# ============================================================
# DATABASE
# ============================================================
async def setup_database():
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
        logger.info(" DB-Setup bereit.")

async def update_fee_stats(exchange_name: str, symbol: str, fee_rate: float):
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
            
            if fee_rate > 0:
                logger.info(
                    f" FEE UPDATE: {exchange_name} {symbol} -> "
                    f"{fee_rate*100:.4f}% (Avg: {new_avg*100:.4f}%, n={new_count})"
                )
            else:
                logger.debug(
                    f" FEE UPDATE: {exchange_name} {symbol} -> "
                    f"0.0000% (Maker/Free) (n={new_count})"
                )
    except Exception as e:
        logger.error(f"Fee DB Error: {e}")

async def process_fee_update(adapter, symbol, order_id):
    if not order_id:
        return
    
    await asyncio.sleep(2)
    
    max_retries = 2
    retry_delay = 5
    timeout = 15
    start_time = time.time()
    
    for attempt in range(max_retries + 1):
        try:
            if time.time() - start_time > timeout:
                break
            fee = await adapter.get_order_fee(order_id)
            
            if fee > 0 or attempt == max_retries:
                await update_fee_stats(adapter.name, symbol, fee)
                return
            
            if attempt < max_retries:
                logger.debug(
                    f" {adapter.name} {symbol}: Fee noch 0%, "
                    f"Retry {attempt+1}/{max_retries} in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
                continue
            
            await update_fee_stats(adapter.name, symbol, 0.0)
            return
            
        except Exception as e:
            logger.error(f" Fee Update Error {adapter.name} {symbol}: {e}")
            
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                fallback_fee = (
                    config.FEES_LIGHTER if adapter.name == "Lighter" 
                    else config.TAKER_FEE_X10
                )
                await update_fee_stats(adapter.name, symbol, fallback_fee)
                return

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
                await conn.executemany(
                    "INSERT OR IGNORE INTO funding_history (symbol, timestamp, lighter_rate, x10_rate) VALUES (?, ?, ?, ?)",
                    rows
                )
                await conn.commit()
    except Exception as e:
        logger.error(f"DB Error Save Rates: {e}")

# ============================================================
# STATE MANAGEMENT
# ============================================================
async def get_open_trades() -> List[Dict]:
    global state_manager
    if state_manager:
        res = state_manager.get_open_trades()
        if asyncio.iscoroutine(res):
            return await res
        return res

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
            logger.info(f" DB SAVE: {td['symbol']} als OPEN gespeichert.")
    except Exception as e:
        logger.error(f"DB Write Error: {e}")
        raise e

async def update_trade_status(symbol: str, status: str):
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("UPDATE trades SET status = ? WHERE symbol = ?", (status, symbol))
        await conn.commit()
    logger.info(f"DB: Trade {symbol} status updated to {status}")

async def add_trade_to_state(trade_data: Dict):
    global state_manager
    if state_manager:
        if 'initial_funding_rate_hourly' not in trade_data:
            trade_data['initial_funding_rate_hourly'] = trade_data.get('net_funding_hourly', 0.0)
        maybe = state_manager.add_trade(trade_data)
        if asyncio.iscoroutine(maybe):
            await maybe
    else:
        await write_detailed_trade_to_db(trade_data)

async def close_trade_in_state(symbol: str):
    global state_manager
    if state_manager:
        maybe = state_manager.close_trade(symbol)
        if asyncio.iscoroutine(maybe):
            await maybe
    else:
        await update_trade_status(symbol, 'CLOSED')

async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
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
            logger.info(f" PnL: {trade_data['symbol']} | ${pnl_data['total_net_pnl']:.2f}")
    except Exception as e:
        logger.error(f"DB History Error: {e}")

# ============================================================
# TRADE EXECUTION
# ============================================================
async def close_trade(trade: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    symbol = trade['symbol']
    notional = trade['notional_usd']
    side = "BUY" if (trade['leg1_exchange'] == 'X10' and trade.get('initial_funding_rate_hourly', 0) < 0) else "SELL"
    
    a1, a2 = (x10, lighter) if trade['leg1_exchange'] == 'X10' else (lighter, x10)
    logger.info(f" Closing {symbol}...")
    
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
        logger.info(f" {symbol} Closed.")
        return True
    else:
        logger.warning(f" {symbol} Close Incomplete. A1:{s1}, A2:{s2}")
        return False

async def execute_trade_parallel(opp: Dict, lighter: LighterAdapter, x10: X10Adapter) -> bool:
    if SHUTDOWN_FLAG:
        return False

    global parallel_exec
    if not parallel_exec:
        logger.error("ParallelExecutionManager not initialized")
        return False

    symbol = opp['symbol']
    lock = await get_execution_lock(symbol)

    if lock.locked():
        logger.debug(f"{symbol} already executing, skip duplicate")
        return False

    async with lock:
        logger.debug(f"{symbol} execution lock acquired")

        existing_trades = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing_trades):
            logger.info(f"{symbol} already open (post-lock check)")
            return False

        if symbol in FAILED_COINS:
            if time.time() - FAILED_COINS[symbol] < 300:
                logger.debug(f"{symbol} blacklisted")
                return False
            else:
                del FAILED_COINS[symbol]

        if 'is_farm_trade' not in opp:
            opp['is_farm_trade'] = False

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

        predictor = get_predictor()
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        
        current_rate_lighter = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        current_net_rate = current_rate_lighter - current_rate_x10
        
        predicted_rate, rate_delta, confidence = await predictor.predict_next_funding_rate(
            symbol=symbol,
            current_rate=abs(current_net_rate),
            lighter_adapter=lighter,
            x10_adapter=x10,
            btc_trend_pct=btc_trend
        )
        
        if not opp.get('is_farm_trade'):
            if predicted_rate < abs(current_net_rate) * 0.8:
                logger.info(
                    f" {symbol} SKIP: Predicted rate drop "
                    f"({abs(current_net_rate):.6f} -> {predicted_rate:.6f}, "
                    f"conf={confidence:.2f})"
                )
                return False

        min_x10 = x10.min_notional_usd(symbol)
        min_lit = lighter.min_notional_usd(symbol)
        absolute_min_required = max(min_x10, min_lit)

        if absolute_min_required > config.MAX_TRADE_SIZE_USD:
            logger.debug(f"Skip {symbol}: Min ${absolute_min_required:.2f} > Max ${config.MAX_TRADE_SIZE_USD:.2f}")
            return False

        if opp.get('is_farm_trade'):
            target_usd = opp.get('notional_usd', config.FARM_NOTIONAL_USD)
        else:
            target_usd = calculate_smart_size(confidence, total_balance)

        final_usd = max(target_usd, absolute_min_required)
        final_usd = min(final_usd, config.MAX_TRADE_SIZE_USD)

        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        bal1 = bal_x10 if ex1.name == 'X10' else bal_lit
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        bal2 = bal_lit if ex2.name == 'Lighter' else bal_x10

        min_required_per_exchange = final_usd * 1.10

        if bal1 < min_required_per_exchange or bal2 < min_required_per_exchange:
            logger.debug(f"{symbol}: Insufficient balance")
            return False

        p_x, p_l = x10.fetch_mark_price(symbol), lighter.fetch_mark_price(symbol)
        if not p_x or not p_l:
            logger.warning(f"{symbol}: Missing price data")
            return False

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
            f" PARALLEL EXEC {symbol} ${final_usd:.2f} | "
            f"{ex1.name} {side1} + {ex2.name} {side2} | "
            f"Confidence: {confidence:.2f} | Predicted : {rate_delta:+.6f}"
        )

        success, x10_order_id, lighter_order_id = await parallel_exec.execute_trade_parallel(
            symbol,
            side1 if ex1.name == 'X10' else side2,
            side2 if ex1.name == 'X10' else side1,
            Decimal(str(final_usd)),
            Decimal(str(final_usd)),
            None,
            None
        )

        if not success:
            logger.warning(f"[EXEC] Parallel execution failed for {symbol}")
            FAILED_COINS[symbol] = time.time()
            return False

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

        order_id1 = x10_order_id if ex1.name == 'X10' else lighter_order_id
        order_id2 = lighter_order_id if ex1.name == 'X10' else x10_order_id

        if order_id1:
            asyncio.create_task(process_fee_update(ex1, symbol, order_id1))
        if order_id2:
            asyncio.create_task(process_fee_update(ex2, symbol, order_id2))

        return True

execute_trade = execute_trade_parallel

# ============================================================
# POSITION MANAGEMENT
# ============================================================
async def get_cached_positions(lighter, x10, force_refresh: bool = False):
    async with get_position_cache_lock():
        now = time.time()

        if not force_refresh and (now - POSITION_CACHE['last_update']) < POSITION_CACHE_TTL:
            logger.debug(
                f"Using cached positions (age: {now - POSITION_CACHE['last_update']:.1f}s)"
            )
            return POSITION_CACHE['x10'], POSITION_CACHE['lighter']

        try:
            logger.debug("Refreshing position cache...")

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

            POSITION_CACHE['x10'] = x10_positions or []
            POSITION_CACHE['lighter'] = lighter_positions or []
            POSITION_CACHE['last_update'] = now

            logger.debug(
                f"Position cache updated: X10={len(POSITION_CACHE['x10'])}, Lighter={len(POSITION_CACHE['lighter'])}"
            )

            return POSITION_CACHE['x10'], POSITION_CACHE['lighter']

        except Exception as e:
            logger.error(f"Position cache refresh error: {e}")
            return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])

async def manage_open_trades(lighter, x10):
    trades = await get_open_trades()
    if not trades:
        return

    try:
        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )

        cache_age = time.time() - POSITION_CACHE['last_update']
        should_reconcile = cache_age < 5.0

        if not should_reconcile:
            logger.debug(
                f" Skipping reconciliation (cache age: {cache_age:.1f}s > 5s). "
                f"Will check on next refresh."
            )
            exchange_symbols = None
        else:
            exchange_symbols = set()
            for p in x10_positions + lighter_positions:
                if abs(p.get('size', 0)) > 1e-8:
                    exchange_symbols.add(p['symbol'])

    except Exception as e:
        logger.warning(f" Position fetch error in manage_open_trades: {e}")
        exchange_symbols = None

    manually_closed = []

    if exchange_symbols is not None and should_reconcile:
        for t in trades:
            sym = t['symbol']
            if sym not in exchange_symbols:
                logger.warning(
                    f" RECONCILE: {sym} in DB aber nicht auf Exchange! "
                    f"(Cache age: {cache_age:.1f}s)"
                )
                manually_closed.append(t)
    
    for t in manually_closed:
        sym = t['symbol']
        
        logger.info(f" Closing manually closed position in DB: {sym}")
        
        await close_trade_in_state(sym)
        
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
        
        logger.info(f" {sym} DB cleanup complete. Slot freed for new trade.")
    
    active_trades = [t for t in trades if t not in manually_closed]
    
    for t in active_trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        rx, rl = x10.fetch_funding_rate(sym), lighter.fetch_funding_rate(sym)
        
        if not all([px, pl, rx is not None, rl is not None]):
            continue
        
        net = rl - rx
        hold_hours = (datetime.utcnow() - t['entry_time']).total_seconds() / 3600
        
        funding_pnl = net * hold_hours * t['notional_usd']
        
        entry_spread = abs(t['entry_price_x10'] - t['entry_price_lighter'])
        current_spread = abs(px - pl)
        spread_change = entry_spread - current_spread
        spread_pnl = spread_change / t['entry_price_x10'] * t['notional_usd']
        
        estimated_fees = t['notional_usd'] * 0.0012
        
        total_pnl = funding_pnl + spread_pnl - estimated_fees
        
        reason = None
        
        if t.get('is_farm_trade'):
            hold_seconds = (datetime.utcnow() - t['entry_time']).total_seconds()
            if hold_seconds > config.FARM_HOLD_SECONDS:
                reason = "Farm Done"
        
        elif total_pnl < -t['notional_usd'] * 0.02:
            reason = "Stop Loss"
            logger.warning(
                f" {sym} Stop Loss triggered! "
                f"PnL: ${total_pnl:.2f} ({total_pnl/t['notional_usd']*100:.2f}%)"
            )
        
        elif total_pnl > t['notional_usd'] * 0.03:
            reason = "Take Profit"
            logger.info(
                f" {sym} Take Profit triggered! "
                f"PnL: ${total_pnl:.2f} ({total_pnl/t['notional_usd']*100:.2f}%)"
            )
        
        elif t.get('initial_funding_rate_hourly') is not None:
            initial_net = t['initial_funding_rate_hourly']
            
            if (initial_net > 0 and net < 0) or (initial_net < 0 and net > 0):
                if not t.get('funding_flip_start_time'):
                    logger.info(
                        f" {sym} Funding Flipped! "
                        f"Initial: {initial_net:.6f} -> Current: {net:.6f}"
                    )
                    
                    async with aiosqlite.connect(config.DB_FILE) as conn:
                        await conn.execute(
                            "UPDATE trades SET funding_flip_start_time = ? WHERE symbol = ?",
                            (datetime.utcnow(), sym)
                        )
                        await conn.commit()
                    
                    t['funding_flip_start_time'] = datetime.utcnow()
                
                elif t.get('funding_flip_start_time'):
                    flip_hours = (datetime.utcnow() - t['funding_flip_start_time']).total_seconds() / 3600
                    
                    if flip_hours > config.FUNDING_FLIP_HOURS_THRESHOLD:
                        reason = f"Funding Flipped {flip_hours:.1f}h"
                        logger.info(
                            f" {sym} Closing due to prolonged funding flip "
                            f"({flip_hours:.1f}h > {config.FUNDING_FLIP_HOURS_THRESHOLD}h)"
                        )
        
        if not reason and hold_hours > (config.DYNAMIC_HOLD_MAX_DAYS * 24):
            reason = f"Max Hold ({hold_hours/24:.1f}d)"
        
        if reason:
            logger.info(f" Closing {sym} (Reason: {reason})")
            
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
                
                POSITION_CACHE['last_update'] = 0.0
                logger.debug(f" Position cache invalidated after {sym} close")

async def cleanup_zombie_positions(lighter, x10):
    try:
        logger.debug(" Running zombie cleanup...")

        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )

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

        db_trades = await get_open_trades()
        db_symbols = {t['symbol'] for t in db_trades}

        logger.debug(
            f" Status: DB={len(db_symbols)}, X10={len(x10_symbols)}, Lighter={len(lighter_symbols)}"
        )

        half_open_x10_only = x10_symbols - lighter_symbols
        half_open_lighter_only = lighter_symbols - x10_symbols
        
        half_open_zombies = half_open_x10_only | half_open_lighter_only
        
        both_exchanges = x10_symbols & lighter_symbols
        full_zombies = both_exchanges - db_symbols
        
        db_zombies = db_symbols - both_exchanges

        if half_open_zombies:
            logger.warning(f" HALF-OPEN ZOMBIES DETECTED: {half_open_zombies}")
            
            for symbol in half_open_zombies:
                logger.info(f" Cleaning half-open zombie: {symbol}")
                
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
                                logger.info(f"    X10 {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(x10, symbol, oid))
                            else:
                                logger.warning(f"    X10 {symbol} close failed")
                        except Exception as e:
                            logger.error(f"    X10 {symbol} close error: {e}")
                
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
                                logger.info(f"    Lighter {symbol} close submitted: {oid or 'OK'}")
                                if oid:
                                    asyncio.create_task(process_fee_update(lighter, symbol, oid))
                                
                                await asyncio.sleep(3)
                                
                                try:
                                    lighter_verify_positions = await lighter.fetch_open_positions()
                                    still_open = any(
                                        p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 
                                        for p in (lighter_verify_positions or [])
                                    )
                                    
                                    if still_open:
                                        logger.warning(
                                            f"    Lighter {symbol} still open after close attempt! "
                                            f"Order may have failed. Check UI."
                                        )
                                    else:
                                        logger.info(f"    Lighter {symbol} verified closed")
                                        
                                except Exception as verify_err:
                                    logger.warning(f"    Could not verify {symbol} close: {verify_err}")
                            else:
                                logger.warning(f"    Lighter {symbol} close failed")
                        except Exception as e:
                            logger.error(f"    Lighter {symbol} close error: {e}")
                
                await asyncio.sleep(0.5)

            logger.info(f" Half-open zombie cleanup: {len(half_open_zombies)} cleaned")

        if full_zombies:
            logger.warning(f" FULL EXCHANGE ZOMBIES (not in DB): {full_zombies}")
            
            for symbol in full_zombies:
                logger.info(f" Cleaning full zombie: {symbol}")

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
                                logger.info(f"    X10 {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(x10, symbol, oid))
                        except Exception as e:
                            logger.error(f"    X10 {symbol}: {e}")
                
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
                                logger.info(f"    Lighter {symbol} closed")
                                if oid:
                                    asyncio.create_task(process_fee_update(lighter, symbol, oid))
                        except Exception as e:
                            logger.error(f"    Lighter {symbol}: {e}")
                
                await asyncio.sleep(0.5)
            
            logger.info(f" Full zombie cleanup: {len(full_zombies)} cleaned")

        if db_zombies:
            logger.warning(f" DB ZOMBIES (closed on exchanges): {db_zombies}")

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

            logger.info(f" DB zombie cleanup: {len(db_zombies)} marked CLOSED")

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

        if not half_open_zombies and not full_zombies and not db_zombies:
            logger.debug(" No zombies found - all clean")
        else:
            total = len(half_open_zombies) + len(full_zombies) + len(db_zombies)
            logger.info(
                f" Zombie cleanup complete: {total} total "
                f"(Half-Open: {len(half_open_zombies)}, "
                f"Full: {len(full_zombies)}, "
                f"DB: {len(db_zombies)})"
            )

    except Exception as e:
        logger.error(f" Zombie cleanup error: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def sync_db_with_exchanges(lighter, x10):
    try:
        logger.info(" Syncing DB with Exchange positions...")
    
        db_trades = await get_open_trades()
        if not db_trades:
            logger.info(" No open trades in DB - sync not needed")
            return
    
        db_symbols = {t['symbol'] for t in db_trades}
        logger.info(f" DB claims {len(db_symbols)} open: {db_symbols}")
    
        x10_positions, lighter_positions = await get_cached_positions(
            lighter, x10, force_refresh=True
        )
    
        exchange_symbols = set()
        for p in x10_positions + lighter_positions:
            if abs(p.get('size', 0)) > 1e-8:
                exchange_symbols.add(p['symbol'])
    
        logger.info(f" Exchanges have {len(exchange_symbols)} open: {exchange_symbols}")
    
        ghost_trades = db_symbols - exchange_symbols
    
        if not ghost_trades:
            logger.info(" DB and Exchanges are in sync")
            return
    
        logger.warning(f" GHOST TRADES found (closed manually?): {ghost_trades}")
    
        async with aiosqlite.connect(config.DB_FILE) as conn:
            for symbol in ghost_trades:
                async with conn.execute(
                    "SELECT entry_time, notional_usd FROM trades WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                ) as cursor:
                    row = await cursor.fetchone()
            
                if row:
                    entry_time, notional = row
                    logger.info(f"   Closing ghost: {symbol} (${notional:.2f}, entered {entry_time})")
            
                await conn.execute(
                    "UPDATE trades SET status = 'CLOSED' WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,)
                )
        
            await conn.commit()
    
        logger.info(f" Closed {len(ghost_trades)} ghost trades in DB")
    
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
        logger.error(f" DB Sync Error: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ============================================================
# OPPORTUNITY DETECTION
# ============================================================
async def volume_farm_mode(lighter, x10, open_syms):
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
        
        logger.info(f" FARM START: {symbol} ${size:.1f}")
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

        if lighter.min_notional_usd(s) > config.MAX_TRADE_SIZE_USD:
            continue

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

        if apy > 0.5:
            last_log = OPPORTUNITY_LOG_CACHE.get(s, 0)
            if now - last_log > OPPORTUNITY_LOG_COOLDOWN:
                logger.info(
                    f" OPPORTUNITY: {s} | APY: {apy*100:.1f}% | "
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
        logger.info(f" Scan Report: {checked} Pairs geprft. Rejects: {reasons}")
        
    return opps[:config.MAX_OPEN_TRADES]

async def check_latency_arb(lighter, x10, open_syms):
    detector = get_detector()
    
    priority_pairs = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ARB-USD"]
    
    for symbol in priority_pairs:
        if symbol in open_syms:
            continue
        
        x10_rate = x10.fetch_funding_rate(symbol)
        lighter_rate = lighter.fetch_funding_rate(symbol)
        
        if x10_rate is None or lighter_rate is None:
            continue
        
        opp = await detector.detect_lag_opportunity(
            symbol=symbol,
            x10_rate=x10_rate,
            lighter_rate=lighter_rate,
            x10_adapter=x10,
            lighter_adapter=lighter
        )
        
        if opp:
            return opp
    
    return None

async def log_real_pnl(lighter, x10):
    try:
        x_bal = await x10.get_real_available_balance()
        l_bal = await lighter.get_real_available_balance()
        logger.info(f" BALANCE: ${x_bal + l_bal:.2f} (X10: ${x_bal:.0f} | Lit: ${l_bal:.0f})")
    except:
        pass

# ============================================================
# MAIN LOOPS
# ============================================================
async def slow_poll_funding(lighter, x10):
    logger.info(" Slow Poller (Funding Rates) aktiv.")
    
    retry_delay = 5
    max_delay = 300
    success_sleep = 300
    
    while True:
        try:
            markets_loaded = False
            for market_retry in range(3):
                try:
                    await x10.load_market_cache(force=True)
                    await lighter.load_market_cache(force=True)
                    markets_loaded = True
                    break
                except Exception as market_err:
                    if "429" in str(market_err) and market_retry < 2:
                        wait = (market_retry + 1) * 2
                        logger.warning(f" Market Cache 429. Retry {market_retry+1}/3 in {wait}s...")
                        await asyncio.sleep(wait)
                    elif market_retry == 2:
                        logger.error(f" Market Cache failed after 3 retries: {market_err}")
                        raise market_err
            
            if not markets_loaded:
                raise Exception("Market Cache Load failed")
            
            await asyncio.sleep(2.0)
            
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
                            wait = (funding_retry + 1) * 5
                            logger.warning(
                                f" Lighter Funding Rate Limit. "
                                f"Retry {funding_retry+1}/3 in {wait}s..."
                            )
                            await asyncio.sleep(wait)
                        else:
                            logger.warning(
                                f" Lighter Funding fetch failed after 3 retries. "
                                f"Using cached rates."
                            )
                            funding_loaded = True
                            break
                    else:
                        raise funding_err
            
            if funding_loaded:
                await save_current_funding_rates(lighter, x10)
            
            if markets_loaded and funding_loaded:
                retry_delay = 5
                logger.debug(f" Funding Poll Success. Next poll in {success_sleep}s")
                await asyncio.sleep(success_sleep)
            else:
                await asyncio.sleep(30)
        
        except asyncio.CancelledError:
            logger.info(" Funding Poller stopped (CancelledError)")
            break
        
        except Exception as e:
            error_str = str(e).lower()
            
            if "429" in error_str or "rate limit" in error_str:
                retry_delay = min(retry_delay * 2, max_delay)
                logger.warning(
                    f" Rate Limited! Backing off: {retry_delay}s "
                    f"(max: {max_delay}s)"
                )
            else:
                retry_delay = min(30, max_delay)
                logger.error(f" Funding Poll Error: {e}")
            
            await asyncio.sleep(retry_delay)

async def logic_loop(lighter, x10, price_event: asyncio.Event, parallel_exec):
    logger.info(" Logic Loop (Event-Driven) aktiv.")
    
    last_zombie_check = 0
    last_pnl_log = 0
    last_farm_check = 0
    last_opportunity_scan = 0
    last_max_trades_warning = 0
    
    SCAN_COOLDOWN_SECONDS = 0.5

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

            if current_open_count >= config.MAX_OPEN_TRADES:
                if now - last_max_trades_warning > 60:
                    logger.info(
                        f" Max Open Trades: {current_open_count}/{config.MAX_OPEN_TRADES}. "
                        f"Waiting for exit signals..."
                    )
                    last_max_trades_warning = now
                continue

            latency_opp = await check_latency_arb(lighter, x10, open_symbols)
            
            if latency_opp:
                logger.info(f" LATENCY ARB DETECTED: {latency_opp['symbol']}")
                task = asyncio.create_task(execute_trade_task(latency_opp, lighter, x10, parallel_exec))
                async with get_task_lock():
                    ACTIVE_TASKS[latency_opp['symbol']] = task
            
            opps = await find_opportunities(lighter, x10, open_symbols)

            cleanup_finished_tasks()

            trade_executed = False

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
                
                task = asyncio.create_task(execute_trade_task(opp, lighter, x10, parallel_exec))
                
                async with lock:
                    ACTIVE_TASKS[symbol] = task
                
                trade_executed = True
                current_open_count += 1
                open_symbols.add(symbol)
                
                logger.info(f" {symbol} execution started (non-blocking)")
                await asyncio.sleep(0.1)
            
            if trade_executed:
                await asyncio.sleep(2)

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
    global SHUTDOWN_FLAG, state_manager, parallel_exec
    logger.info(" BOT V3 (Event-Driven Edition) START")
    
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
    
    parallel_exec = ParallelExecutionManager(x10, lighter)

    t_ws_x10 = asyncio.create_task(x10.start_websocket())
    t_ws_lighter = asyncio.create_task(lighter.start_websocket())
    t_funding = asyncio.create_task(slow_poll_funding(lighter, x10))
    t_logic = asyncio.create_task(logic_loop(lighter, x10, price_update_event, parallel_exec))

    try:
        await asyncio.gather(t_ws_x10, t_ws_lighter, t_funding, t_logic)
    except asyncio.CancelledError:
        pass
    finally:
        SHUTDOWN_FLAG = True
        logger.info(" Flushing state to DB...")
        if state_manager:
            await state_manager.stop()
        logger.info(" SHUTDOWN: Closing connections...")
        await x10.aclose()
        await lighter.aclose()
        logger.info(" Closed.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass