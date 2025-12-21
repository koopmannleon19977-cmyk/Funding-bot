# src/core/trading.py
"""
Trade execution logic.

This module handles:
- Opening trades (execute_trade_parallel)
- Closing trades (close_trade)
- Task management (launch_trade_task)
- Position size helpers
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import config
from src.utils import safe_float
from src.volatility_monitor import get_volatility_monitor
from src.fee_manager import get_fee_manager
from src.telegram_bot import get_telegram_bot

# B5: JSON Logger for structured logging
try:
    from src.utils.json_logger import get_json_logger, LogCategory
    json_logger = get_json_logger()
except ImportError:
    json_logger = None

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared with main.py)
# ============================================================
FAILED_COINS = {}
ACTIVE_TASKS = {}
SHUTDOWN_FLAG = False
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()
RECENTLY_OPENED_TRADES = {}
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 120.0  # FIX (2025-12-19): Extended to 120s to match zombie check & allow for Exchange API latency (Lighter takes 3-5s to show new positions)


# ============================================================
# IMPORTS FROM CORE (lazy to avoid circular)
# ============================================================
def _get_state_functions():
    """Lazy import to avoid circular dependencies"""
    from src.core.state import (
        get_open_trades, 
        add_trade_to_state, 
        close_trade_in_state,
        get_execution_lock,
        check_total_exposure
    )
    return get_open_trades, add_trade_to_state, close_trade_in_state, get_execution_lock, check_total_exposure


# ============================================================
# HELPER FUNCTIONS
# ============================================================
async def get_actual_position_size(adapter, symbol: str) -> Optional[float]:
    """Get actual position size in coins from exchange"""
    try:
        positions = await adapter.fetch_open_positions()
        for p in (positions or []):
            if p.get('symbol') == symbol:
                return safe_float(p.get('size', 0))
        return None
    except Exception as e:
        logger.error(f"Failed to get position size for {symbol}: {e}")
        return None


async def safe_close_x10_position(x10, symbol, side, notional) -> Optional[str]:
    """Close X10 position using ACTUAL position size. Returns order_id if successful."""
    try:
        # Wait for settlement
        await asyncio.sleep(3.0)
        
        # Get ACTUAL position
        actual_size = await get_actual_position_size(x10, symbol)
        
        if actual_size is None or abs(actual_size) < 1e-8:
            logger.info(f"✅ No X10 position to close for {symbol}")
            return None
        
        actual_size_abs = abs(actual_size)
        
        # Determine side from position
        if actual_size > 0:
            original_side = "BUY"  # LONG
        else:
            original_side = "SELL"  # SHORT
        
        logger.info(f"🔻 Closing X10 {symbol}: size={actual_size_abs:.6f} coins, side={original_side}")
        
        # Close with ACTUAL coin size
        price = safe_float(x10.fetch_mark_price(symbol))
        if price <= 0:
            logger.error(f"No price for {symbol}")
            return None
        
        notional_actual = actual_size_abs * price
        
        success, order_id = await x10.close_live_position(symbol, original_side, notional_actual)
        
        if success:
            logger.info(f"✅ X10 {symbol} closed ({actual_size_abs:.6f} coins)")
            return order_id
        else:
            logger.error(f"❌ X10 close failed for {symbol}")
            return None
    
    except Exception as e:
        logger.error(f"X10 close exception: {e}")
        return None


# ============================================================
# TRADE EXECUTION
# ============================================================
async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    """Wrapper for trade execution with cleanup"""
    symbol = opp['symbol']
    try:
        await execute_trade_parallel(opp, lighter, x10, parallel_exec)
    except asyncio.CancelledError:
        logger.debug(f"Task {symbol} cancelled")
        raise
    except Exception as e:
        logger.error(f"❌ Task {symbol} failed: {e}", exc_info=True)
        FAILED_COINS[symbol] = time.time()
    finally:
        if symbol in ACTIVE_TASKS:
            try:
                del ACTIVE_TASKS[symbol]
                logger.debug(f"🧹 Cleaned task: {symbol}")
            except KeyError:
                pass


async def launch_trade_task(opp: Dict, lighter, x10, parallel_exec):
    """Launch trade execution in background task."""
    symbol = opp['symbol']
    
    # Check cooldown
    if symbol in FAILED_COINS:
        cooldown = time.time() - FAILED_COINS[symbol]
        if cooldown < 180:  # 3min cooldown
            return
    
    if symbol in ACTIVE_TASKS:
        return

    async def _task_wrapper():
        try:
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)
        except asyncio.CancelledError:
            logger.debug(f"Task {symbol} cancelled")
            raise
        except Exception as e:
            logger.error(f"❌ Task {symbol} failed: {e}", exc_info=True)
            FAILED_COINS[symbol] = time.time()
        finally:
            if symbol in ACTIVE_TASKS:
                try:
                    del ACTIVE_TASKS[symbol]
                except KeyError:
                    pass

    task = asyncio.create_task(_task_wrapper())
    ACTIVE_TASKS[symbol] = task


async def execute_trade_parallel(opp: Dict, lighter, x10, parallel_exec) -> bool:
    """Execute a trade on both exchanges in parallel"""
    global SHUTDOWN_FLAG, IN_FLIGHT_MARGIN
    
    if SHUTDOWN_FLAG:
        return False

    # Lazy imports
    get_open_trades, add_trade_to_state, _, get_execution_lock, check_total_exposure = _get_state_functions()

    symbol = opp['symbol']
    reserved_amount = 0.0
    lock = await get_execution_lock(symbol)
    if lock.locked():
        return False

    async with lock:
        # ═══════════════════════════════════════════════════════════════
        # FIX: Check shutdown flag again after acquiring lock
        # ═══════════════════════════════════════════════════════════════
        if SHUTDOWN_FLAG:
            logger.debug(f"🚫 {symbol}: Shutdown detected - aborting trade execution")
            return False
        
        # Check if position already exists
        try:
            x10_positions = await x10.fetch_open_positions()
            lighter_positions = await lighter.fetch_open_positions()

            # ═══════════════════════════════════════════════════════════════
            # HARD LIMIT: Do not open new trades if max open positions reached
            # This is intentionally placed here (not only in process_symbol)
            # because the monitoring/farm loop can call execute_trade_parallel
            # directly and would otherwise bypass MAX_OPEN_TRADES.
            # We count by SYMBOL (positions), not by order legs.
            # ═══════════════════════════════════════════════════════════════
            max_positions = int(getattr(config, 'MAX_OPEN_POSITIONS', getattr(config, 'MAX_OPEN_TRADES', 40)))
            if max_positions > 0:
                open_symbols_real = {
                    p.get('symbol') for p in (x10_positions or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                } | {
                    p.get('symbol') for p in (lighter_positions or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                }

                # Also include DB open trades and in-flight executions/tasks
                existing = await get_open_trades()
                open_symbols_db = {t.get('symbol') for t in (existing or []) if t.get('symbol')}

                active_symbols = set()
                try:
                    # ACTIVE_TASKS can store asyncio.Tasks or placeholder strings like "RESERVED"
                    active_symbols |= set(ACTIVE_TASKS.keys())
                except Exception:
                    pass
                try:
                    if hasattr(parallel_exec, 'active_executions') and isinstance(parallel_exec.active_executions, dict):
                        active_symbols |= set(parallel_exec.active_executions.keys())
                except Exception:
                    pass

                total_active_symbols = {s for s in (open_symbols_real | open_symbols_db | active_symbols) if s}
                if len(total_active_symbols) >= max_positions:
                    logger.info(
                        f"⛔ {opp.get('symbol')}: Max open positions reached "
                        f"({len(total_active_symbols)}/{max_positions}) - skip new entry"
                    )
                    return False
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Check both position lists more carefully - also check size
            # ═══════════════════════════════════════════════════════════════
            for pos in (x10_positions or []):
                if pos.get('symbol') == symbol and abs(safe_float(pos.get('size', 0))) > 1e-8:
                    logger.warning(f"⚠️ {symbol} already open on X10 (size={pos.get('size')}) - SKIP")
                    return False
            
            for pos in (lighter_positions or []):
                if pos.get('symbol') == symbol and abs(safe_float(pos.get('size', 0))) > 1e-8:
                    logger.warning(f"⚠️ {symbol} already open on Lighter (size={pos.get('size')}) - SKIP")
                    return False
            
        except Exception as e:
            logger.error(f"Failed to check positions for {symbol}: {e}")
            return False
        
        # Check state
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing):
            return False

        # Volatility check
        vol_monitor = get_volatility_monitor()
        if not vol_monitor.can_enter_trade(symbol):
            logger.info(f"⛔ {symbol}: Volatility too high (Regime: {vol_monitor.current_regimes.get(symbol, 'UNKNOWN')}) - skip entry")
            return False

        # Exposure check
        trade_size = opp.get('size_usd', getattr(config, 'DESIRED_NOTIONAL_USD', 500))
        can_trade, exposure_pct, max_pct = await check_total_exposure(x10, lighter, trade_size)
        if not can_trade:
            logger.info(f"⛔ {symbol}: Exposure limit ({exposure_pct:.1%} > {max_pct:.0%})")
            return False

        # Sizing
        try:
            min_req_x10 = float(x10.min_notional_usd(symbol))
            min_req_lit = float(lighter.min_notional_usd(symbol))
            min_req = max(min_req_x10, min_req_lit)
            max_per_trade = float(getattr(config, 'MAX_NOTIONAL_USD', 18.0))
            
            if min_req > config.MAX_TRADE_SIZE_USD:
                return False

            async with IN_FLIGHT_LOCK:
                raw_x10 = await x10.get_real_available_balance()
                raw_lit = await lighter.get_real_available_balance()
                bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))

            # Position sizing (FIXED - using DESIRED_NOTIONAL_USD)
            available_capital = min(bal_x10_real, bal_lit_real)
            
            desired_size = float(getattr(config, 'DESIRED_NOTIONAL_USD', 80.0))
            final_usd = max(desired_size, min_req)
            
            # Check we can afford it (WITH LEVERAGE)
            leverage = getattr(config, 'LEVERAGE_MULTIPLIER', 1.0)
            required_margin_check = (final_usd / leverage) * 1.05  # 5% buffer
            
            if required_margin_check > available_capital:
                logger.warning(f"🛑 {symbol}: Insufficient capital (need ${required_margin_check:.2f} margin, have ${available_capital:.2f})")
                return False
            
            # Check within limits
            if final_usd > config.MAX_TRADE_SIZE_USD:
                final_usd = config.MAX_TRADE_SIZE_USD
            
            logger.info(f"📏 SIZE {symbol}: ${final_usd:.2f} (Lev {leverage}x, Margin ${required_margin_check:.2f})")

            # Liquidity check
            l_ex = opp.get('leg1_exchange', 'X10')
            l_side = opp.get('leg1_side', 'BUY')
            lit_side_check = l_side if l_ex == 'Lighter' else ("SELL" if l_side == "BUY" else "BUY")
            
            if not await lighter.check_liquidity(symbol, lit_side_check, final_usd, is_maker=True):
                logger.warning(f"🛑 {symbol}: Insufficient Lighter liquidity")
                return False

            # Balance check with leverage
            leverage = getattr(config, 'LEVERAGE_MULTIPLIER', 1.0)
            required_margin = (final_usd / leverage) * 1.05
            
            if bal_x10_real < required_margin or bal_lit_real < required_margin:
                return False

            # Reserve margin
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + required_margin
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + required_margin
                reserved_amount = required_margin

        except Exception as e:
            logger.error(f"Sizing error {symbol}: {e}")
            return False

        # Execute trade
        try:
            leg1_ex = opp.get('leg1_exchange', 'X10')
            leg1_side = opp.get('leg1_side', 'BUY')
            
            x10_side = leg1_side if leg1_ex == 'X10' else ("SELL" if leg1_side == "BUY" else "BUY")
            lit_side = leg1_side if leg1_ex == 'Lighter' else ("SELL" if leg1_side == "BUY" else "BUY")

            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f}")
            
            # Register for protection
            async with RECENTLY_OPENED_LOCK:
                RECENTLY_OPENED_TRADES[symbol] = time.time()

            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: Persist trade BEFORE execution (PENDING)
            # Prevents orphan exposure when hedge fails or bot crashes mid-flight.
            # ═══════════════════════════════════════════════════════════════
            pending_recorded = False
            try:
                entry_price_x10_est = safe_float(
                    opp.get("entry_price_x10_est") or x10.fetch_mark_price(symbol) or 0.0
                )
                entry_price_lighter_est = safe_float(
                    opp.get("entry_price_lighter_est") or lighter.fetch_mark_price(symbol) or 0.0
                )
                await add_trade_to_state(
                    {
                        "symbol": symbol,
                        "entry_time": datetime.now(timezone.utc),
                        "notional_usd": final_usd,
                        "status": "pending",
                        "leg1_exchange": leg1_ex,
                        "entry_price_x10": entry_price_x10_est,
                        "entry_price_lighter": entry_price_lighter_est,
                        "is_farm_trade": opp.get("is_farm_trade", False),
                        "account_label": "Main/Main",
                        "side_x10": x10_side,
                        "side_lighter": lit_side,
                    }
                )
                pending_recorded = True
                logger.info(f"📝 Recorded PENDING trade {symbol} in state/DB")
            except Exception as e:
                logger.warning(f"⚠️ Failed to record PENDING trade for {symbol}: {e}")

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol=symbol,
                side_x10=x10_side,
                side_lighter=lit_side,
                size_x10=final_usd,
                size_lighter=final_usd
            )

            if success:
                # ═══════════════════════════════════════════════════════════════
                # TRADE RECORDING - Save to database with comprehensive logging
                # ═══════════════════════════════════════════════════════════════
                logger.info(f"📝 Recording trade {symbol} in database...")
                
                # FIX (2025-12-19): Fetch ACTUAL fill prices from POSITION API (not order API!)
                # Order API returns limit price, Position API returns actual avg fill price
                entry_price_x10 = 0.0
                entry_price_lighter = 0.0
                try:
                    # Brief delay to let positions update on exchanges
                    await asyncio.sleep(0.5)
                    
                    # Fetch positions from both exchanges
                    px_pos, lit_pos = await asyncio.gather(
                        x10.fetch_open_positions(),
                        lighter.fetch_open_positions(),
                        return_exceptions=True
                    )
                    px_pos = [] if isinstance(px_pos, Exception) else (px_pos or [])
                    lit_pos = [] if isinstance(lit_pos, Exception) else (lit_pos or [])
                    
                    # X10: Get entry_price from position (NOT order!)
                    x10_p = next((p for p in px_pos if p.get("symbol") == symbol), None)
                    if x10_p:
                        p = safe_float(x10_p.get("entry_price") or x10_p.get("avg_entry_price") or 0.0)
                        if p > 0:
                            entry_price_x10 = p
                            logger.info(f"✅ X10 {symbol}: Got entry_price ${p:.6f} from position")
                    
                    # Lighter: Get avg_entry_price from position
                    l_p = next((p for p in lit_pos if p.get("symbol") == symbol), None)
                    if l_p:
                        p = safe_float(l_p.get("avg_entry_price") or l_p.get("entry_price") or 0.0)
                        if p > 0:
                            entry_price_lighter = p
                            logger.info(f"✅ Lighter {symbol}: Got avg_entry_price ${p:.6f} from position")
                    
                    # Fallback to mark price if position not found yet
                    if entry_price_x10 <= 0:
                        entry_price_x10 = x10.fetch_mark_price(symbol) or 0.0
                        logger.warning(f"⚠️ X10 fill price not found in position, using mark price ${entry_price_x10:.6f}")
                    if entry_price_lighter <= 0:
                        entry_price_lighter = lighter.fetch_mark_price(symbol) or 0.0
                        logger.warning(f"⚠️ Lighter fill price not found in position, using mark price ${entry_price_lighter:.6f}")
                except Exception as e:
                    logger.warning(f"⚠️ Error fetching fill prices from positions: {e}")
                    entry_price_x10 = x10.fetch_mark_price(symbol) or 0.0
                    entry_price_lighter = lighter.fetch_mark_price(symbol) or 0.0
                
                apy_value = opp.get('apy', 0.0)
                
                trade_data = {
                    'symbol': symbol,
                    'entry_time': datetime.now(timezone.utc),
                    'notional_usd': final_usd,
                    'status': 'open',
                    'leg1_exchange': leg1_ex,
                    'entry_price_x10': entry_price_x10,
                    'entry_price_lighter': entry_price_lighter,
                    'is_farm_trade': opp.get('is_farm_trade', False),
                    'account_label': "Main/Main",
                    'x10_order_id': str(x10_id) if x10_id else None,
                    'lighter_order_id': str(lit_id) if lit_id else None,
                    'side_x10': x10_side,
                    'side_lighter': lit_side,
                }
                
                try:
                    # Prefer updating the existing PENDING record; fall back to insert if needed.
                    from src.state_manager import get_state_manager
                    sm = await get_state_manager()
                    updated = await sm.update_trade(
                        symbol,
                        {
                            "status": "open",
                            "entry_price_x10": entry_price_x10,
                            "entry_price_lighter": entry_price_lighter,
                            "x10_order_id": str(x10_id) if x10_id else None,
                            "lighter_order_id": str(lit_id) if lit_id else None,
                            "side_x10": x10_side,
                            "side_lighter": lit_side,
                            "size_usd": final_usd,
                            "is_farm_trade": opp.get("is_farm_trade", False),
                            "account_label": "Main/Main",
                        },
                    )
                    if not updated:
                        await add_trade_to_state(trade_data)
                    logger.info(f"✅ Trade {symbol} recorded successfully")
                    logger.info(f"   X10 Order ID: {x10_id}")
                    logger.info(f"   Lighter Order ID: {lit_id[:30] if lit_id and len(lit_id) > 30 else lit_id}...")
                    logger.info(f"   Size: ${final_usd:.2f}")
                    logger.info(f"   Entry X10: ${entry_price_x10:.6f}")
                    logger.info(f"   Entry Lighter: ${entry_price_lighter:.6f}")
                    logger.info(f"   APY: {apy_value:.2f}%")

                    # B5: JSON structured log for trade entry
                    if json_logger:
                        json_logger.trade_entry(
                            symbol=symbol,
                            side=x10_side,
                            size=final_usd,
                            price=entry_price_x10,
                            exchange="BOTH",
                            x10_order_id=str(x10_id) if x10_id else None,
                            lighter_order_id=str(lit_id)[:30] if lit_id else None,
                            entry_price_x10=entry_price_x10,
                            entry_price_lighter=entry_price_lighter,
                            apy=apy_value,
                            leg1_exchange=leg1_ex
                        )

                    # ═══════════════════════════════════════════════════════════════
                    # PNL DATA ENRICHMENT (best-effort):
                    # Capture actual entry quantities/prices + actual fee rates for both legs.
                    # This makes realized PnL calculation possible later without relying on
                    # spread-only heuristics.
                    # ═══════════════════════════════════════════════════════════════
                    try:
                        from src.state_manager import get_state_manager
                        sm = await get_state_manager()

                        # Fetch fee rates for entry orders (if possible)
                        fee_tasks = []
                        fee_tasks.append(x10.get_order_fee(str(x10_id)) if x10_id else asyncio.sleep(0, result=None))
                        fee_tasks.append(lighter.get_order_fee(str(lit_id)) if lit_id else asyncio.sleep(0, result=None))
                        fee_x10_rate, fee_lit_rate = await asyncio.gather(*fee_tasks, return_exceptions=True)

                        if isinstance(fee_x10_rate, Exception):
                            fee_x10_rate = None
                        if isinstance(fee_lit_rate, Exception):
                            fee_lit_rate = None

                        # Fetch actual entry snapshot from open positions (retry a bit: exchanges can lag)
                        entry_updates = {}
                        for _ in range(6):
                            try:
                                px_pos, lit_pos = await asyncio.gather(
                                    x10.fetch_open_positions(),
                                    lighter.fetch_open_positions(),
                                    return_exceptions=True
                                )
                                px_pos = [] if isinstance(px_pos, Exception) else (px_pos or [])
                                lit_pos = [] if isinstance(lit_pos, Exception) else (lit_pos or [])

                                x10_p = next((p for p in px_pos if p.get("symbol") == symbol), None)
                                l_p = next((p for p in lit_pos if p.get("symbol") == symbol), None)

                                if x10_p:
                                    x_size = safe_float(x10_p.get("size", 0.0))
                                    x_entry = safe_float(x10_p.get("entry_price", 0.0))
                                    if abs(x_size) > 1e-10:
                                        entry_updates["entry_qty_x10"] = abs(x_size)
                                    if x_entry > 0:
                                        entry_updates["entry_price_x10"] = x_entry

                                if l_p:
                                    l_size = safe_float(l_p.get("size", 0.0))
                                    l_entry = safe_float(l_p.get("avg_entry_price", 0.0))
                                    if abs(l_size) > 1e-10:
                                        entry_updates["entry_qty_lighter"] = abs(l_size)
                                    if l_entry > 0:
                                        entry_updates["entry_price_lighter"] = l_entry

                                # stop early if we have both legs
                                if ("entry_qty_x10" in entry_updates and "entry_qty_lighter" in entry_updates):
                                    break
                            except Exception:
                                pass
                            await asyncio.sleep(0.4)

                        if fee_x10_rate is not None:
                            entry_updates["entry_fee_x10"] = safe_float(fee_x10_rate, 0.0)
                        if fee_lit_rate is not None:
                            entry_updates["entry_fee_lighter"] = safe_float(fee_lit_rate, 0.0)

                        if entry_updates:
                            await sm.update_trade(symbol, entry_updates)
                            logger.debug(f"📌 {symbol}: Stored entry snapshot for PnL ({entry_updates})")
                    except Exception as enrich_err:
                        logger.debug(f"{symbol}: Entry PnL enrichment skipped: {enrich_err}")
                except Exception as db_error:
                    # CRITICAL: Trade is open on exchanges but not in DB!
                    logger.error(f"❌ Failed to record trade {symbol}: {db_error}")
                    logger.critical(f"═══════════════════════════════════════════════════════════")
                    logger.critical(f"🚨 ORPHAN TRADE DETECTED: {symbol}")
                    logger.critical(f"🚨 Trade is OPEN on exchanges but NOT recorded in database!")
                    logger.critical(f"   X10 Order ID: {x10_id}")
                    logger.critical(f"   Lighter Order ID: {lit_id}")
                    logger.critical(f"   Size: ${final_usd:.2f}")
                    logger.critical(f"═══════════════════════════════════════════════════════════")
                    
                    # Try to send Telegram alert
                    try:
                        telegram = get_telegram_bot()
                        if telegram and telegram.enabled:
                            await telegram.send_error(
                                f"🚨 ORPHAN TRADE: {symbol}\n"
                                f"Trade is OPEN on exchanges but NOT in database!\n"
                                f"X10: {x10_id}\n"
                                f"Lighter: {lit_id}\n"
                                f"Size: ${final_usd:.2f}"
                            )
                    except Exception:
                        pass
                
                logger.info(f"✅ OPENED {symbol}: ${final_usd:.2f}")
                return True
            else:
                logger.warning(f"❌ Trade execution failed for {symbol}")
                # Mark pending/open record as rollback so it no longer counts as open-risk.
                if pending_recorded:
                    try:
                        from src.state_manager import get_state_manager
                        sm = await get_state_manager()
                        await sm.update_trade(
                            symbol,
                            {
                                "status": "rollback",
                                "closed_at": int(time.time() * 1000),
                                "x10_order_id": str(x10_id) if x10_id else None,
                                "lighter_order_id": str(lit_id) if lit_id else None,
                            },
                        )
                        logger.warning(f"📝 Marked {symbol} as ROLLBACK in state/DB")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to mark rollback for {symbol}: {e}")
                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.error(f"Execution error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False
        finally:
            if reserved_amount > 0:
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] = max(0.0, IN_FLIGHT_MARGIN.get('X10', 0.0) - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(0.0, IN_FLIGHT_MARGIN.get('Lighter', 0.0) - reserved_amount)


# ============================================================
# CLOSE TRADE
# ============================================================
async def close_trade(trade: Dict, lighter, x10) -> bool:
    """
    Close trade on both exchanges with comprehensive logging.
    """
    _, _, close_trade_in_state, _, _ = _get_state_functions()
    
    symbol = trade['symbol']
    # FIX: Accept both 'notional_usd' and 'size_usd' field names
    notional_usd = safe_float(trade.get('notional_usd') or trade.get('size_usd') or 0)
    
    logger.info(f"═══════════════════════════════════════════════════════════")
    logger.info(f"🔻 CLOSING TRADE: {symbol}")
    logger.info(f"   Notional: ${notional_usd:.2f}")
    logger.info(f"═══════════════════════════════════════════════════════════")

    # Step 1: Close Lighter
    logger.info(f"📤 [LEG 1] Closing Lighter position for {symbol}...")
    lighter_success = False
    lighter_code_error = False
    lighter_exit_order_id = None

    try:
        positions = await lighter.fetch_open_positions()
        pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
        size = safe_float(pos.get('size', 0)) if pos else 0.0

        if pos and abs(size) > 1e-10:
            side = "SELL" if size > 0 else "BUY"
            position_side = "LONG" if size > 0 else "SHORT"
            px = safe_float(lighter.fetch_mark_price(symbol))
            if px > 0:
                usd_size = abs(size) * px
                logger.info(f"   Lighter {symbol}: {position_side} {abs(size):.6f} coins @ ${px:.4f} = ${usd_size:.2f}")
                
                try:
                    result = await lighter.close_live_position(symbol, side, usd_size)
                    if isinstance(result, tuple):
                        lighter_success = result[0]
                        lighter_exit_order_id = result[1] if len(result) > 1 else None
                    else:
                        lighter_success = bool(result)
                    
                    if lighter_success:
                        logger.info(f"✅ [LEG 1] Lighter {symbol} closed: {lighter_exit_order_id}")
                        # Expose exit order id to caller for PnL reconciliation
                        try:
                            trade["lighter_exit_order_id"] = lighter_exit_order_id
                        except Exception:
                            pass
                    else:
                        logger.error(f"❌ [LEG 1] Lighter {symbol} close returned False")
                except TypeError as type_err:
                    logger.critical(f"🚨 TypeError in Lighter close for {symbol}: {type_err}")
                    lighter_code_error = True
                except Exception as close_err:
                    logger.error(f"❌ [LEG 1] Lighter close failed for {symbol}: {close_err}")
            else:
                logger.error(f"❌ [LEG 1] Lighter close {symbol}: Invalid price (px={px})")
        else:
            logger.info(f"✅ [LEG 1] Lighter {symbol}: No position to close (size={size})")
            lighter_success = True

    except TypeError as e:
        logger.critical(f"🚨 TypeError in Lighter close flow for {symbol}: {e}")
        lighter_code_error = True
    except Exception as e:
        logger.error(f"❌ [LEG 1] Lighter close failed for {symbol}: {e}")

    # Emergency recovery for code errors
    if lighter_code_error:
        logger.warning(f"⚠️ CODE ERROR! Force-closing X10 for {symbol}...")
        x10_emergency_ok = False
        try:
            await safe_close_x10_position(x10, symbol, "AUTO", 0)
            x10_emergency_ok = True
        except Exception as x10_err:
            logger.error(f"❌ EMERGENCY X10 close FAILED: {x10_err}")

        try:
            await close_trade_in_state(symbol, pnl=0, funding=0)
        except Exception:
            pass

        return x10_emergency_ok

    # Normal path: Close X10
    if not lighter_success:
        logger.error(f"❌ Lighter failed for {symbol}, keeping X10 as hedge")
        return False

    logger.info(f"✅ [LEG 1] Lighter {symbol} closed, closing X10...")

    x10_success = False
    x10_exit_order_id = None
    try:
        x10_exit_order_id = await safe_close_x10_position(x10, symbol, "AUTO", 0)
        x10_success = x10_exit_order_id is not None
        if x10_success:
            logger.info(f"✅ [LEG 2] X10 {symbol} closed: {x10_exit_order_id}")
            # Expose exit order id to caller for PnL reconciliation
            try:
                trade["x10_exit_order_id"] = x10_exit_order_id
            except Exception:
                pass
        else:
            logger.error(f"❌ [LEG 2] X10 {symbol} close returned None")
    except Exception as e:
        logger.error(f"❌ [LEG 2] X10 close failed for {symbol}: {e}")

    if lighter_success and x10_success:
        logger.info(f"═══════════════════════════════════════════════════════════")
        logger.info(f"✅ TRADE CLOSED: {symbol}")
        logger.info(f"   Lighter Exit: {lighter_exit_order_id}")
        logger.info(f"   X10 Exit: {x10_exit_order_id}")
        logger.info(f"═══════════════════════════════════════════════════════════")
        return True
    else:
        logger.warning(f"═══════════════════════════════════════════════════════════")
        logger.warning(f"⚠️ TRADE PARTIALLY CLOSED: {symbol}")
        logger.warning(f"   Lighter: {'✅' if lighter_success else '❌'} (Exit: {lighter_exit_order_id})")
        logger.warning(f"   X10: {'✅' if x10_success else '❌'} (Exit: {x10_exit_order_id})")
        logger.warning(f"═══════════════════════════════════════════════════════════")
        return False


# ============================================================
# SINGLE SYMBOL PROCESSING
# ============================================================
async def process_symbol(symbol: str, lighter, x10, parallel_exec, lock: asyncio.Lock):
    """
    Process a single symbol for trading opportunity.
    
    This function:
    1. Checks blacklist and cooldowns
    2. Validates funding rates and prices
    3. Checks APY threshold
    4. Validates spread and OI
    5. Executes trade if all conditions are met
    """
    async with lock:
        try:
            # Quick guards: blacklist, recent failures
            if symbol in getattr(config, 'BLACKLIST_SYMBOLS', []):
                logger.debug(f"{symbol}: in BLACKLIST, skip")
                return

            if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 300):
                logger.debug(f"{symbol}: in FAILED_COINS cooldown, skip")
                return

            # Check if already have open position
            get_open_trades, _, _, _, _ = _get_state_functions()
            open_trades = await get_open_trades()
            open_symbols = {t['symbol'] for t in open_trades}
            if symbol in open_symbols:
                logger.debug(f"{symbol}: already have open position, skip")
                return

            # Check max open trades limit
            max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
            if len(open_trades) >= max_trades:
                logger.debug(f"{symbol}: MAX_OPEN_TRADES ({max_trades}) reached, skip")
                return

            # Prices and funding rates
            px = x10.fetch_mark_price(symbol)
            pl = lighter.fetch_mark_price(symbol)
            rl = lighter.fetch_funding_rate(symbol)
            rx = x10.fetch_funding_rate(symbol)

            if rl is None or rx is None:
                logger.debug(f"{symbol}: missing funding rates (rl={rl}, rx={rx})")
                return

            if px is None or pl is None:
                logger.debug(f"{symbol}: missing prices (px={px}, pl={pl})")
                return

            # Update volatility monitor
            try:
                get_volatility_monitor().update_price(symbol, safe_float(px))
            except Exception:
                pass

            # rl und rx sind stündliche Funding Rates (laut Dokumentation beider Exchanges)
            # APY = hourly_rate * 24 hours/day * 365 days/year
            net = rl - rx
            apy = abs(net) * 24 * 365

            # Threshold check
            from src.threshold_manager import get_threshold_manager
            threshold_manager = get_threshold_manager()
            req_apy = threshold_manager.get_threshold(symbol, is_maker=True)
            if apy < req_apy:
                logger.debug(f"{symbol}: APY {apy*100:.2f}% < required {req_apy*100:.2f}%")
                return

            # Price validity & spread
            try:
                px_f = safe_float(px)
                pl_f = safe_float(pl)
                if px_f <= 0 or pl_f <= 0:
                    logger.debug(f"{symbol}: invalid prices px={px_f}, pl={pl_f}")
                    return
                spread = abs(px_f - pl_f) / px_f
                max_spread = getattr(config, 'MAX_SPREAD_FILTER_PERCENT', 0.005)
                if spread > max_spread:
                    logger.debug(f"{symbol}: spread {spread*100:.2f}% > max {max_spread*100:.2f}%")
                    return
            except Exception as e:
                logger.debug(f"{symbol}: price parsing error: {e}")
                return

            # Open Interest Check
            oi_x10 = 0.0
            oi_lighter = 0.0
            try:
                oi_x10 = await x10.fetch_open_interest(symbol)
            except Exception:
                pass
            try:
                oi_lighter = await lighter.fetch_open_interest(symbol)
            except Exception:
                pass
            
            total_oi = oi_x10 + oi_lighter
            
            min_oi_usd = getattr(config, 'MIN_OPEN_INTEREST_USD', 50000)
            if total_oi > 0 and total_oi < min_oi_usd:
                logger.debug(f"{symbol}: OI ${total_oi:.0f} < min ${min_oi_usd:.0f}")
                return

            # Build opportunity payload
            opp = {
                'symbol': symbol,
                'apy': apy * 100,
                'net_funding_hourly': net,
                'leg1_exchange': 'Lighter' if rl > rx else 'X10',
                'leg1_side': 'SELL' if rl > rx else 'BUY',
                'is_farm_trade': False,
                'spread_pct': spread,
                'open_interest': total_oi,
                'prediction_confidence': 0.5
            }

            logger.info(f"✅ {symbol}: EXECUTING trade APY={opp['apy']:.2f}% spread={spread*100:.2f}% OI=${total_oi:.0f}")

            # Execute trade
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}", exc_info=True)
            telegram = get_telegram_bot()
            if telegram and telegram.enabled:
                asyncio.create_task(telegram.send_error(f"Symbol Error {symbol}: {e}"))
        finally:
            # Clean up task tracking
            ACTIVE_TASKS.pop(symbol, None)

