# src/core/monitoring.py
"""
Monitoring and background loops.

This module handles:
- Connection watchdog
- Task cleanup
- Emergency position cleanup
- Farm loop (volume farming)
"""

import asyncio
import logging
import time
from typing import List

import config
from src.utils import safe_float
from src.telegram_bot import get_telegram_bot

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared references - set from main.py)
# ============================================================
SHUTDOWN_FLAG = False
LAST_DATA_UPDATE = 0.0
WATCHDOG_TIMEOUT = 120.0
ACTIVE_TASKS = {}
TASKS_LOCK = asyncio.Lock()
LAST_ARBITRAGE_LAUNCH = 0.0


# ============================================================
# LAZY IMPORTS
# ============================================================
def _get_state_functions():
    """Lazy import to avoid circular dependencies"""
    from src.core.state import get_open_trades
    return get_open_trades


def _get_trade_management():
    """Lazy import trade management"""
    from src.core.trade_management import (
        get_cached_positions,
        cleanup_zombie_positions
    )
    return get_cached_positions, cleanup_zombie_positions


# ============================================================
# CONNECTION WATCHDOG
# ============================================================
async def connection_watchdog(ws_manager=None, x10=None, lighter=None):
    """Watchdog to detect connection timeouts."""
    global LAST_DATA_UPDATE, SHUTDOWN_FLAG
    
    logger.info("🐕 Connection Watchdog gestartet (Timeout: 120s)")
    
    while not SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(30)
            
            if SHUTDOWN_FLAG:
                break
            
            now = time.time()
            time_since_update = now - LAST_DATA_UPDATE
            
            if time_since_update > WATCHDOG_TIMEOUT:
                logger.warning(
                    f"⚠️ WATCHDOG ALARM: Keine Daten seit {time_since_update:.0f}s! "
                    f"(Limit: {WATCHDOG_TIMEOUT}s)"
                )
                
                # Telegram Alert
                telegram = get_telegram_bot()
                if telegram and telegram.enabled:
                    await telegram.send_error(
                        f"🐕 Connection Watchdog Alert!\n"
                        f"Keine Daten seit {time_since_update:.0f}s\n"
                        f"Versuche Reconnect..."
                    )
                
                # Try reconnect
                if ws_manager:
                    try:
                        logger.info("🔄 Watchdog initiiert WebSocket Reconnect...")
                        await ws_manager.reconnect_all()
                        LAST_DATA_UPDATE = time.time()
                        logger.info("✅ Watchdog Reconnect erfolgreich")
                    except Exception as e:
                        logger.error(f"❌ Watchdog Reconnect fehlgeschlagen: {e}")
                
                # Fallback: REST refresh
                if x10 and lighter:
                    try:
                        logger.info("🔄 Watchdog: Force-Refresh Market Data via REST...")
                        await asyncio.gather(
                            x10.load_market_cache(force=True),
                            lighter.load_market_cache(force=True),
                            return_exceptions=True
                        )
                        LAST_DATA_UPDATE = time.time()
                        logger.info("✅ Watchdog REST Refresh erfolgreich")
                    except Exception as e:
                        logger.error(f"❌ Watchdog REST Refresh fehlgeschlagen: {e}")
                
                # Critical: Exit if still dead after 2x timeout
                if time_since_update > WATCHDOG_TIMEOUT * 2:
                    logger.critical(
                        f"🚨 CRITICAL: Keine Daten seit {time_since_update:.0f}s! "
                        "Bot wird beendet (Supervisor startet neu)..."
                    )
                    if telegram and telegram.enabled:
                        await telegram.send_error(
                            "🚨 Bot CRITICAL ERROR - Neustart erforderlich"
                        )
                    SHUTDOWN_FLAG = True
                    break
            else:
                logger.debug(f"🐕 Watchdog OK: Daten-Alter {time_since_update:.1f}s")
        
        except asyncio.CancelledError:
            logger.info("Connection watchdog cancelled")
            break
        except Exception as e:
            logger.error(f"Connection watchdog error: {e}")
            await asyncio.sleep(30)


