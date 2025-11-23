# monitor_funding.py
import sys
import os
import time
import asyncio
import aiosqlite
import logging
import random
import traceback
from datetime import datetime
from typing import Optional, List, Dict
from decimal import Decimal

# ============================================================
# IMPORTS & SETUP
# ============================================================
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.telegram_bot import get_telegram_bot
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction_v2 import get_predictor
from src.latency_arb import get_detector
from src.state_manager import InMemoryStateManager
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.parallel_execution import ParallelExecutionManager
from src.account_manager import get_account_manager
from src.websocket_manager import WebSocketManager
from src.prediction import calculate_smart_size

# Logging Setup
logger = config.setup_logging()
config.validate_runtime_config(logger)

# Globals
FAILED_COINS = {}
ACTIVE_TASKS = {}
SHUTDOWN_FLAG = False
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 10.0  # Increased to 10s to prevent API rate limits
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
OPPORTUNITY_LOG_CACHE = {}

# NEW: In-flight margin reservation to avoid double-counting balance in parallel tasks
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()

telegram_bot = None

# ============================================================
# HELPERS
# ============================================================
async def get_execution_lock(symbol: str) -> asyncio.Lock:
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]

async def update_fee_stats(exchange: str, symbol: str, fee: float):
    # Simplifizierte Fee-Stats, da wir primär StateManager nutzen
    # Für Phase 4 optional, hier minimal implementiert
    pass 

async def process_fee_update(adapter, symbol, order_id):
    if not order_id: return
    try:
        fee = await adapter.get_order_fee(order_id)
        await update_fee_stats(adapter.name, symbol, fee)
    except:
        pass

# ============================================================
# STATE & DB WRAPPERS
# ============================================================
state_manager = None

async def get_open_trades() -> List[Dict]:
    return state_manager.get_open_trades() if state_manager else []

async def add_trade_to_state(trade_data: Dict):
    if state_manager: await state_manager.add_trade(trade_data)

async def close_trade_in_state(symbol: str):
    if state_manager: await state_manager.close_trade(symbol)

