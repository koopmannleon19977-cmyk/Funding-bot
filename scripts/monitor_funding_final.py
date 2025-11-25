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
from datetime import datetime, timezone
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
POSITION_CACHE_TTL = 30.0  # 10s -> 30s to reduce API calls
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

# Add small helper to return the global state manager instance used elsewhere
def get_state_manager():
    return state_manager

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
                try:
                    entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S.%f')
                except Exception:
                    try:
                        entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        entry_time = datetime.utcnow()
            elif not isinstance(entry_time, datetime):
                entry_time = datetime.utcnow()
            
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
    common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()
    logger.info(f"🔍 Scanning {len(common)} pairs. Open symbols to skip: {open_syms}")
    # Verify market data is loaded
    if not common:
        logger.warning("⚠️ No common markets found")
        logger.debug(f"X10 markets: {len(x10.market_info)}, Lighter: {len(lighter.market_info)}")
        return []
    
    # Verify price cache has data
    x10_prices = len([s for s in common if x10.fetch_mark_price(s) is not None])
    lit_prices = len([s for s in common if lighter.fetch_mark_price(s) is not None])
    
    logger.debug(f"Price cache status: X10={x10_prices}/{len(common)}, Lighter={lit_prices}/{len(common)}")
    
    if x10_prices == 0 and lit_prices == 0:
        logger.warning("⚠️ Price cache completely empty - WebSocket streams may not be working")
        # Force reload
        await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            lighter.load_funding_rates_and_prices()
        )

    logger.info(
        f"🔍 Scanning {len(common)} pairs. "
        f"Lighter markets: {len(lighter.market_info)}, X10 markets: {len(x10.market_info)}"
    )

    semaphore = asyncio.Semaphore(10)  # Reduced from 20 to avoid rate limits

    async def fetch_symbol_data(s: str):
        async with semaphore:
            try:
                await asyncio.sleep(0.05)
                
                # Get funding rates (from cache, updated by maintenance_loop)
                lr = lighter.fetch_funding_rate(s)
                xr = x10.fetch_funding_rate(s)
                
                # Get prices (from cache, updated by WebSocket)
                px = x10.fetch_mark_price(s)
                pl = lighter.fetch_mark_price(s)
                
                # Log missing data for debugging
                if lr is None or xr is None:
                    logger.debug(f"{s}: Missing rates (L={lr}, X={xr})")
                if px is None or pl is None:
                    logger.debug(f"{s}: Missing prices (X={px}, L={pl})")
                
                return (s, lr, xr, px, pl)
                
            except Exception as e:
                logger.debug(f"Error fetching {s}: {e}")
                return (s, None, None, None, None)

    # Launch concurrent fetches
    tasks = [asyncio.create_task(fetch_symbol_data(s)) for s in common]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions
    clean_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"Task exception: {r}")
            continue
        clean_results.append(r)

    # Update threshold manager
    current_rates = [lr for (_s, lr, _xr, _px, _pl) in clean_results if lr is not None]
    if current_rates:
        try:
            threshold_manager.update_metrics(current_rates)
        except Exception as e:
            logger.debug(f"Failed to update threshold metrics: {e}")

    now_ts = time.time()
    valid_pairs = 0
    
    for s, rl, rx, px, pl in clean_results:
        # Debug: Log filter checks
        if s in open_syms:
            logger.debug(f"🔄 Skip {s}: Already open (in open_syms)")
            continue
            
        if s in config.BLACKLIST_SYMBOLS:
            logger.debug(f"⛔ Skip {s}: In BLACKLIST")
            continue
            
        if is_tradfi_or_fx(s):
            logger.debug(f"⛔ Skip {s}: TradFi/FX")
            continue

        if s in FAILED_COINS and (now_ts - FAILED_COINS[s] < 300):
            logger.debug(f"⏰ Skip {s}: Failed cooldown")
            continue

        # Count valid data
        has_rates = rl is not None and rx is not None
        has_prices = px is not None and pl is not None
        
        if has_rates and has_prices:
            valid_pairs += 1

        if not has_rates:
            logger.debug(f"Skip {s}: Missing rates (L={rl}, X={rx})")
            continue

        # Update volatility monitor
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

        if not has_prices:
            logger.debug(f"Skip {s}: Missing prices (X={px}, L={pl})")
            continue
        
        # CRITICAL: Guard against division by zero AND invalid prices
        try:
            if px is None or pl is None or px <= 0 or pl <= 0:
                logger.debug(f"Skip {s}: Invalid prices (X={px}, L={pl})")
                continue
            
            # Safe spread calculation with explicit float conversion
            px_float = float(px)
            pl_float = float(pl)
            
            if px_float <= 0:
                logger.debug(f"Skip {s}: X10 price is zero/negative ({px_float})")
                continue
                
            spread = abs(px_float - pl_float) / px_float
            
            if spread > config.MAX_SPREAD_FILTER_PERCENT:
                logger.debug(f"Skip {s}: Spread {spread*100:.2f}% too high")
                continue
                
        except (TypeError, ValueError, ZeroDivisionError) as e:
            logger.debug(f"Skip {s}: Price calculation error - {e}")
            continue

        # Log high APY opportunities
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

    logger.info(f"✅ Found {len(opps)} opportunities from {valid_pairs} valid pairs (scanned {len(clean_results)})")
    
    opps.sort(key=lambda x: x['apy'], reverse=True)
    return opps[:config.MAX_OPEN_TRADES]
    return opps[:config.MAX_OPEN_TRADES]