# ============================================================
# TASK CLEANUP
# ============================================================
async def cleanup_finished_tasks():
    """Background task: Remove completed tasks from ACTIVE_TASKS.
    
    Note: ACTIVE_TASKS may contain:
    - asyncio.Task objects (actual running tasks)
    - "RESERVED" strings (placeholder for slots being prepared)
    
    We only clean up actual Task objects that are .done().
    """
    global SHUTDOWN_FLAG
    
    while not SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(5)
            
            async with TASKS_LOCK:
                # Find finished tasks - only check actual Task objects, not "RESERVED" placeholders
                finished = [
                    sym for sym, task in ACTIVE_TASKS.items()
                    if isinstance(task, asyncio.Task) and task.done()
                ]
                for sym in finished:
                    try:
                        task = ACTIVE_TASKS.pop(sym, None)
                        if task and isinstance(task, asyncio.Task):
                            # Retrieve exception to prevent "exception never retrieved"
                            try:
                                task.result()
                            except Exception:
                                pass
                    except Exception:
                        pass
                
                if finished:
                    logger.debug(f"🧹 Cleaned {len(finished)} finished tasks: {finished}")
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Task cleanup error: {e}")
            await asyncio.sleep(5)


# ============================================================
# EMERGENCY CLEANUP
# ============================================================
async def emergency_position_cleanup(lighter, x10, max_age_hours: float = 48.0, interval_seconds: int = 3600):
    """Emergency background task to perform periodic zombie cleanup."""
    global SHUTDOWN_FLAG
    
    _, cleanup_zombie_positions = _get_trade_management()
    
    logger.info(f"Starting emergency_position_cleanup (interval={interval_seconds}s)")
    try:
        while not SHUTDOWN_FLAG:
            try:
                await cleanup_zombie_positions(lighter, x10)
            except Exception as e:
                logger.error(f"Emergency cleanup iteration failed: {e}")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("emergency_position_cleanup cancelled")
    except Exception as e:
        logger.error(f"emergency_position_cleanup error: {e}")


# ============================================================
# FARM LOOP
# ============================================================
async def farm_loop(lighter, x10, parallel_exec):
    """Volume Farming loop."""
    global SHUTDOWN_FLAG, LAST_ARBITRAGE_LAUNCH
    
    get_open_trades = _get_state_functions()
    get_cached_positions, _ = _get_trade_management()
    
    last_trades: List[float] = []

    while True:
        if SHUTDOWN_FLAG:
            logger.info("Farm loop: SHUTDOWN_FLAG detected, exiting")
            break
        try:
            if not config.VOLUME_FARM_MODE:
                await asyncio.sleep(60)
                continue

            logger.info("🚜 Farm Mode ACTIVE")
            if SHUTDOWN_FLAG:
                break
            
            # Block if arbitrage was active recently
            if time.time() - LAST_ARBITRAGE_LAUNCH < 10.0:
                await asyncio.sleep(1)
                continue
            
            # Rate limiting
            now = time.time()
            last_trades = [t for t in last_trades if now - t < 60]
            
            burst_limit = getattr(config, 'FARM_BURST_LIMIT', 10)
            if len(last_trades) >= burst_limit:
                oldest = last_trades[0]
                wait_time = 60 - (now - oldest)
                logger.info(f"🚜 Rate limited: wait {wait_time:.1f}s")
                await asyncio.sleep(max(wait_time, 5))
                continue
            
            # Check open trades
            trades = await get_open_trades()
            farm_count = sum(1 for t in trades if t.get('is_farm_trade'))
            open_symbols_db = {t['symbol'] for t in trades}
            
            async with TASKS_LOCK:
                executing_symbols = set(ACTIVE_TASKS.keys())
            
            # Check real exchange positions
            try:
                x10_pos, lighter_pos = await get_cached_positions(lighter, x10, force=False)
                real_x10_symbols = {
                    p.get('symbol') for p in (x10_pos or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                }
                real_lighter_symbols = {
                    p.get('symbol') for p in (lighter_pos or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                }
                real_exchange_symbols = real_x10_symbols | real_lighter_symbols
            except Exception:
                real_exchange_symbols = set()
            
            all_active_symbols = open_symbols_db | executing_symbols | real_exchange_symbols
            current_total_count = len(all_active_symbols)
            
            total_limit = getattr(config, 'MAX_OPEN_TRADES', 3)
            
            if current_total_count >= total_limit:
                logger.debug(f"🚜 Farm: Slots full ({current_total_count}/{total_limit})")
                await asyncio.sleep(5)
                continue

            if farm_count >= getattr(config, 'FARM_MAX_CONCURRENT', 3):
                await asyncio.sleep(10)
                continue

            # Farm logic would continue here...
            # (Finding opportunities and executing trades)
            await asyncio.sleep(getattr(config, 'FARM_CHECK_INTERVAL', 10))
            
        except asyncio.CancelledError:
            logger.info("Farm loop cancelled")
            break
        except Exception as e:
            logger.error(f"❌ Farm Error: {e}")
            await asyncio.sleep(30)


# ============================================================
# TRADE MANAGEMENT LOOP
# ============================================================
async def trade_management_loop(lighter, x10, manage_open_trades_fn=None):
    """Monitors open trades and closes them based on criteria (Farm-Timer, TP/SL, etc.)."""
    global SHUTDOWN_FLAG
    
    logger.info("🛡️ Trade Management Loop gestartet")
    
    while not SHUTDOWN_FLAG:
        try:
            if manage_open_trades_fn:
                await manage_open_trades_fn(lighter, x10)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Trade Management Error: {e}")
            await asyncio.sleep(5)


# ============================================================
# MAINTENANCE LOOP
# ============================================================
async def maintenance_loop(lighter, x10, parallel_exec=None, sync_check_fn=None):
    """Background tasks: Funding rates refresh + REST Fallback + Sync Check."""
    global LAST_DATA_UPDATE, SHUTDOWN_FLAG
    
    funding_refresh_interval = 30
    sync_check_interval = 300
    last_x10_refresh = 0
    last_lighter_refresh = 0
    last_sync_check = 0
    
    while not SHUTDOWN_FLAG:
        try:
            now = time.time()
            LAST_DATA_UPDATE = now
            
            # X10 Funding Refresh
            x10_funding_count = len(getattr(x10, 'funding_cache', {}))
            if x10_funding_count < 20 or (now - last_x10_refresh > 60):
                logger.debug(f"📊 X10 Funding Cache refresh (current={x10_funding_count})")
                try:
                    await x10.load_market_cache(force=True)
                    last_x10_refresh = now
                    logger.debug(f"X10: Funding Refresh OK, cache={len(x10.funding_cache)}")
                except Exception as e:
                    logger.warning(f"X10 Funding Refresh failed: {e}")

            # Lighter Funding Refresh
            lighter_funding_count = len(getattr(lighter, 'funding_cache', {}))
            if lighter_funding_count < 50 or (now - last_lighter_refresh > 60):
                logger.debug(f"📊 Lighter Funding Cache refresh (current={lighter_funding_count})")
                try:
                    await lighter.load_funding_rates_and_prices()
                    last_lighter_refresh = now
                    logger.debug(f"Lighter: Funding Refresh OK, cache={len(lighter.funding_cache)}")
                except Exception as e:
                    logger.warning(f"Lighter Funding Refresh failed: {e}")
            
            # Exchange Sync Check (every 5 minutes)
            if now - last_sync_check > sync_check_interval:
                logger.info("🔍 Running periodic exchange sync check...")
                try:
                    if sync_check_fn:
                        await sync_check_fn(lighter, x10, parallel_exec)
                    last_sync_check = now
                except Exception as e:
                    logger.error(f"Sync check failed: {e}")
            
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")
            await asyncio.sleep(30)


# ============================================================
# HEALTH REPORTER
# ============================================================
async def health_reporter(event_loop, parallel_exec):
    """Periodic health status reporter."""
    global SHUTDOWN_FLAG
    
    interval = 3600  # 1 hour
    telegram = get_telegram_bot()
    
    while not SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(interval)
            
            if hasattr(event_loop, 'get_task_status'):
                task_status = event_loop.get_task_status()
            else:
                task_status = {}
            
            if hasattr(parallel_exec, 'get_execution_stats'):
                exec_stats = parallel_exec.get_execution_stats()
            else:
                exec_stats = {}
            
            running = sum(1 for t in task_status.values() if isinstance(t, dict) and t.get("status") == "running")
            total = len(task_status)
            
            logger.info(
                f"📊 Health: {running}/{total} tasks running, "
                f"{exec_stats.get('total_executions', 0)} trades, "
                f"{exec_stats.get('pending_rollbacks', 0)} pending rollbacks"
            )
            
            # Fee Stats
            try:
                from src.fee_manager import get_fee_manager
                fee_manager = get_fee_manager()
                fee_stats = fee_manager.get_stats()
                logger.info(
                    f"📊 FEES: X10={fee_stats['x10']['taker']:.4%} ({fee_stats['x10']['source']}) | "
                    f"Lighter={fee_stats['lighter']['taker']:.4%} ({fee_stats['lighter']['source']})"
                )
            except Exception:
                pass
            
            # Send to Telegram
            if telegram and telegram.enabled:
                msg = (
                    f"📊 **Health Report**\n"
                    f"Tasks: {running}/{total} running\n"
                    f"Trades: {exec_stats.get('successful', 0)} ✅ / {exec_stats.get('failed', 0)} ❌\n"
                    f"Rollbacks: {exec_stats.get('rollbacks_successful', 0)} ✅ / {exec_stats.get('rollbacks_failed', 0)} ❌"
                )
                await telegram.send_message(msg)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Health reporter error: {e}")
            await asyncio.sleep(60)


# ============================================================
# BALANCE WATCHDOG (DISABLED)
# ============================================================
async def balance_watchdog():
    """DISABLED – X10 Starknet Migration makes balance detection unreliable."""
    return


# ============================================================
# GLOBALS FOR LOGIC LOOP
# ============================================================
FAILED_COINS = {}
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()


# ============================================================
# MAIN TRADING LOGIC LOOP
# ============================================================
async def logic_loop(lighter, x10, price_event, parallel_exec):
    """
    Main trading loop with opportunity detection and execution.
    
    This function:
    1. Runs reconciliation when idle
    2. Finds trading opportunities
    3. Checks balances
    4. Launches trades with slot reservation
    """
    global LAST_DATA_UPDATE, LAST_ARBITRAGE_LAUNCH, SHUTDOWN_FLAG, ACTIVE_TASKS
    
    # Lazy imports
    from src.core.state import get_open_trades, get_symbol_lock
    from src.core.opportunities import find_opportunities
    from src.core.trading import execute_trade_parallel
    from src.core.trade_management import (
        get_cached_positions, 
        reconcile_state_with_exchange
    )
    
    REFRESH_DELAY = getattr(config, 'REFRESH_DELAY_SECONDS', 5)
    logger.info(f"Logic Loop gestartet – REFRESH alle {REFRESH_DELAY}s")

    while not SHUTDOWN_FLAG:
        try:
            LAST_DATA_UPDATE = time.time()
            
            # --- Reconciliation when idle ---
            if parallel_exec and hasattr(parallel_exec, 'is_busy') and not parallel_exec.is_busy():
                await reconcile_state_with_exchange(lighter, x10, parallel_exec)
            
            open_trades = await get_open_trades()
            open_syms = {t['symbol'] for t in open_trades}
            
            max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
            if len(open_trades) >= max_trades:
                logger.debug(f"MAX_OPEN_TRADES ({max_trades}) reached, waiting...")
                await asyncio.sleep(REFRESH_DELAY)
                continue

            opportunities = await find_opportunities(lighter, x10, open_syms, is_farm_mode=None)
            
            if opportunities:
                logger.info(f"🎯 Found {len(opportunities)} opportunities")
                
                # Balance Check
                try:
                    bal_x10 = await x10.get_real_available_balance()
                except Exception as e:
                    logger.error(f"❌ X10 Balance Check FAILED: {e}")
                    bal_x10 = 0.0
                
                try:
                    bal_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    logger.error(f"❌ Lighter Balance Check FAILED: {e}")
                    bal_lit = 0.0

                if bal_x10 < 5.0:
                    logger.warning(f"⚠️ X10 Balance too low (${bal_x10:.2f})")
                    await asyncio.sleep(REFRESH_DELAY * 2)
                    continue

                # Calculate slots
                open_trades_refreshed = await get_open_trades()
                open_symbols_db = {t.get('symbol') for t in open_trades_refreshed}
                current_open_count = len(open_symbols_db)
                
                async with TASKS_LOCK:
                    executing_symbols = set(ACTIVE_TASKS.keys())
                
                # Get real positions
                # ═══════════════════════════════════════════════════════════════
                # FIX: During shutdown, use cached positions but also check shutdown flag
                # ═══════════════════════════════════════════════════════════════
                try:
                    # Check shutdown before fetching positions
                    if SHUTDOWN_FLAG:
                        logger.debug("🚫 Shutdown detected - skipping position fetch")
                        await asyncio.sleep(REFRESH_DELAY)
                        continue
                    
                    x10_pos, lighter_pos = await get_cached_positions(lighter, x10, force=False)
                    real_x10_symbols = {
                        p.get('symbol') for p in (x10_pos or [])
                        if abs(safe_float(p.get('size', 0))) > 1e-8
                    }
                    real_lighter_symbols = {
                        p.get('symbol') for p in (lighter_pos or [])
                        if abs(safe_float(p.get('size', 0))) > 1e-8
                    }
                    real_exchange_symbols = real_x10_symbols | real_lighter_symbols
                except Exception as e:
                    logger.warning(f"Failed to fetch real positions: {e}")
                    real_exchange_symbols = set()
                
                all_active_symbols = open_symbols_db | executing_symbols | real_exchange_symbols
                current_total_count = len(all_active_symbols)
                
                slots_available = max_trades - current_total_count

                if slots_available <= 0:
                    logger.debug(f"⛔ Max trades reached ({current_total_count}/{max_trades})")
                    await asyncio.sleep(REFRESH_DELAY)
                    continue

                # Sort opportunities by APY
                opportunities.sort(key=lambda x: x.get('apy', 0), reverse=True)

                # Select candidates
                trades_to_launch = []
                existing_symbols = open_symbols_db.copy()
                burst_limit = getattr(config, 'FARM_MAX_CONCURRENT_ORDERS', 3)
                launch_limit = min(slots_available, burst_limit)

                for opp in opportunities:
                    if len(trades_to_launch) >= launch_limit:
                        break
                    
                    symbol = opp.get('symbol')
                    if not symbol:
                        continue
                    
                    if symbol in existing_symbols:
                        continue
                    
                    if symbol in ACTIVE_TASKS:
                        continue
                    
                    if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 180):
                        continue
                    
                    trades_to_launch.append(opp)

                # Reserve slots atomically
                if trades_to_launch:
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Check shutdown flag before launching trades
                    # ═══════════════════════════════════════════════════════════════
                    if SHUTDOWN_FLAG:
                        logger.info("🚫 Shutdown detected - aborting trade launch")
                        await asyncio.sleep(REFRESH_DELAY)
                        continue
                    
                    LAST_ARBITRAGE_LAUNCH = time.time()
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Check for existing open orders to prevent duplicate trades
                    # This prevents starting a second trade while first one has partial fill
                    # ═══════════════════════════════════════════════════════════════
                    symbols_with_open_orders = set()
                    try:
                        if lighter and hasattr(lighter, 'get_open_orders'):
                            for opp in trades_to_launch:
                                sym = opp.get('symbol')
                                if sym:
                                    try:
                                        open_orders = await asyncio.wait_for(
                                            lighter.get_open_orders(sym),
                                            timeout=2.0
                                        )
                                        if open_orders and len(open_orders) > 0:
                                            symbols_with_open_orders.add(sym)
                                            logger.warning(f"⚠️ {sym}: Skipping launch - {len(open_orders)} open order(s) already exist")
                                    except asyncio.TimeoutError:
                                        logger.debug(f"⏱️ {sym}: Open orders check timed out, assuming safe")
                                    except Exception as e:
                                        logger.debug(f"⚠️ {sym}: Failed to check open orders: {e}")
                    except Exception as e:
                        logger.debug(f"Failed to check open orders: {e}")
                    
                    reserved_symbols = []
                    async with TASKS_LOCK:
                        total_active_count = len(open_symbols_db | set(ACTIVE_TASKS.keys()))
                        actual_slots_available = max(0, max_trades - total_active_count)
                        
                        for opp in trades_to_launch:
                            if len(reserved_symbols) >= actual_slots_available:
                                break
                            
                            sym = opp.get('symbol')
                            if sym and sym not in ACTIVE_TASKS and sym not in symbols_with_open_orders:
                                ACTIVE_TASKS[sym] = "RESERVED"
                                reserved_symbols.append(sym)
                    
                    if not reserved_symbols:
                        await asyncio.sleep(REFRESH_DELAY)
                        continue
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Double-check shutdown flag right before launching
                    # ═══════════════════════════════════════════════════════════════
                    if SHUTDOWN_FLAG:
                        logger.info("🚫 Shutdown detected - cancelling reserved trades")
                        async with TASKS_LOCK:
                            for sym in reserved_symbols:
                                ACTIVE_TASKS.pop(sym, None)
                        await asyncio.sleep(REFRESH_DELAY)
                        continue
                    
                    logger.info(f"🚀 Launching {len(reserved_symbols)} trades")
                    
                    # Balance for batch
                    avg_trade_size = getattr(config, 'DESIRED_NOTIONAL_USD', 12)
                    leverage = getattr(config, 'LEVERAGE_MULTIPLIER', 1.0)
                    locked_per_exchange = current_open_count * (avg_trade_size / leverage) * 1.2
                    
                    async with IN_FLIGHT_LOCK:
                        available_x10 = bal_x10 - IN_FLIGHT_MARGIN.get('X10', 0) - locked_per_exchange
                        available_lit = bal_lit - IN_FLIGHT_MARGIN.get('Lighter', 0) - locked_per_exchange
                    
                    available_x10 = max(0, available_x10 - 3.0)
                    available_lit = max(0, available_lit - 3.0)
                    
                    # Launch trades
                    launched_count = 0
                    for opp in trades_to_launch:
                        symbol = opp.get('symbol')
                        if not symbol or symbol not in reserved_symbols:
                            continue
                        
                        is_farm = opp.get('is_farm_trade', False)
                        base_default = getattr(config, 'FARM_NOTIONAL_USD', 12) if is_farm else getattr(config, 'DESIRED_NOTIONAL_USD', 16)
                        
                        trade_size = None
                        for key in ('trade_size', 'notional_usd', 'trade_notional_usd'):
                            if key in opp and isinstance(opp.get(key), (int, float)):
                                trade_size = float(opp[key])
                                break
                        
                        if trade_size is None:
                            trade_size = float(base_default)
                        
                        required_margin = (trade_size / leverage) * 1.05
                        
                        if available_x10 < required_margin or available_lit < required_margin:
                            async with TASKS_LOCK:
                                if ACTIVE_TASKS.get(symbol) == "RESERVED":
                                    del ACTIVE_TASKS[symbol]
                            continue
                        
                        available_x10 -= required_margin
                        available_lit -= required_margin
                        
                        logger.info(f"🚀 Launching {symbol} (APY={opp.get('apy', 0):.1f}%)")
                        
                        # Create handler with proper closure
                        def make_handler(sym, opportunity):
                            async def _handle_with_lock():
                                async with get_symbol_lock(sym):
                                    try:
                                        await execute_trade_parallel(opportunity, lighter, x10, parallel_exec)
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception as e:
                                        logger.error(f"❌ Task {sym} failed: {e}")
                                        FAILED_COINS[sym] = time.time()
                                    finally:
                                        async with TASKS_LOCK:
                                            ACTIVE_TASKS.pop(sym, None)
                            return _handle_with_lock
                        
                        if SHUTDOWN_FLAG:
                            async with TASKS_LOCK:
                                if ACTIVE_TASKS.get(symbol) == "RESERVED":
                                    del ACTIVE_TASKS[symbol]
                            break
                        
                        handler = make_handler(symbol, opp)
                        task = asyncio.create_task(handler())
                        
                        async with TASKS_LOCK:
                            if SHUTDOWN_FLAG:
                                task.cancel()
                                ACTIVE_TASKS.pop(symbol, None)
                                break
                            ACTIVE_TASKS[symbol] = task
                        
                        LAST_ARBITRAGE_LAUNCH = time.time()
                        launched_count += 1
                        await asyncio.sleep(0.5)
                    
                    # Cleanup reservations
                    async with TASKS_LOCK:
                        for sym in list(ACTIVE_TASKS.keys()):
                            if ACTIVE_TASKS.get(sym) == "RESERVED":
                                del ACTIVE_TASKS[sym]
                    
                    if launched_count > 0:
                        logger.info(f"✅ Launched {launched_count} trades this cycle")
            
            if SHUTDOWN_FLAG:
                logger.info("Logic loop: SHUTDOWN_FLAG detected, exiting")
                break
            await asyncio.sleep(REFRESH_DELAY)
            
        except asyncio.CancelledError:
            logger.info("Logic loop cancelled")
            break
        except Exception as e:
            logger.error(f"Logic loop error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)

