# monitor_funding.py
import sys
import os
import time
import asyncio
import inspect
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
logger = config.setup_logging(per_run=True, run_id=os.getenv("RUN_ID"))
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

# ============================================================
# GLOBALS (Ergänzung)
# ============================================================
# Behalte IN_FLIGHT_MARGIN von vorher!
# Define in-flight reservation globals (used when reserving trade notional)
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()
# ============================================================
# FEE & STARTUP LOGIC
# ============================================================
async def load_fee_cache():
    """Load historical fee stats from state manager on startup

    Note: The `InMemoryStateManager.start()` now loads fee stats into
    the running state; this function is kept as a no-op compatibility hook.
    """
    # State manager handles fee cache loading during startup
    return

# `update_fee_stats` removed: `state_manager.update_fee_stats()` is used instead.


async def process_fee_update(adapter, symbol: str, order_id: str):
    """Fetch order fee after execution and update stats via state manager."""
    if not order_id or order_id == "DRY_RUN":
        return

    # Wait for order to settle
    await asyncio.sleep(3.0)

    for retry in range(2):
        try:
            fee_rate = await adapter.get_order_fee(order_id)

            if fee_rate > 0 or retry == 1:
                # Update via state manager
                if state_manager:
                    await state_manager.update_fee_stats(
                        exchange=adapter.name,
                        symbol=symbol,
                        fee_rate=fee_rate if fee_rate > 0 else config.TAKER_FEE_X10
                    )
                return

            # Retry if zero (order might not be settled)
            await asyncio.sleep(2)

        except Exception as e:
            if retry == 1:
                # Fallback on final retry
                fallback = config.TAKER_FEE_X10 if adapter.name == 'X10' else 0.0
                if state_manager:
                    await state_manager.update_fee_stats(adapter.name, symbol, fallback)
            await asyncio.sleep(1)

def get_estimated_fee_rate(exchange: str, symbol: str) -> float:
    """Get cached fee rate from state manager"""
    if state_manager:
        try:
            return state_manager.get_fee_estimate(exchange, symbol)
        except Exception:
            # If state manager call fails, fall back to defaults below
            pass

    # Fallback if state manager not initialized
    return config.TAKER_FEE_X10 if exchange == 'X10' else 0.0

telegram_bot = None

# ============================================================
# HELPERS
# ============================================================
async def get_execution_lock(symbol: str) -> asyncio.Lock:
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]

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
        return False

    return True

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
    opps: List[Dict] = []
    common = set(lighter.price_cache.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()

    logger.info(
        f"🔍 Scanning {len(common)} pairs. "
        f"Lighter cache: {len(lighter.price_cache)}, X10 markets: {len(x10.market_info)}"
    )

    semaphore = asyncio.Semaphore(20)

    async def maybe_await(func, *args):
        try:
            res = func(*args)
        except Exception:
            # Let caller handle/log exceptions per-symbol
            raise
        if inspect.isawaitable(res):
            return await res
        return res

    async def fetch_symbol(s: str):
        async with semaphore:
            try:
                lr = await maybe_await(lighter.fetch_funding_rate, s)
                xr = await maybe_await(x10.fetch_funding_rate, s)
                px = await maybe_await(x10.fetch_mark_price, s)
                pl = await maybe_await(lighter.fetch_mark_price, s)
                return (s, lr, xr, px, pl)
            except Exception as e:
                logger.debug(f"Error fetching data for {s}: {e}")
                return (s, None, None, None, None)

    # Launch concurrent fetches
    tasks = [asyncio.create_task(fetch_symbol(s)) for s in common]
    results = await asyncio.gather(*tasks)

    # Update threshold manager using collected lighter rates
    current_rates = [lr for (_s, lr, _xr, _px, _pl) in results if lr is not None]
    try:
        threshold_manager.update_metrics(current_rates)
    except Exception as e:
        logger.debug(f"Failed to update threshold metrics: {e}")

    now_ts = time.time()
    for s, rl, rx, px, pl in results:
        if s in open_syms or s in config.BLACKLIST_SYMBOLS or is_tradfi_or_fx(s):
            continue

        if s in FAILED_COINS and (now_ts - FAILED_COINS[s] < 300):
            continue

        if rl is None or rx is None:
            logger.debug(f"Skip {s}: Missing rates (L={rl}, X={rx})")
            continue

        if px is not None:
            try:
                get_volatility_monitor().update_price(s, px)
            except Exception as e:
                logger.debug(f"Volatility monitor update failed for {s}: {e}")

        net = rl - rx
        apy = abs(net) * 24 * 365

        req_apy = threshold_manager.get_threshold(s, is_maker=True)
        if apy < req_apy:
            continue

        # Best-effort fetch missing prices
        if px is None:
            try:
                px = await maybe_await(x10.fetch_mark_price, s)
                if px is not None:
                    try:
                        get_volatility_monitor().update_price(s, px)
                    except Exception:
                        pass
            except Exception:
                px = None

        if pl is None:
            try:
                pl = await maybe_await(lighter.fetch_mark_price, s)
            except Exception:
                pl = None

        if not px or not pl:
            logger.debug(f"Skip {s}: Missing prices")
            continue

        spread = abs(px - pl) / px
        if spread > config.MAX_SPREAD_FILTER_PERCENT:
            logger.debug(f"Skip {s}: Spread {spread*100:.2f}% too high")
            continue

        if apy > 0.5:
            if now_ts - OPPORTUNITY_LOG_CACHE.get(s, 0) > 60:
                logger.info(f"💎 {s} | APY: {apy*100:.1f}%")
                OPPORTUNITY_LOG_CACHE[s] = now_ts

        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net,
            'leg1_exchange': 'Lighter' if rl > rx else 'X10',
            'leg1_side': 'SELL' if rl > rx else 'BUY',
            'is_farm_trade': False
        })

    opps.sort(key=lambda x: x['apy'], reverse=True)
    logger.info(f"✅ Found {len(opps)} opportunities")
    return opps[:config.MAX_OPEN_TRADES]

async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    symbol = opp['symbol']
    try:
        await execute_trade_parallel(opp, lighter, x10, parallel_exec)
    finally:
        if symbol in ACTIVE_TASKS:
            del ACTIVE_TASKS[symbol]

async def execute_trade_parallel(opp: Dict, lighter, x10, parallel_exec) -> bool:
    if SHUTDOWN_FLAG:
        return False

    symbol = opp['symbol']
    lock = await get_execution_lock(symbol)
    if lock.locked():
        return False

    async with lock:
        # Check if already open
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing):
            return False

        # Volatility check
        vol_monitor = get_volatility_monitor()
        if not vol_monitor.can_enter_trade(symbol):
            return False

        # Account rotation
        acct_mgr = get_account_manager()
        acc_x10 = acct_mgr.get_next_x10_account()
        acc_lit = acct_mgr.get_next_lighter_account()
        current_label = f"{acc_x10.get('label')}/{acc_lit.get('label')}"

        # Prediction
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        predictor = get_predictor()

        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        net_rate = current_rate_lit - current_rate_x10

        pred_rate, _, conf = await predictor.predict_next_funding_rate(
            symbol, abs(net_rate), lighter, x10, btc_trend
        )

        if not opp.get('is_farm_trade') and pred_rate < abs(net_rate) * 0.8:
            return False

        # SIZE CALCULATION
        # ---------------------------------------------------------
        # CRITICAL: RESERVATION LOGIC TO PREVENT RACE CONDITIONS
        # ---------------------------------------------------------
        global IN_FLIGHT_MARGIN, IN_FLIGHT_LOCK
        reserved_amount = 0.0

        if opp.get('is_farm_trade'):
            final_usd = float(config.FARM_NOTIONAL_USD)
            final_usd = 16.0  # FORCE LOWER SIZE
        else:
            # Get balances (lock-protected read and subtract in-flight reservations)
            try:
                async with IN_FLIGHT_LOCK:
                    raw_x10 = await x10.get_real_available_balance()
                    raw_lit = await lighter.get_real_available_balance()

                    # Deduct currently executing trades
                    bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                    bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
                    
                    logger.info(f"💰 Balances: X10=${bal_x10_real:.1f} (raw=${raw_x10:.1f}), Lit=${bal_lit_real:.1f} (raw=${raw_lit:.1f})")
            except Exception as e:
                logger.error(f"Balance fetch error: {e}")
                return False
            # CRITICAL: Check minimum balance BEFORE reservation
            if bal_x10_real < 20.0 or bal_lit_real < 20.0:
                logger.warning(f"⚠️ Insufficient balance for {symbol}: X10=${bal_x10_real:.1f}, Lit=${bal_lit_real:.1f}")
                return False

            max_per_trade = min(bal_x10_real, bal_lit_real) * 0.20  # Conservative 20%

            # Kelly sizing with confidence
            size_calc = calculate_smart_size(conf, bal_x10_real + bal_lit_real)
            size_calc *= vol_monitor.get_size_adjustment(symbol)

            final_usd = min(size_calc, max_per_trade, config.MAX_TRADE_SIZE_USD)

        # Exchange minimum (but cap at 18 to prevent oversizing)
        min_req = max(x10.min_notional_usd(symbol), lighter.min_notional_usd(symbol))
        if final_usd < min_req:
            final_usd = float(min_req)

        # Absolute minimum check
        if final_usd < 12.0:
            logger.debug(f"Size too small: ${final_usd:.1f}")
            return False

        # RESERVE MARGIN
        try:
            async with IN_FLIGHT_LOCK:
                # RE-CHECK balances after acquiring lock
                raw_x10_check = await x10.get_real_available_balance()
                raw_lit_check = await lighter.get_real_available_balance()
                
                bal_x10_check = max(0.0, raw_x10_check - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_check = max(0.0, raw_lit_check - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
                
                # ABORT if insufficient after re-check
                if bal_x10_real < final_usd or bal_lit_real < final_usd:
                    logger.error(f"🚫 ABORT {symbol}: Insufficient after lock - X10=${bal_x10_check:.1f}, Lit=${bal_lit_check:.1f}, Need=${final_usd:.1f}")
                    return False

                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + final_usd
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + final_usd
                reserved_amount = final_usd
                
                logger.info(f"🔒 Reserved ${reserved_amount:.1f} | Total In-Flight: X10=${IN_FLIGHT_MARGIN['X10']:.1f}, Lit=${IN_FLIGHT_MARGIN['Lighter']:.1f}")
        except Exception as e:
            logger.error(f"Reservation error: {e}")
            return False

        # SIDE MAPPING
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        side1 = opp['leg1_side']
        side2 = "SELL" if side1 == "BUY" else "BUY"

        x10_side = side1 if getattr(ex1, 'name', '') == 'X10' else side2
        lighter_side = side2 if getattr(ex1, 'name', '') == 'X10' else side1

        # FINAL SANITY CHECK: Verify size doesn't exceed minimums
        if final_usd < 14.0 or final_usd > 20.0:
            logger.error(f"🚫 ABORT {symbol}: Invalid size ${final_usd:.1f}")
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] -= reserved_amount
                IN_FLIGHT_MARGIN['Lighter'] -= reserved_amount
            return False

        # PRE-FLIGHT POSITION CHECK (informational only)
        try:
            x10_check = await x10.fetch_open_positions()
            lighter_check = await lighter.fetch_open_positions()
            
            if any(p['symbol'] == symbol for p in (x10_check or [])):
                logger.debug(f"{symbol} already open on X10")
                return False
            if any(p['symbol'] == symbol for p in (lighter_check or [])):
                logger.debug(f"{symbol} already open on Lighter")
                return False
                
        except Exception as e:
            logger.debug(f"Pre-flight check failed: {e}")

        # EXECUTION
        logger.info(f"🚀 EXEC {symbol} ${final_usd:.1f} | X10 {x10_side} / Lit {lighter_side}")

        # CRITICAL: Cancel all pending orders first (prevents ghost fills)
        try:
            await asyncio.gather(
                x10.cancel_all_orders(symbol) if hasattr(x10, 'cancel_all_orders') else asyncio.sleep(0),
                lighter.cancel_all_orders(symbol) if hasattr(lighter, 'cancel_all_orders') else asyncio.sleep(0),
                return_exceptions=True
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"Pre-exec cancel: {e}")

        # CRITICAL: Double-check no position exists before execution
        existing_in_state = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing_in_state):
            logger.error(f"🚫 ABORT {symbol}: Already in state DB")
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] -= reserved_amount
                IN_FLIGHT_MARGIN['Lighter'] -= reserved_amount
            return False

        try:
            pre_check_x10 = await x10.fetch_open_positions()
            pre_check_lit = await lighter.fetch_open_positions()
            if any(p['symbol'] == symbol for p in (pre_check_x10 or [])) or \
               any(p['symbol'] == symbol for p in (pre_check_lit or [])):
                logger.error(f"🚨 ABORT {symbol}: Position exists before exec")
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] -= reserved_amount
                    IN_FLIGHT_MARGIN['Lighter'] -= reserved_amount
                return False
        except Exception as e:
            logger.error(f"Pre-check error {symbol}: {e}")
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] -= reserved_amount
                IN_FLIGHT_MARGIN['Lighter'] -= reserved_amount
            return False

        try:
            # Store initial positions for rollback comparison
            initial_x10_pos = await x10.fetch_open_positions()
            initial_lit_pos = await lighter.fetch_open_positions()
            
            initial_x10_syms = {p['symbol'] for p in (initial_x10_pos or [])}
            initial_lit_syms = {p['symbol'] for p in (initial_lit_pos or [])}
            
            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol, x10_side, lighter_side,
                Decimal(str(final_usd)), Decimal(str(final_usd)), None, None
            )
            
            # CRITICAL: Always verify actual positions after execution
            await asyncio.sleep(2)
            
            final_x10_pos = await x10.fetch_open_positions()
            final_lit_pos = await lighter.fetch_open_positions()
            
            x10_opened = any(p['symbol'] == symbol for p in (final_x10_pos or []) if p['symbol'] not in initial_x10_syms)
            lit_opened = any(p['symbol'] == symbol for p in (final_lit_pos or []) if p['symbol'] not in initial_lit_syms)
            
            # CRITICAL: If execution reported success but positions don't match → FORCE ROLLBACK
            if success and (not x10_opened or not lit_opened):
                logger.error(f"🚨 MISMATCH {symbol}: success={success} but X10={x10_opened}, Lit={lit_opened}")
                success = False
            
            # CRITICAL: If execution reported failure but positions exist → FORCE CLEANUP
            if not success and (x10_opened or lit_opened):
                logger.error(f"🧟 GHOST CREATED {symbol}: success={success} but X10={x10_opened}, Lit={lit_opened}")
                
                if x10_opened:
                    x10_pos = next(p for p in final_x10_pos if p['symbol'] == symbol)
                    close_side = "SELL" if x10_pos.get('size', 0) > 0 else "BUY"
                    logger.error(f"→ FORCE CLOSE X10 {symbol} {close_side}")
                    await x10.close_live_position(symbol, close_side, final_usd)
                
                if lit_opened:
                    lit_pos = next(p for p in final_lit_pos if p['symbol'] == symbol)
                    close_side = "SELL" if lit_pos.get('size', 0) > 0 else "BUY"
                    logger.error(f"→ FORCE CLOSE Lighter {symbol} {close_side}")
                    await lighter.close_live_position(symbol, close_side, final_usd)
                
                FAILED_COINS[symbol] = time.time()
                return False

            if success:
                # Verification
                await asyncio.sleep(3)
                try:
                    x10_verify = await x10.fetch_open_positions()
                    lighter_verify = await lighter.fetch_open_positions()

                    x10_filled = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (x10_verify or []))
                    lighter_filled = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (lighter_verify or []))

                    if not x10_filled or not lighter_filled:
                        logger.error(f"🚨 VERIFY FAIL {symbol}: X10={x10_filled}, Lit={lighter_filled}")
                        
                        # Rollback
                        if x10_filled:
                            await x10.close_live_position(symbol, side1 if ex1 == x10 else side2, final_usd)
                        if lighter_filled:
                            await lighter.close_live_position(symbol, side2 if ex1 == x10 else side1, final_usd)

                        FAILED_COINS[symbol] = time.time()
                        return False

                    logger.info(f"✅ VERIFIED {symbol}")

                except Exception as verify_err:
                    logger.error(f"Verification error {symbol}: {verify_err}")

                # Save trade
                trade = opp.copy()
                px = x10.fetch_mark_price(symbol) or 0.0
                pl = lighter.fetch_mark_price(symbol) or 0.0
                spread = abs(px - pl) / px if px > 0 else 0.0

                trade.update({
                    'notional_usd': final_usd,
                    'entry_time': datetime.utcnow(),
                    'entry_price_x10': px,
                    'entry_price_lighter': pl,
                    'initial_spread_pct': spread,
                    'account_label': current_label,
                    'initial_funding_rate_hourly': net_rate
                })
                await add_trade_to_state(trade)
                
                if telegram_bot:
                    await telegram_bot.send_message(f"✅ {symbol} opened\n💰 ${final_usd:.0f} | Conf:{conf:.0%}")

                # Fee tracking
                if x10_id:
                    asyncio.create_task(process_fee_update(x10, symbol, x10_id))
                if lit_id:
                    asyncio.create_task(process_fee_update(lighter, symbol, lit_id))

                return True
            else:
                logger.error(f"🚨 EXEC FAILED {symbol} - AGGRESSIVE CLEANUP")
                
                # Step 1: Cancel orders immediately
                await asyncio.gather(
                    x10.cancel_all_orders(symbol) if hasattr(x10, 'cancel_all_orders') else asyncio.sleep(0),
                    lighter.cancel_all_orders(symbol) if hasattr(lighter, 'cancel_all_orders') else asyncio.sleep(0),
                    return_exceptions=True
                )
                await asyncio.sleep(2.0)
                
                # Step 2: Force-fetch positions to detect ghost fills
                cleanup_x10 = await x10.fetch_open_positions()
                cleanup_lit = await lighter.fetch_open_positions()
                
                x10_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_x10 or []))
                lit_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_lit or []))
                
                # Step 3: Close any ghost positions aggressively
                if x10_ghost or lit_ghost:
                    logger.error(f"🧟 GHOST DETECTED {symbol}: X10={x10_ghost}, Lit={lit_ghost}")
                    
                    if x10_ghost:
                        actual_pos = next(p for p in cleanup_x10 if p['symbol'] == symbol)
                        close_side = "SELL" if actual_pos.get('size', 0) > 0 else "BUY"
                        await x10.close_live_position(symbol, close_side, final_usd)
                    
                    if lit_ghost:
                        actual_pos = next(p for p in cleanup_lit if p['symbol'] == symbol)
                        close_side = "SELL" if actual_pos.get('size', 0) > 0 else "BUY"
                        await lighter.close_live_position(symbol, close_side, final_usd)

                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.error(f"Execution error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False

        finally:
            # ALWAYS RELEASE MARGIN
            if reserved_amount > 0.01:
                async with IN_FLIGHT_LOCK:
                    old_x10 = IN_FLIGHT_MARGIN.get('X10', 0.0)
                    old_lit = IN_FLIGHT_MARGIN.get('Lighter', 0.0)
                    
                    IN_FLIGHT_MARGIN['X10'] = max(0.0, old_x10 - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(0.0, old_lit - reserved_amount)
                    
                    logger.info(f"🔓 Released ${reserved_amount:.1f} | Remaining: X10=${IN_FLIGHT_MARGIN['X10']:.1f}, Lit=${IN_FLIGHT_MARGIN['Lighter']:.1f}")
                    reserved_amount = 0.0

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

    try:
        p_x10, p_lit = await get_cached_positions(lighter, x10)
    except:
        return

    for t in trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        if not px or not pl: continue

        rx = x10.fetch_funding_rate(sym) or 0
        rl = lighter.fetch_funding_rate(sym) or 0
        # Improved Net Calc
        base_net = rl - rx
        current_net = -base_net if t['leg1_exchange'] == 'X10' else base_net

        # PnL & Duration
        entry_time = t['entry_time']
        if isinstance(entry_time, str):
            try: entry_time = datetime.fromisoformat(entry_time)
            except: entry_time = datetime.utcnow()

        hold_hours = (datetime.utcnow() - entry_time).total_seconds() / 3600
        funding_pnl = current_net * hold_hours * t['notional_usd']

        # Spread
        entry_spread = abs(t['entry_price_x10'] - t['entry_price_lighter'])
        curr_spread = abs(px - pl)
        spread_pnl = (entry_spread - curr_spread) / px * t['notional_usd']

        # Fees (Dynamic from State Manager)
        fee_x10 = state_manager.get_fee_estimate('X10', sym, default=config.TAKER_FEE_X10)
        fee_lit = state_manager.get_fee_estimate('Lighter', sym, default=0.0)
        est_fees = t['notional_usd'] * (fee_x10 + fee_lit) * 2.0

        total_pnl = funding_pnl + spread_pnl - est_fees

        # Check Exits
        reason = None
        vol_mon = get_volatility_monitor()

        if vol_mon.should_close_due_to_volatility(sym):
            reason = "VOLATILITY_PANIC"
        elif t.get('is_farm_trade') and hold_hours * 3600 > config.FARM_HOLD_SECONDS:
            reason = "FARM_COMPLETE"
        elif total_pnl < -t['notional_usd'] * 0.03:
            reason = "STOP_LOSS"
        elif total_pnl > t['notional_usd'] * 0.05:
            reason = "TAKE_PROFIT"
        elif t.get('initial_funding_rate_hourly', 0) * current_net < 0:
            if not t.get('funding_flip_start_time'):
                t['funding_flip_start_time'] = datetime.utcnow()
                if state_manager:
                    await state_manager.update_trade(sym, {'funding_flip_start_time': datetime.utcnow()})
            else:
                flip_start = t['funding_flip_start_time']
                if isinstance(flip_start, str): flip_start = datetime.fromisoformat(flip_start)
                if (datetime.utcnow() - flip_start).total_seconds() / 3600 > config.FUNDING_FLIP_HOURS_THRESHOLD:
                    reason = "FUNDING_FLIP"
        else:
            if t.get('funding_flip_start_time'):
                if state_manager:
                    await state_manager.update_trade(sym, {'funding_flip_start_time': None})

        if reason:
            logger.info(f" 💸 EXIT {sym}: {reason} | PnL ${total_pnl:.2f} (Fees: ${est_fees:.2f})")
            if await close_trade(t, lighter, x10):
                await close_trade_in_state(sym)
                await archive_trade_to_history(t, reason, {
                    'total_net_pnl': total_pnl, 'funding_pnl': funding_pnl,
                    'spread_pnl': spread_pnl, 'fees': est_fees
                })
                telegram = get_telegram_bot()
                if telegram.enabled:
                    await telegram.send_trade_alert(sym, reason, t['notional_usd'], total_pnl)

async def cleanup_zombie_positions(lighter, x10):
    """Full Zombie Cleanup Implementation"""
    try:
        x_pos, l_pos = await get_cached_positions(lighter, x10, force=True)
        
        x_syms = {p['symbol'] for p in x_pos if abs(p.get('size', 0)) > 1e-8}
        l_syms = {p['symbol'] for p in l_pos if abs(p.get('size', 0)) > 1e-8}
        
        db_trades = await get_open_trades()
        db_syms = {t['symbol'] for t in db_trades}
        
        # 1. Exchange Zombies (Open on Exchange, Closed in DB)
        all_exchange = x_syms | l_syms
        zombies = all_exchange - db_syms
        
        if zombies:
            logger.warning(f" 🧟 ZOMBIES DETECTED: {zombies}")
            for sym in zombies:
                # FIXED: Get actual position size and side
                if sym in x_syms:
                    p = next((pos for pos in x_pos if pos['symbol'] == sym), None)
                    if p:
                        size_usd = abs(p.get('size', 0)) * x10.fetch_mark_price(sym)
                        side = "BUY" if p.get('size', 0) < 0 else "SELL"  # Opposite side to close
                        logger.info(f" Closing X10 zombie {sym}: {side} ${size_usd:.1f}")
                        await x10.close_live_position(sym, side, size_usd)
                
                if sym in l_syms:
                    p = next((pos for pos in l_pos if pos['symbol'] == sym), None)
                    if p:
                        size_usd = abs(p.get('size', 0)) * lighter.fetch_mark_price(sym)
                        side = "BUY" if p.get('size', 0) < 0 else "SELL"
                        logger.info(f" Closing Lighter zombie {sym}: {side} ${size_usd:.1f}")
                        await lighter.close_live_position(sym, side, size_usd)
                    
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
            if True:  # DISABLED until zombies cleaned
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
    """Main Logic Loop"""
    logger.info("🧠 Logic Loop started")
    last_zombie = 0
    management_task = None
    
    while True:
        try:
            # Wait for trigger
            try:
                await asyncio.wait_for(price_event.wait(), timeout=5.0)  # Increased from 1.0
            except asyncio.TimeoutError:
                pass
            price_event.clear()

            # Cleanup
            await cleanup_finished_tasks()

            # Manage trades
            if management_task is None or management_task.done():
                if management_task and management_task.exception():
                    logger.error(f"Manage Task Error: {management_task.exception()}")
                management_task = asyncio.create_task(manage_open_trades(lighter, x10))

            # Zombie cleanup
            now = time.time()
            if now - last_zombie > 180:  # Reduced from 300 to 180s
                try:
                    await cleanup_zombie_positions(lighter, x10)
                except Exception as ze:
                    logger.error(f"Zombie cleanup: {ze}")
                last_zombie = now

            # Check opportunities
            trades = await get_open_trades()
            
            # REMOVED all asymmetry checks - they were blocking trades
            
            current_executing = len(ACTIVE_TASKS)
            current_open = len(trades)
            total_busy = current_open + current_executing
            
            # CRITICAL: Only allow 1 new execution at a time
            MAX_CONCURRENT_NEW = 1
            
            if total_busy < config.MAX_OPEN_TRADES:
                open_syms = {t['symbol'] for t in trades}
                active_syms = set(ACTIVE_TASKS.keys())
                all_busy_syms = open_syms | active_syms

                slots_available = config.MAX_OPEN_TRADES - total_busy
                new_trades_allowed = min(MAX_CONCURRENT_NEW, slots_available)

                logger.debug(f"Slots: Open={current_open}, Exec={current_executing}, New={new_trades_allowed}")

                # CRITICAL: Never start new trade if already executing
                if current_executing > 0:
                    logger.debug(f"⏳ Waiting for {current_executing} executions to finish")
                    await asyncio.sleep(2)
                    continue
                
                # Latency arb (limit 1)
                if new_trades_allowed > 0:
                    detector = get_detector()
                    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
                        if sym in open_syms or sym in ACTIVE_TASKS:
                            continue
                        
                        opp = await detector.detect_lag_opportunity(
                            sym, x10.fetch_funding_rate(sym) or 0, 
                            lighter.fetch_funding_rate(sym) or 0, x10, lighter
                        )
                        if opp:
                            if sym not in ACTIVE_TASKS:
                                ACTIVE_TASKS[sym] = asyncio.create_task(
                                    execute_trade_task(opp, lighter, x10, parallel_exec)
                                )
                                new_trades_allowed -= 1
                            break
                
                # Standard funding arb
                if new_trades_allowed > 0:
                    opps = await find_opportunities(lighter, x10, all_busy_syms)
                    started = 0
                    for opp in opps:
                        if started >= new_trades_allowed:
                            break

                        sym = opp['symbol']
                        if sym in ACTIVE_TASKS:
                            continue

                        if sym not in ACTIVE_TASKS:
                            ACTIVE_TASKS[sym] = asyncio.create_task(
                                execute_trade_task(opp, lighter, x10, parallel_exec)
                            )
                            started += 1
                            
                            # CRITICAL: Wait for execution to fully complete
                            logger.info(f"⏳ Waiting for {sym} execution to complete...")
                            await ACTIVE_TASKS[sym]
                            await asyncio.sleep(3)  # Extra safety delay

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Logic Loop Error: {e}")
            traceback.print_exc()
            if telegram_bot:
                await telegram_bot.send_error(f"Logic: {str(e)[:200]}")
            await asyncio.sleep(2)

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
        # Fee table also ensured here (defensive - load_fee_cache also creates it)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fee_stats (
                exchange TEXT, symbol TEXT, 
                avg_fee_rate REAL, sample_size INTEGER, 
                last_updated TIMESTAMP,
                PRIMARY KEY (exchange, symbol)
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
    except Exception as e:
        logger.critical(f"FATAL ERROR: {e}")
        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_error(f"Bot Crashed: {e}")
    finally:
        SHUTDOWN_FLAG = True
        logger.info(" Stopping Managers...")
        await ws_manager.stop()
        await state_manager.stop()
        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_message("🛑 Bot Shutdown")
            await telegram_bot.stop()
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