async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    symbol = opp['symbol']
    try:
        await execute_trade_parallel(opp, lighter, x10, parallel_exec)
    except Exception as e:
        logger.error(f"Task {symbol} error: {e}")
    finally:
        # CRITICAL FIX: Always remove from ACTIVE_TASKS
        if symbol in ACTIVE_TASKS:
            try:
                del ACTIVE_TASKS[symbol]
                logger.debug(f"🧹 Cleaned task: {symbol}")
            except KeyError:
                pass

async def launch_trade_task(opp: Dict, lighter, x10, parallel_exec):
    """Launch trade execution in background task."""
    symbol = opp['symbol']
    
    # CRITICAL: Check FAILED_COINS before creating task
    if symbol in FAILED_COINS:
        cooldown = time.time() - FAILED_COINS[symbol]
        if cooldown < 180:  # 3min cooldown
            return
    
    if symbol in ACTIVE_TASKS:
        return

    async def _task_wrapper():
        try:
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)
        except Exception as e:
            logger.error(f"Task {symbol} error: {e}")
        finally:
            # CRITICAL: ALWAYS remove from ACTIVE_TASKS
            if symbol in ACTIVE_TASKS:
                try:
                    del ACTIVE_TASKS[symbol]
                    logger.debug(f"🧹 Cleaned task: {symbol}")
                except KeyError:
                    pass

    task = asyncio.create_task(_task_wrapper())
    ACTIVE_TASKS[symbol] = task

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

        # ═══════════════════════════════════════════════════════════════
        # CRITICAL FIX: CHECK BALANCE **BEFORE** RESERVATION
        # ═══════════════════════════════════════════════════════════════
        global IN_FLIGHT_MARGIN, IN_FLIGHT_LOCK
        reserved_amount = 0.0
        
        try:
            # STEP 1: GET AVAILABLE BALANCE (with lock)
            async with IN_FLIGHT_LOCK:
                raw_x10 = await x10.get_real_available_balance()
                raw_lit = await lighter.get_real_available_balance()
                
                bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
            
            # STEP 2: CALCULATE SIZE
            if opp.get('is_farm_trade'):
                final_usd = 16.0
            else:
                # ABORT EARLY if insufficient
                if bal_x10_real < 20.0 or bal_lit_real < 20.0:
                    logger.warning(
                        f"⚠️ Insufficient balance for {symbol}: "
                        f"X10=${bal_x10_real:.1f}, Lit=${bal_lit_real:.1f}"
                    )
                    FAILED_COINS[symbol] = time.time()
                    return False

                max_per_trade = min(bal_x10_real, bal_lit_real) * 0.20
                size_calc = calculate_smart_size(conf, bal_x10_real + bal_lit_real)
                size_calc *= vol_monitor.get_size_adjustment(symbol)
                final_usd = min(size_calc, max_per_trade, config.MAX_TRADE_SIZE_USD)

            # Exchange minimum
            min_req = max(x10.min_notional_usd(symbol), lighter.min_notional_usd(symbol))
            if final_usd < min_req:
                final_usd = float(min_req)

            # Absolute minimum
            if final_usd < 12.0:
                logger.debug(f"Size too small: ${final_usd:.1f}")
                return False

            # STEP 3: FINAL BALANCE CHECK BEFORE RESERVATION
            async with IN_FLIGHT_LOCK:
                # RE-CHECK after lock (race condition protection)
                raw_x10 = await x10.get_real_available_balance()
                raw_lit = await lighter.get_real_available_balance()
                
                bal_x10_check = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_check = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
                
                # ABORT if insufficient BEFORE reservation
                if bal_x10_check < final_usd or bal_lit_check < final_usd:
                    logger.error(
                        f"🚫 ABORT {symbol}: Insufficient balance - "
                        f"X10=${bal_x10_check:.1f}, Lit=${bal_lit_check:.1f}, Need=${final_usd:.1f}"
                    )
                    FAILED_COINS[symbol] = time.time()
                    # CRITICAL: Remove from ACTIVE_TASKS immediately
                    if symbol in ACTIVE_TASKS:
                        try:
                            del ACTIVE_TASKS[symbol]
                            logger.debug(f"🧹 Cleaned task after balance abort: {symbol}")
                        except KeyError:
                            pass
                    return False
                
                # STEP 4: NOW RESERVE (balance is confirmed sufficient)
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + final_usd
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + final_usd
                reserved_amount = final_usd
                
                logger.info(
                    f"🔒 Reserved ${reserved_amount:.1f} for {symbol} | "
                    f"In-Flight: X10=${IN_FLIGHT_MARGIN['X10']:.1f}, Lit=${IN_FLIGHT_MARGIN['Lighter']:.1f}"
                )

        except Exception as e:
            logger.error(f"Balance check/reservation error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False

        # SIDE MAPPING
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        side1 = opp['leg1_side']
        side2 = "SELL" if side1 == "BUY" else "BUY"

        x10_side = side1 if getattr(ex1, 'name', '') == 'X10' else side2
        lighter_side = side2 if getattr(ex1, 'name', '') == 'X10' else side1

        # PRE-FLIGHT POSITION CHECK
        try:
            x10_check = await x10.fetch_open_positions()
            lighter_check = await lighter.fetch_open_positions()
            
            if any(p['symbol'] == symbol for p in (x10_check or [])):
                logger.error(f"🚫 ABORT {symbol}: Already open on X10")
                FAILED_COINS[symbol] = time.time()
                return False
            
            if any(p['symbol'] == symbol for p in (lighter_check or [])):
                logger.error(f"🚫 ABORT {symbol}: Already open on Lighter")
                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.warning(f"Pre-flight check error {symbol}: {e}")

        # EXECUTE PARALLEL
        try:
            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f}, X10={x10_side}, Lit={lighter_side}")
            
            # EXECUTE PARALLEL
            success_x10, success_lit = False, False
            x10_fill_price, lit_fill_price = None, None
            
            try:
                # Fire both simultaneously
                x10_task = asyncio.create_task(
                    x10.open_live_position(symbol, x10_side, final_usd, post_only=True)
                )
                lit_task = asyncio.create_task(
                    lighter.open_live_position(symbol, lighter_side, final_usd, post_only=False)
                )
                
                results = await asyncio.gather(x10_task, lit_task, return_exceptions=True)
                x10_result, lit_result = results
                
                # Check X10
                if isinstance(x10_result, Exception):
                    logger.error(f"❌ X10 {symbol} failed: {x10_result}")
                else:
                    success_x10, x10_fill_price = x10_result
                
                # Check Lighter
                if isinstance(lit_result, Exception):
                    logger.error(f"❌ Lighter {symbol} failed: {lit_result}")
                else:
                    success_lit, lit_fill_price = lit_result
                
                # ROLLBACK if partial fill
                if success_x10 and not success_lit:
                    logger.error(f"🔄 ROLLBACK {symbol}: X10 filled, Lighter failed")
                    await asyncio.sleep(2)
                    await x10.close_live_position(symbol, x10_side, final_usd)
                    FAILED_COINS[symbol] = time.time()
                    return False
                
                if success_lit and not success_x10:
                    logger.error(f"🔄 ROLLBACK {symbol}: Lighter filled, X10 failed")
                    await asyncio.sleep(2)
                    await lighter.close_live_position(symbol, lighter_side, final_usd)
                    FAILED_COINS[symbol] = time.time()
                    return False
                
                # SUCCESS
                if success_x10 and success_lit:
                    logger.info(f"✅ {symbol} opened successfully")
                    
                    # Store trade
                    trade_data = {
                        'symbol': symbol,
                        'entry_time': datetime.now(timezone.utc),
                        'notional_usd': final_usd,
                        'status': 'OPEN',
                        'leg1_exchange': opp['leg1_exchange'],
                        'initial_spread_pct': opp.get('spread_pct', 0.0),
                        'initial_funding_rate_hourly': abs(net_rate),
                        'entry_price_x10': x10_fill_price,
                        'entry_price_lighter': lit_fill_price,
                        'is_farm_trade': opp.get('is_farm_trade', False),
                        'account_label': current_label
                    }
                    
                    state_mgr = get_state_manager()
                    await state_mgr.add_trade(trade_data)
                    
                    vol_monitor.record_entry(symbol)
                    return True
                
                # BOTH FAILED
                logger.error(f"❌ {symbol} failed: Both legs failed")
                FAILED_COINS[symbol] = time.time()
                return False
                
            except Exception as e:
                logger.error(f"Execution error {symbol}: {e}")
                import traceback
                traceback.print_exc()
                
                # CLEANUP any partial fills
                await asyncio.sleep(2)
                cleanup_x10 = await x10.fetch_open_positions()
                cleanup_lit = await lighter.fetch_open_positions()
                
                x10_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_x10 or []))
                lit_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_lit or []))
                
                if x10_ghost:
                    logger.error(f"🧟 Cleaning X10 ghost for {symbol}")
                    await x10.close_live_position(symbol, x10_side, final_usd)
                
                if lit_ghost:
                    logger.error(f"🧟 Cleaning Lighter ghost for {symbol}")
                    await lighter.close_live_position(symbol, lighter_side, final_usd)
                
                FAILED_COINS[symbol] = time.time()
                return False

        finally:
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: ALWAYS RELEASE MARGIN ON **ANY** EXIT PATH
            # ═══════════════════════════════════════════════════════════════
            if reserved_amount > 0.01:
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] = max(0.0, IN_FLIGHT_MARGIN.get('X10', 0.0) - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(0.0, IN_FLIGHT_MARGIN.get('Lighter', 0.0) - reserved_amount)
                    
                    logger.info(
                        f"🔓 Released ${reserved_amount:.1f} for {symbol} | "
                        f"Remaining: X10=${IN_FLIGHT_MARGIN['X10']:.1f}, Lit=${IN_FLIGHT_MARGIN['Lighter']:.1f}"
                    )

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
    """Fetch positions with proper caching and error handling"""
    now = time.time()
    
    # Always force refresh if cache is empty or stale
    cache_age = now - POSITION_CACHE['last_update']
    cache_empty = (len(POSITION_CACHE['x10']) == 0 and len(POSITION_CACHE['lighter']) == 0)
    
    if not force and not cache_empty and cache_age < POSITION_CACHE_TTL:
        logger.debug(f"Using cached positions (age: {cache_age:.1f}s)")
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    
    try:
        logger.debug(f"Fetching fresh positions (force={force}, cache_age={cache_age:.1f}s)")
        
        # Fetch with timeout
        t1 = asyncio.create_task(x10.fetch_open_positions())
        t2 = asyncio.create_task(lighter.fetch_open_positions())
        
        p_x10, p_lit = await asyncio.wait_for(
            asyncio.gather(t1, t2, return_exceptions=True),
            timeout=10.0
        )
        
        # Handle exceptions
        if isinstance(p_x10, Exception):
            logger.error(f"X10 position fetch failed: {p_x10}")
            p_x10 = POSITION_CACHE.get('x10', [])
        
        if isinstance(p_lit, Exception):
            logger.error(f"Lighter position fetch failed: {p_lit}")
            p_lit = POSITION_CACHE.get('lighter', [])
        
        # Ensure lists
        p_x10 = p_x10 if isinstance(p_x10, list) else []
        p_lit = p_lit if isinstance(p_lit, list) else []
        
        # Update cache
        POSITION_CACHE['x10'] = p_x10
        POSITION_CACHE['lighter'] = p_lit
        POSITION_CACHE['last_update'] = now
        
        logger.info(f"📊 Positions: X10={len(p_x10)}, Lighter={len(p_lit)}")
        
        # Debug log actual positions
        if p_x10:
            logger.debug(f"X10 positions: {[p.get('symbol') for p in p_x10]}")
        if p_lit:
            logger.debug(f"Lighter positions: {[p.get('symbol') for p in p_lit]}")
        
        return p_x10, p_lit
        
    except asyncio.TimeoutError:
        logger.error("Position fetch timeout, using stale cache")
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    except Exception as e:
        logger.error(f"Position cache error: {e}")
        import traceback
        traceback.print_exc()
        return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])

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
    """Full Zombie Cleanup Implementation with Correct Side Detection"""
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
            logger.warning(f"🧟 ZOMBIES DETECTED: {zombies}")
            for sym in zombies:
                # X10 Zombie Cleanup
                if sym in x_syms:
                    p = next((pos for pos in x_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = p.get('size', 0)

                        # CRITICAL FIX: Determine close side based on CURRENT position
                        # Positive size = LONG position → close with SELL
                        # Negative size = SHORT position → close with BUY
                        if position_size > 0:
                            close_side = "SELL"  # Close LONG
                            original_side = "BUY"
                        else:
                            close_side = "BUY"   # Close SHORT
                            original_side = "SELL"

                        size_usd = abs(position_size) * (x10.fetch_mark_price(sym) or 0.0)

                        if size_usd < 1.0:
                            logger.warning(f"⚠️ X10 zombie {sym} too small (${size_usd:.2f}), skipping")
                            continue

                        logger.info(f"🔻 Closing X10 zombie {sym}: pos_size={position_size:.6f}, close={close_side}, ${size_usd:.1f}")

                        # Use close_live_position with ORIGINAL side (it handles inversion internally)
                        try:
                            success, _ = await x10.close_live_position(sym, original_side, size_usd)
                            if not success:
                                logger.error(f"❌ Failed to close X10 zombie {sym}")
                            else:
                                logger.info(f"✅ Closed X10 zombie {sym}")
                        except Exception as e:
                            logger.error(f"X10 zombie close exception for {sym}: {e}")

                # Lighter Zombie Cleanup
                if sym in l_syms:
                    p = next((pos for pos in l_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = p.get('size', 0)

                        # CRITICAL FIX: Same logic for Lighter
                        if position_size > 0:
                            close_side = "SELL"
                            original_side = "BUY"
                        else:
                            close_side = "BUY"
                            original_side = "SELL"

                        size_usd = abs(position_size) * (lighter.fetch_mark_price(sym) or 0.0)

                        if size_usd < 1.0:
                            logger.warning(f"⚠️ Lighter zombie {sym} too small (${size_usd:.2f}), skipping")
                            continue

                        logger.info(f"🔻 Closing Lighter zombie {sym}: pos_size={position_size:.6f}, close={close_side}, ${size_usd:.1f}")

                        try:
                            success, _ = await lighter.close_live_position(sym, original_side, size_usd)
                            if not success:
                                logger.error(f"❌ Failed to close Lighter zombie {sym}")
                            else:
                                logger.info(f"✅ Closed Lighter zombie {sym}")
                        except Exception as e:
                            logger.error(f"Lighter zombie close exception for {sym}: {e}")

        # 2. DB Ghosts (Open in DB, Closed on Exchange)
        ghosts = db_syms - all_exchange
        if ghosts:
            logger.warning(f"👻 GHOSTS DETECTED: {ghosts}")
            for sym in ghosts:
                logger.info(f"Closing ghost {sym} in DB")
                try:
                    await close_trade_in_state(sym)
                except Exception as e:
                    logger.error(f"Failed to close ghost {sym} in DB: {e}")

    except Exception as e:
        logger.error(f"Zombie Cleanup Error: {e}")

async def farm_loop(lighter, x10, parallel_exec):
    """Aggressive Volume Farming Loop"""
    if not config.VOLUME_FARM_MODE:
        logger.info("🚜 Farm Mode disabled - exiting")
        return

    logger.info("🚜 Farming Loop started.")
    while True:
        try:
            trades = await get_open_trades()
            if len(trades) >= config.FARM_MAX_CONCURRENT:
                await asyncio.sleep(1)
                continue

            open_syms = {t['symbol'] for t in trades}

            # DISABLED: Farm loop creates zombies - use normal opportunity finding instead
            # TODO: Re-enable after zombie fix verified
            await asyncio.sleep(60)
            continue

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
                await asyncio.wait_for(price_event.wait(), timeout=0.5)  # 5.0 → 0.5 for faster reaction
                logger.debug("📡 Price update event triggered")
            except asyncio.TimeoutError:
                pass  # Kein Log spam
            price_event.clear()
            
            # Verify WebSocket streams are alive
            x10_cache_size = len(x10.price_cache)
            lit_cache_size = len(lighter.price_cache)
            
            if x10_cache_size == 0 and lit_cache_size == 0:
                logger.warning("⚠️ Both price caches empty - WebSocket streams may be down")

            # Cleanup
            await cleanup_finished_tasks()

            # SINGLE call to get_open_trades - use everywhere
            try:
                current_trades = await get_open_trades()
            except Exception as e:
                logger.error(f"Error fetching open trades: {e}")
                current_trades = []

            open_syms = {t['symbol'] for t in current_trades}

            # Remove zombie tasks (only remove if task is finished)
            zombie_tasks = [s for s, t in ACTIVE_TASKS.items() if s in open_syms and t.done()]
            for sym in zombie_tasks:
                logger.warning(f"🧟 Removing zombie task: {sym}")
                try:
                    del ACTIVE_TASKS[sym]
                except KeyError:
                    pass

            # Manage trades (use same current_trades)
            if management_task is None or management_task.done():
                if management_task and management_task.exception():
                    logger.error(f"Manage Task Error: {management_task.exception()}")
                # Pass current_trades to avoid second call (manage_open_trades may fetch again internally)
                management_task = asyncio.create_task(manage_open_trades(lighter, x10))

            # Zombie cleanup (run at most once every 180 seconds)
            now = time.monotonic()
            if now - last_zombie > 180:
                try:
                    await cleanup_zombie_positions(lighter, x10)
                    
                    # Clear old failed coins (older than 5min)
                    expired = [s for s, t in FAILED_COINS.items() if time.time() - t > 300]
                    for s in expired:
                        try:
                            del FAILED_COINS[s]
                            logger.info(f"🔄 Cleared cooldown: {s}")
                        except KeyError:
                            pass
                except Exception as ze:
                    logger.error(f"Zombie cleanup: {ze}")
                # record monotonic timestamp to throttle next run
                last_zombie = now

            # Check opportunities (use cached current_trades)
            logger.debug(f"📊 Current trades: {len(current_trades)}, symbols: {open_syms}")

            # REMOVED all asymmetry checks - they were blocking trades

            current_executing = len(ACTIVE_TASKS)
            current_open = len(current_trades)
            total_busy = current_open + current_executing
            
            # CRITICAL: Only allow 1 new execution at a time
            MAX_CONCURRENT_NEW = 1
            
            if total_busy < config.MAX_OPEN_TRADES:
                open_syms = {t['symbol'] for t in current_trades}
                active_syms = set(ACTIVE_TASKS.keys())
                all_busy_syms = open_syms | active_syms

                slots_available = config.MAX_OPEN_TRADES - total_busy
                new_trades_allowed = min(MAX_CONCURRENT_NEW, slots_available)

                logger.debug(f"Slots: Open={current_open}, Exec={current_executing}, New={new_trades_allowed}")

                
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
                            logger.debug(f"⛔ Stopping: Max new trades reached ({new_trades_allowed})")
                            break

                        sym = opp['symbol']
                        
                        # Log EVERY opportunity with reason
                        if sym in ACTIVE_TASKS:
                            logger.info(f"❌ Skip {sym} (APY={opp.get('apy', 0):.1f}%): Already executing")
                            continue
                        
                        if sym in config.BLACKLIST_SYMBOLS:
                            logger.info(f"❌ Skip {sym} (APY={opp.get('apy', 0):.1f}%): BLACKLISTED")
                            continue
                        
                        if sym in FAILED_COINS:
                            cooldown_total = 300
                            cooldown_elapsed = time.time() - FAILED_COINS.get(sym, 0)
                            cooldown_remaining = max(0, cooldown_total - cooldown_elapsed)
                            logger.info(f"❌ Skip {sym} (APY={opp.get('apy', 0):.1f}%): FAILED cooldown ({cooldown_remaining:.0f}s left)")
                            continue
                        
                        # Should execute!
                        logger.info(f"🚀 EXECUTING {sym}: APY={opp.get('apy', 0):.1f}%, Net={opp.get('net_funding_hourly', 0):.6f}")

                        ACTIVE_TASKS[sym] = asyncio.create_task(
                            execute_trade_task(opp, lighter, x10, parallel_exec)
                        )
                        started += 1
                        
                        # Wait for execution
                        logger.info(f"⏳ Waiting for {sym} execution to complete...")
                        await ACTIVE_TASKS[sym]
                        await asyncio.sleep(3)

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
    
    # 3. Load Market Data FIRST (CRITICAL - Required for WebSocket subscriptions)
    logger.info("📊 Loading Market Data...")
    try:
        # PARALLEL statt sequential: gather with return_exceptions to inspect errors
        load_results = await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            return_exceptions=True
        )

        # Check for errors from the parallel load
        for i, result in enumerate(load_results):
            if isinstance(result, Exception):
                logger.error(f"Market load failed: {result}")

        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)

        logger.info(f"✅ Markets loaded: X10={x10_count}, Lighter={lit_count}")

        if x10_count == 0 and lit_count == 0:
            raise ValueError("No markets loaded from any exchange")

        if lit_count == 0:
            logger.warning("⚠️ Lighter markets not loaded - bot will use X10 only")

        if x10_count == 0:
            logger.warning("⚠️ X10 markets not loaded - bot will use Lighter only")
        
        # Load initial funding rates
        logger.info("📈 Loading initial funding rates...")
        await lighter.load_funding_rates_and_prices()
        
        # Verify data loaded
        test_syms = ["BTC-USD", "ETH-USD", "SOL-USD"]
        loaded_ok = False
        for sym in test_syms:
            if sym in x10.market_info and sym in lighter.market_info:
                px = x10.fetch_mark_price(sym)
                pl = lighter.fetch_mark_price(sym)
                if px and pl and px > 0 and pl > 0:
                    logger.info(f"✅ Data OK: {sym} X10=${px:.2f}, Lighter=${pl:.2f}")
                    loaded_ok = True
                    break
        
        if not loaded_ok:
            raise ValueError("No valid market data after load")
            
    except Exception as e:
        logger.critical(f"❌ FATAL: Market data load failed: {e}")
        return
    
    # 4. NOW Start WebSocket Streams (AFTER markets loaded)
    logger.info("🌐 Starting WebSocket streams...")
    ws_manager = WebSocketManager([x10, lighter])
    await ws_manager.start()
    
    # Wait for WS connections and verify health
    logger.info("⏳ Waiting for WebSocket connections...")
    await asyncio.sleep(5)
    
    # Verify WebSocket streams are receiving data
    logger.info("🔍 Verifying WebSocket health...")
    for check in range(3):
        await asyncio.sleep(2)
        x10_cache_size = len(x10.price_cache)
        lit_cache_size = len(lighter.price_cache)
        
        logger.info(f"📊 Cache status: X10={x10_cache_size} prices, Lighter={lit_cache_size} prices")
        
        if x10_cache_size > 0 or lit_cache_size > 0:
            logger.info("✅ WebSocket streams healthy - receiving price updates")
            break
            
        if check == 2:
            logger.warning("⚠️ WebSocket streams not receiving data - proceeding anyway")

    # 5. Init Managers
    parallel_exec = ParallelExecutionManager(x10, lighter)
    
    # 6. Start Background Loops
    logger.info("🚀 Spawning Tasks...")
    tasks = [
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