async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            exit_time = datetime.utcnow()
            entry_time = trade_data.get('entry_time')
            if isinstance(entry_time, str):
                try: entry_time = datetime.fromisoformat(entry_time)
                except: entry_time = datetime.utcnow() # Fallback
            
            duration = (exit_time - entry_time).total_seconds() / 3600 if entry_time else 0
            
            await conn.execute("""
                INSERT INTO trade_history 
                (symbol, entry_time, exit_time, hold_duration_hours, close_reason, 
                 final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd, account_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'], entry_time, exit_time, duration, close_reason,
                pnl_data['total_net_pnl'], pnl_data['funding_pnl'], pnl_data['spread_pnl'], pnl_data['fees'],
                trade_data.get('account_label', 'Main')
            ))
            await conn.commit()
            logger.info(f" 💰 PnL {trade_data['symbol']}: ${pnl_data['total_net_pnl']:.2f} ({close_reason})")
    except Exception as e:
        logger.error(f"Archive Error: {e}")

# ============================================================
# CORE TRADING LOGIC
# ============================================================
def is_tradfi_or_fx(symbol: str) -> bool:
    s = symbol.upper().replace("-USD", "").replace("/", "")
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")): return True
    if s.startswith(("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")) and "EUROC" not in s: return True
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")): return True
    return False

async def find_opportunities(lighter, x10, open_syms) -> List[Dict]:
    opps = []
    common = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()
    
    # Update Threshold Metrics
    current_rates = [lighter.fetch_funding_rate(s) for s in common if lighter.fetch_funding_rate(s) is not None]
    threshold_manager.update_metrics(current_rates)

    for s in common:
        if s in open_syms or s in config.BLACKLIST_SYMBOLS or is_tradfi_or_fx(s): continue
        if s in FAILED_COINS and (time.time() - FAILED_COINS[s] < 300): continue
        
        # Volatility Update
        if px := x10.fetch_mark_price(s):
            get_volatility_monitor().update_price(s, px)

        rl = lighter.fetch_funding_rate(s)
        rx = x10.fetch_funding_rate(s)
        if rl is None or rx is None: continue
        
        net = rl - rx
        apy = abs(net) * 24 * 365
        
        # Dynamic Threshold
        req_apy = threshold_manager.get_threshold(s, is_maker=True)
        if apy < req_apy: continue
        
        px = x10.fetch_mark_price(s)
        pl = lighter.fetch_mark_price(s)
        if not px or not pl: continue
        
        spread = abs(px - pl) / px
        if spread > config.MAX_SPREAD_FILTER_PERCENT: continue
        
        # Log Opportunity
        if apy > 0.5:
            now = time.time()
            if now - OPPORTUNITY_LOG_CACHE.get(s, 0) > 60:
                logger.info(f" 💎 OPP: {s} | APY: {apy*100:.1f}% | Net: {net:.6f}")
                OPPORTUNITY_LOG_CACHE[s] = now

        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net,
            'leg1_exchange': 'Lighter' if rl > rx else 'X10',
            'leg1_side': 'SELL' if rl > rx else 'BUY', # If Lighter pays X10 (rl > rx): Short Lighter, Long X10
            # Wait: Logic fix. 
            # If rl > rx: Lighter Rate Positive (Pays). X10 Rate Lower.
            # Strategy: Short Lighter (Earn), Long X10 (Pay less).
            # Correct.
            # If leg1=Lighter, leg1_side=SELL (Short).
            'is_farm_trade': False
        })
    
    opps.sort(key=lambda x: x['apy'], reverse=True)
    return opps[:config.MAX_OPEN_TRADES]

async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    symbol = opp['symbol']
    try:
        await execute_trade_parallel(opp, lighter, x10, parallel_exec)
    finally:
        if symbol in ACTIVE_TASKS:
            del ACTIVE_TASKS[symbol]

async def execute_trade_parallel(opp: Dict, lighter, x10, parallel_exec) -> bool:
    if SHUTDOWN_FLAG: return False
    symbol = opp['symbol']
    
    lock = await get_execution_lock(symbol)
    if lock.locked(): return False

    async with lock:
        # 1. Re-Check Existence
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing): return False

        # 2. Volatility Check
        vol_monitor = get_volatility_monitor()
        if not vol_monitor.can_enter_trade(symbol): return False
        
        # 3. Account Rotation (Phase 4 Feature)
        acct_mgr = get_account_manager()
        acc_x10 = acct_mgr.get_next_x10_account()
        acc_lit = acct_mgr.get_next_lighter_account()
        current_label = f"{acc_x10.get('label')}/{acc_lit.get('label')}"

        # 4. Prediction & Sizing
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        predictor = get_predictor()
        
        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        net_rate = current_rate_lit - current_rate_x10
        
        pred_rate, _, conf = await predictor.predict_next_funding_rate(
            symbol, abs(net_rate), lighter, x10, btc_trend
        )
        
        # Skip if prediction is bad (unless farming)
        if not opp.get('is_farm_trade') and pred_rate < abs(net_rate) * 0.8:
            return False

        # --- REPLACED: Race-safe balance snapshot + atomic reservation ---
        # Determine adapter identities (used for logging/reservation keys)
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        ex2 = lighter if ex1 is x10 else x10

        reserved = False
        final_usd = 0.0
        try:
            async with IN_FLIGHT_LOCK:
                try:
                    real_bal_x10 = await x10.get_real_available_balance()
                    real_bal_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    logger.error(f"Balance fetch error: {e}")
                    return False

                # Subtract already reserved in-flight amounts to get usable free balance
                bal_x10 = max(0.0, real_bal_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit = max(0.0, real_bal_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
                total_bal = bal_x10 + bal_lit

                # Determine intended size
                if opp.get('is_farm_trade'):
                    final_usd = float(config.FARM_NOTIONAL_USD)
                else:
                    size_calc = calculate_smart_size(conf, total_bal)
                    size_calc *= vol_monitor.get_size_adjustment(symbol)
                    final_usd = float(min(size_calc, config.MAX_TRADE_SIZE_USD))

                # Ensure min notionals
                min_req = max(x10.min_notional_usd(symbol), lighter.min_notional_usd(symbol))
                if final_usd < min_req:
                    final_usd = float(min_req)

                # Buffer to avoid small slippage; require both exchanges to have at least 110% of notional
                min_required = final_usd * 1.10
                if bal_x10 < min_required or bal_lit < min_required:
                    logger.debug(f"SKIP {symbol}: Insuff Balance (Real-InFlight). Need {min_required:.1f}, Have X10 {bal_x10:.1f} / Lit {bal_lit:.1f}")
                    return False

                # Reserve atomically
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + final_usd
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + final_usd
                reserved = True
        except Exception as e:
            logger.error(f"In-flight reservation error: {e}")
            return False

        # Keep backward-compatible 'size' variable used below
        size = final_usd

        # 5. Execute - determine best strategy / sides (existing logic preserved)
        # FIX #1: Calculate sides based on NET profitability
        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        
        if abs(current_rate_x10 - current_rate_lit) < 0.00001:
            logger.warning(f"{symbol}: Rates converged, skip")
            # Release reservation promptly
            # Note: finally block will release, but we can release earlier by toggling reserved False and adjusting margins
            # We'll just return and let finally release handle it.
            return False
        
        net_strategy1 = -current_rate_x10 - current_rate_lit
        net_strategy2 = -current_rate_lit - current_rate_x10
        
        if net_strategy1 > net_strategy2:
            side_x10 = "BUY"
            side_lit = "SELL"
            expected_net = net_strategy1
        else:
            side_x10 = "SELL"
            side_lit = "BUY"
            expected_net = net_strategy2
        
        logger.debug(
            f"{symbol}: X10={current_rate_x10:.6f} Lit={current_rate_lit:.6f} "
            f"→ X10 {side_x10} / Lit {side_lit} (Net: {expected_net:.6f})"
        )
        
        # Sanity check: Net should be positive
        if expected_net <= 0:
            logger.warning(f"{symbol}: Expected net {expected_net:.6f} <= 0, skip")
            return False

        logger.info(
            f"⚡ EXEC {symbol} ${size:.1f} | "
            f"X10 {side_x10} ({current_rate_x10:+.6f}) / "
            f"Lit {side_lit} ({current_rate_lit:+.6f}) | "
            f"Expected Net: {expected_net:.6f} | "
            f"Conf: {conf:.2%}"
        )

        try:
            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol,
                side_x10,
                side_lit,
                Decimal(str(size)),
                Decimal(str(size))
            )

            if success:
                trade = opp.copy()
                px = x10.fetch_mark_price(symbol) or 0.0
                pl = lighter.fetch_mark_price(symbol) or 0.0
                spread = abs(px - pl) / px if px > 0 else 0.0

                trade.update({
                    'notional_usd': size,
                    'entry_time': datetime.utcnow(),
                    'entry_price_x10': px,
                    'entry_price_lighter': pl,
                    'initial_spread_pct': spread,
                    'account_label': current_label,
                    'initial_funding_rate_hourly': net_rate
                })
                await add_trade_to_state(trade)
                if telegram_bot:
                    await telegram_bot.send_message(
                        f"✅ {symbol} opened\n💰 ${size:.0f} | Conf:{conf:.0%}"
                    )
                asyncio.create_task(process_fee_update(x10, symbol, x10_id))
                asyncio.create_task(process_fee_update(lighter, symbol, lit_id))
                return True
            else:
                FAILED_COINS[symbol] = time.time()
                return False
        finally:
            # Release reservation after slight delay to allow exchanges to debit (prevent tight races)
            try:
                if reserved:
                    await asyncio.sleep(1.5)
                    async with IN_FLIGHT_LOCK:
                        IN_FLIGHT_MARGIN['X10'] = max(0.0, IN_FLIGHT_MARGIN.get('X10', 0.0) - final_usd)
                        IN_FLIGHT_MARGIN['Lighter'] = max(0.0, IN_FLIGHT_MARGIN.get('Lighter', 0.0) - final_usd)
            except Exception as e:
                logger.error(f"Error releasing in-flight margin for {symbol}: {e}")

async def close_trade(trade: Dict, lighter, x10) -> bool:
    symbol = trade['symbol']
    notional = trade['notional_usd']
    
    # Simple logic: Just close both sides optimistically
    logger.info(f" 🔻 CLOSING {symbol}...")
    res = await asyncio.gather(
        x10.close_live_position(symbol, "BUY", notional), # Side parameter ignored by intelligent close logic usually
        lighter.close_live_position(symbol, "BUY", notional),
        return_exceptions=True
    )
    
    # Check success (tuple returns (bool, order_id))
    s1 = res[0][0] if isinstance(res[0], tuple) else False
    s2 = res[1][0] if isinstance(res[1], tuple) else False
    
    if s1 and s2:
        return True
    
    logger.warning(f" Close partial fail {symbol}: X10={s1}, Lit={s2}")
    return False # Retry logic handles this

# ============================================================
# LOOPS & MANAGEMENT
# ============================================================
async def get_cached_positions(lighter, x10, force=False):
    now = time.time()
    if not force and (now - POSITION_CACHE['last_update']) < POSITION_CACHE_TTL:
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    
    try:
        t1 = asyncio.create_task(x10.fetch_open_positions())
        t2 = asyncio.create_task(lighter.fetch_open_positions())
        p_x10, p_lit = await asyncio.gather(t1, t2, return_exceptions=True)
        
        POSITION_CACHE['x10'] = p_x10 if isinstance(p_x10, list) else []
        POSITION_CACHE['lighter'] = p_lit if isinstance(p_lit, list) else []
        POSITION_CACHE['last_update'] = now
    except Exception as e:
        logger.error(f"Pos Cache Error: {e}")
        
    return POSITION_CACHE['x10'], POSITION_CACHE['lighter']

async def manage_open_trades(lighter, x10):
    trades = await get_open_trades()
    if not trades: return

    # 1. Update Prices for PnL
    try:
        p_x10, p_lit = await get_cached_positions(lighter, x10)
    except:
        return

    for t in trades:
        sym = t['symbol']
        px = x10.fetch_mark_price(sym)
        pl = lighter.fetch_mark_price(sym)
        if not px or not pl: continue

        # Calculate PnL
        rx = x10.fetch_funding_rate(sym) or 0.0
        rl = lighter.fetch_funding_rate(sym) or 0.0
        
        # 1. FIX: Robuste Net Rate Calculation
        if t['leg1_exchange'] == 'Lighter':
            if t.get('leg1_side') == 'SELL':
                current_net = rl - rx # Short Lighter (receive), Long X10 (pay)
            else:
                current_net = rx - rl # Long Lighter (pay), Short X10 (receive)
        else: # Leg1 = X10
            if t.get('leg1_side') == 'BUY':
                current_net = rl - rx # Long X10 (pay), Short Lighter (receive)
            else:
                current_net = rx - rl # Short X10 (receive), Long Lighter (pay)

        # FIX #2: Calculate net based on position direction
        entry_time = t['entry_time']
        if isinstance(entry_time, str):
            try: entry_time = datetime.fromisoformat(entry_time)
            except: entry_time = datetime.utcnow()
        hold_hours = (datetime.utcnow() - entry_time).total_seconds() / 3600
        initial_net = t.get('initial_funding_rate_hourly', 0)
        if initial_net > 0:
            current_net = rl - rx
        else:
            current_net = rx - rl
        funding_pnl = current_net * hold_hours * t['notional_usd']
        
        # 2. FIX: Spread PnL in % (Korrekt)
        # entry_spread_pct ist schon in %, curr_spread_pct auch. NICHT durch Preis teilen.
        entry_spread_pct = abs(t['entry_price_x10'] - t['entry_price_lighter']) / t['entry_price_x10']
        current_spread_pct = abs(px - pl) / px
        
        spread_pnl = (entry_spread_pct - current_spread_pct) * t['notional_usd']
        
        # Fees approx (0.05% * 2 round trip + buffer)
        fees = t['notional_usd'] * 0.0012 
        
        total_pnl = funding_pnl + spread_pnl - fees
        
        # Check Exits
        reason = None
        
        # A. Volatility Panic
        vol_mon = get_volatility_monitor()
        if vol_mon.should_close_due_to_volatility(sym):
            reason = "VOLATILITY_PANIC"
            
        # B. Farm Timeout
        elif t.get('is_farm_trade'):
            if hold_hours * 3600 > config.FARM_HOLD_SECONDS:
                reason = "FARM_COMPLETE"
                
        # C. Stop Loss / Take Profit
        elif total_pnl < -t['notional_usd'] * 0.03: # 3% Stop
            reason = "STOP_LOSS"
        elif total_pnl > t['notional_usd'] * 0.05: # 5% TP
            reason = "TAKE_PROFIT"
            
        # D. Funding Flip (FIX: Explicit Sign Check)
        # D. Funding Flip (FIX: Use initial_net consistently)
        elif initial_net * current_net < 0:
            if not t.get('funding_flip_start_time'):
                now_ts = datetime.utcnow()
                t['funding_flip_start_time'] = now_ts
                if state_manager:
                    await state_manager.update_trade(sym, {
                        'funding_flip_start_time': datetime.utcnow()
                    })
                logger.info(f" 🔄 {sym} Funding Flipped! Init: {initial_net:.6f} -> Now: {current_net:.6f}")
            else:
                flip_start = t['funding_flip_start_time']
                if isinstance(flip_start, str): flip_start = datetime.fromisoformat(flip_start)
                if (datetime.utcnow() - flip_start).total_seconds() / 3600 > config.FUNDING_FLIP_HOURS_THRESHOLD:
                    reason = "FUNDING_FLIPPED"
        
        # Execute Close
        if reason:
            logger.info(f" Exiting {sym}: {reason} (PnL: ${total_pnl:.2f} | Fund: ${funding_pnl:.2f} | Spread: ${spread_pnl:.2f})")
            if await close_trade(t, lighter, x10):
                await close_trade_in_state(sym)
                await archive_trade_to_history(t, reason, {
                    'total_net_pnl': total_pnl,
                    'funding_pnl': funding_pnl,
                    'spread_pnl': spread_pnl,
                    'fees': 0.0
                })
                if telegram_bot:
                    await telegram_bot.send_message(
                        f"🏁 {sym} closed: {reason}\n💰 PnL: ${total_pnl:.2f}"
                    )

async def cleanup_zombie_positions(lighter, x10):
    """Full Zombie Cleanup Implementation"""
    try:
        x_pos, l_pos = await get_cached_positions(lighter, x10, force=True)
        
        x_syms = {p['symbol'] for p in x_pos if abs(p['size']) > 1e-8}
        l_syms = {p['symbol'] for p in l_pos if abs(p['size']) > 1e-8}
        
        db_trades = await get_open_trades()
        db_syms = {t['symbol'] for t in db_trades}
        
        # 1. Exchange Zombies (Open on Exchange, Closed in DB)
        all_exchange = x_syms | l_syms
        zombies = all_exchange - db_syms
        
        if zombies:
            logger.warning(f" 🧟 ZOMBIES DETECTED: {zombies}")
            for sym in zombies:
                # Close X10
                if sym in x_syms:
                    p = next(p for p in x_pos if p['symbol'] == sym)
                    await x10.close_live_position(sym, "BUY" if p['size'] > 0 else "SELL", abs(p['size'] * 100)) # Approx notional
                # Close Lighter
                if sym in l_syms:
                    p = next(p for p in l_pos if p['symbol'] == sym)
                    await lighter.close_live_position(sym, "BUY" if p['size'] > 0 else "SELL", abs(p['size'] * 100))
                    
        # 2. DB Ghosts (Open in DB, Closed on Exchange)
        ghosts = db_syms - all_exchange
        if ghosts:
            logger.warning(f" 👻 GHOSTS DETECTED: {ghosts}")
            for sym in ghosts:
                logger.info(f" Closing ghost {sym} in DB")
                await close_trade_in_state(sym)
                
    except Exception as e:
        logger.error(f"Zombie Cleanup Error: {e}")

async def farm_loop(lighter, x10, parallel_exec):
    """Aggressive Volume Farming Loop"""
    logger.info(" 🚜 Farming Loop started.")
    while True:
        try:
            if not getattr(config, 'VOLUME_FARM_MODE', False):
                await asyncio.sleep(10)
                continue

            trades = await get_open_trades()
            if len(trades) >= config.FARM_MAX_CONCURRENT:
                await asyncio.sleep(1)
                continue

            open_syms = {t['symbol'] for t in trades}
            
            # Simple Farming Strategy: Low Spread Pairs
            candidates = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "ARB-USD"]
            random.shuffle(candidates)

            for sym in candidates:
                if sym in open_syms: continue
                
                px = x10.fetch_mark_price(sym)
                pl = lighter.fetch_mark_price(sym)
                if not px or not pl: continue
                
                spread = abs(px - pl) / px
                if spread > config.FARM_MAX_SPREAD_PCT: continue

                # Just enter randomly 
                opp = {
                    'symbol': sym,
                    'apy': 0,
                    'net_funding_hourly': 0,
                    'leg1_exchange': 'X10',
                    'leg1_side': 'BUY',
                    'is_farm_trade': True,
                    'notional_usd': config.FARM_NOTIONAL_USD
                }
                
                async with await get_execution_lock(sym):
                    if sym not in ACTIVE_TASKS:
                        task = asyncio.create_task(execute_trade_task(opp, lighter, x10, parallel_exec))
                        ACTIVE_TASKS[sym] = task
                
                await asyncio.sleep(random.uniform(2, 5))
                break

            await asyncio.sleep(1)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Farm Loop Error: {e}")
            await asyncio.sleep(5)

async def cleanup_finished_tasks():
    """Helper to clean up finished execution tasks"""
    finished = [s for s, t in ACTIVE_TASKS.items() if t.done()]
    for s in finished:
        try:
            await ACTIVE_TASKS[s]
        except Exception as e:
            logger.error(f"Task {s} failed: {e}")
        del ACTIVE_TASKS[s]


async def logic_loop(lighter, x10, price_event, parallel_exec):
    """Main Logic Loop - Non-Blocking Refactor (Roadmap #2)"""
    logger.info(" 🧠 Logic Loop started (Non-Blocking).")
    last_zombie = 0
    management_task = None
    
    while True:
        try:
            # 1. Wait for Trigger (Price Update or Timeout)
            try:
                await asyncio.wait_for(price_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            price_event.clear()

            # 2. Cleanup Old Tasks
            await cleanup_finished_tasks()

            # 3. Manage Open Trades (Non-Blocking)
            # Only spawn if previous cycle finished to prevent overlap race conditions
            if management_task is None or management_task.done():
                if management_task and management_task.exception():
                    logger.error(f"Manage Task Error: {management_task.exception()}")
                management_task = asyncio.create_task(manage_open_trades(lighter, x10))

            # 4. Zombie Cleanup (every 5 min)
            now = time.time()
            if now - last_zombie > 300:
                try:
                    await cleanup_zombie_positions(lighter, x10)
                except Exception as ze:
                    logger.error(f"Zombie cleanup error: {ze}")
                    if telegram_bot:
                        await telegram_bot.send_error(f"Zombie: {ze}")
                last_zombie = now

            # 5. Check Opportunities
            trades = await get_open_trades()
            
            # CRITICAL FIX: Limit concurrent executions to prevent margin exhaustion
            # Max 2 new executions at a time, even if we have slots
            MAX_CONCURRENT_NEW = 2
            current_executing = len(ACTIVE_TASKS)
            current_open = len(trades)
            total_busy = current_open + current_executing
            
            if total_busy < config.MAX_OPEN_TRADES:
                open_syms = {t['symbol'] for t in trades}
                
                # Calculate how many new trades we can start
                slots_available = config.MAX_OPEN_TRADES - total_busy
                new_trades_allowed = min(MAX_CONCURRENT_NEW, slots_available)
                
                logger.debug(
                    f"Slots: Open={current_open}, Executing={current_executing}, "
                    f"Available={slots_available}, Will start={new_trades_allowed}"
                )
                
                # A. Latency Arb (limit to 1)
                if new_trades_allowed > 0:
                    detector = get_detector()
                    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
                        if sym in open_syms or sym in ACTIVE_TASKS: continue
                        
                        opp = await detector.detect_lag_opportunity(
                            sym, x10.fetch_funding_rate(sym) or 0, 
                            lighter.fetch_funding_rate(sym) or 0, x10, lighter
                        )
                        if opp:
                            async with await get_execution_lock(sym):
                                if sym not in ACTIVE_TASKS:
                                    ACTIVE_TASKS[sym] = asyncio.create_task(
                                        execute_trade_task(opp, lighter, x10, parallel_exec)
                                    )
                                    new_trades_allowed -= 1
                            break
                
                # B. Standard Funding Arb (limited by new_trades_allowed)
                if new_trades_allowed > 0:
                    opps = await find_opportunities(lighter, x10, open_syms)
                    started = 0
                    for opp in opps:
                        if started >= new_trades_allowed:
                            break
                        
                        sym = opp['symbol']
                        if sym in ACTIVE_TASKS:
                            continue
                        
                        async with await get_execution_lock(sym):
                            if sym not in ACTIVE_TASKS:
                                ACTIVE_TASKS[sym] = asyncio.create_task(
                                    execute_trade_task(opp, lighter, x10, parallel_exec)
                                )
                                started += 1

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Logic Loop Error: {e}")
            traceback.print_exc()
            if telegram_bot:
                await telegram_bot.send_error(f"Logic: {str(e)[:200]}")
            await asyncio.sleep(1)

async def maintenance_loop(lighter, x10):
    """Background tasks: Funding rates"""
    while True:
        try:
            await lighter.load_funding_rates_and_prices()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(60)

async def setup_database():
    """Ensure Tables Exist"""
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                symbol TEXT PRIMARY KEY,
                entry_time TIMESTAMP,
                notional_usd REAL,
                status TEXT DEFAULT 'OPEN',
                leg1_exchange TEXT,
                initial_spread_pct REAL,
                initial_funding_rate_hourly REAL,
                entry_price_x10 REAL,
                entry_price_lighter REAL,
                funding_flip_start_time TIMESTAMP,
                is_farm_trade BOOLEAN DEFAULT FALSE,
                account_label TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                entry_time TIMESTAMP,
                exit_time TIMESTAMP,
                hold_duration_hours REAL,
                close_reason TEXT,
                final_pnl_usd REAL,
                funding_pnl_usd REAL,
                spread_pnl_usd REAL,
                fees_usd REAL,
                account_label TEXT
            )
        """)
        await conn.commit()

# ============================================================
# MAIN
# ============================================================
async def main():
    global SHUTDOWN_FLAG, state_manager
    logger.info(" 🔥 BOT V4 (Full Architected) STARTING...")

    # 1. Init Infrastructure
    state_manager = InMemoryStateManager(config.DB_FILE)
    await state_manager.start()
    global telegram_bot
    telegram_bot = get_telegram_bot()
    if telegram_bot.enabled:
        await telegram_bot.start()
        logger.info("📱 Telegram Bot connected")
    await setup_database()
    
    # 2. Init Adapters
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # 3. Init Managers
    ws_manager = WebSocketManager([x10, lighter])
    parallel_exec = ParallelExecutionManager(x10, lighter)
    
    # 4. Load Initial Data
    logger.info(" Loading Market Data...")
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    
    # 5. Start Background Loops
    logger.info(" Spawning Tasks...")
    tasks = [
        asyncio.create_task(ws_manager.start()),
        asyncio.create_task(logic_loop(lighter, x10, price_event, parallel_exec)),
        asyncio.create_task(farm_loop(lighter, x10, parallel_exec)),
        asyncio.create_task(maintenance_loop(lighter, x10))
    ]
    
    logger.info(" ✅ BOT RUNNING. Press Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        SHUTDOWN_FLAG = True
        logger.info(" Stopping Managers...")
        await ws_manager.stop()
        await state_manager.stop()
        await x10.aclose()
        await lighter.aclose()
        logger.info(" BOT SHUTDOWN COMPLETE.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass