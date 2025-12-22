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
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal

import config
from src.utils import safe_float, safe_decimal, quantize_usd
from src.application.fee_manager import get_fee_manager
from src.core.events import CriticalError, NotificationEvent
from src.core.interfaces import Position

# B5: JSON Logger for structured logging
try:
    from src.utils.json_logger import get_json_logger, LogCategory
    json_logger = get_json_logger()
except ImportError:
    json_logger = None

logger = logging.getLogger(__name__)

# Event handler for external notifications
EVENT_HANDLER = None

def set_event_handler(handler):
    """Set the event handler for core notifications."""
    global EVENT_HANDLER
    EVENT_HANDLER = handler

async def publish_event(event):
    """Publish an event to the handler."""
    if EVENT_HANDLER:
        try:
            await EVENT_HANDLER(event)
        except Exception as e:
            logger.error(f"Failed to publish event {type(event).__name__}: {e}")

# ============================================================
# GLOBALS (shared with main.py)
# ============================================================
FAILED_COINS = {}
ACTIVE_TASKS = {}
SHUTDOWN_FLAG = False
IN_FLIGHT_MARGIN = {'X10': Decimal('0'), 'Lighter': Decimal('0')}
IN_FLIGHT_LOCK = asyncio.Lock()
RECENTLY_OPENED_TRADES = {}
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 120.0 


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
async def get_actual_position_size(adapter, symbol: str) -> Optional[Decimal]:
    """Get actual position size in coins from exchange using Decimal."""
    try:
        positions: List[Position] = await adapter.fetch_open_positions()
        for p in (positions or []):
            if p.symbol == symbol:
                return p.size
        return None
    except Exception as e:
        logger.error(f"Failed to get position size for {symbol}: {e}")
        return None


async def safe_close_x10_position(x10, symbol: str, side: str, notional: Decimal) -> Optional[str]:
    """Close X10 position using ACTUAL position size. Returns order_id if successful."""
    try:
        # Wait for settlement
        await asyncio.sleep(3.0)
        
        # Get ACTUAL position (Decimal)
        actual_size = await get_actual_position_size(x10, symbol)
        
        if actual_size is None or abs(actual_size) < Decimal('1e-8'):
            logger.info(f"✅ No X10 position to close for {symbol}")
            return None
        
        actual_size_abs = abs(actual_size)
        
        # Determine side from position
        original_side = "BUY" if actual_size > 0 else "SELL"
        
        logger.info(f"🔻 Closing X10 {symbol}: size={actual_size_abs:.6f} coins, side={original_side}")
        
        # Close with ACTUAL coin size
        price = await x10.fetch_mark_price(symbol)
        if price <= 0:
            logger.error(f"No price for {symbol}")
            return None
        
        notional_actual = actual_size_abs * price
        
        # Using interface place_order for closing
        close_side = "SELL" if actual_size > 0 else "BUY"
        result = await x10.place_order(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            size=actual_size_abs,
            reduce_only=True
        )
        
        if result.success:
            logger.info(f"✅ X10 {symbol} closed ({actual_size_abs:.6f} coins)")
            return result.order_id
        else:
            logger.error(f"❌ X10 close failed for {symbol}: {result.error_message}")
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
    """Execute a trade on both exchanges in parallel using Decimal"""
    global SHUTDOWN_FLAG, IN_FLIGHT_MARGIN
    
    if SHUTDOWN_FLAG:
        return False

    # Lazy imports
    get_open_trades, add_trade_to_state, _, get_execution_lock, check_total_exposure = _get_state_functions()

    symbol = opp['symbol']
    reserved_amount = Decimal('0')
    lock = await get_execution_lock(symbol)
    if lock.locked():
        return False

    async with lock:
        if SHUTDOWN_FLAG:
            logger.debug(f"🚫 {symbol}: Shutdown detected - aborting trade execution")
            return False
        
        # Check if position already exists
        try:
            x10_positions = await x10.fetch_open_positions()
            lighter_positions = await lighter.fetch_open_positions()

            max_positions = int(getattr(config, 'MAX_OPEN_POSITIONS', getattr(config, 'MAX_OPEN_TRADES', 40)))
            if max_positions > 0:
                open_symbols_real = {
                    p.symbol for p in (x10_positions or [])
                    if abs(p.size) > Decimal('1e-8')
                } | {
                    p.symbol for p in (lighter_positions or [])
                    if abs(p.size) > Decimal('1e-8')
                }

                existing = await get_open_trades()
                open_symbols_db = {t.get('symbol') for t in (existing or []) if t.get('symbol')}

                active_symbols = set()
                try:
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
                        f"⛔ {symbol}: Max open positions reached "
                        f"({len(total_active_symbols)}/{max_positions}) - skip new entry"
                    )
                    return False
            
            for pos in (x10_positions or []):
                if pos.symbol == symbol and abs(pos.size) > Decimal('1e-8'):
                    logger.warning(f"⚠️ {symbol} already open on X10 (size={pos.size}) - SKIP")
                    return False
            
            for pos in (lighter_positions or []):
                if pos.symbol == symbol and abs(pos.size) > Decimal('1e-8'):
                    logger.warning(f"⚠️ {symbol} already open on Lighter (size={pos.size}) - SKIP")
                    return False
            
        except Exception as e:
            logger.error(f"Failed to check positions for {symbol}: {e}")
            return False
        
        # Check state
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing):
            return False


        # Exposure check
        trade_size = safe_decimal(opp.get('size_usd') or getattr(config, 'DESIRED_NOTIONAL_USD', 500))
        can_trade, exposure_pct, max_pct = await check_total_exposure(x10, lighter, trade_size)
        if not can_trade:
            exposure_print = (exposure_pct * Decimal('100')).quantize(Decimal('0.1')) if isinstance(exposure_pct, Decimal) else exposure_pct
            max_print = (max_pct * Decimal('100')).quantize(Decimal('0.1')) if isinstance(max_pct, Decimal) else max_pct
            logger.info(f"⛔ {symbol}: Exposure limit ({exposure_print}% > {max_print}%)")
            return False

        # Sizing
        try:
            min_req_x10 = safe_decimal(await x10.min_notional_usd(symbol))
            min_req_lit = safe_decimal(await lighter.min_notional_usd(symbol))
            min_req = max(min_req_x10, min_req_lit)
            
            if min_req > safe_decimal(config.MAX_TRADE_SIZE_USD):
                return False

            async with IN_FLIGHT_LOCK:
                raw_x10 = await x10.get_available_balance()
                raw_lit = await lighter.get_available_balance()
                bal_x10_real = max(Decimal('0'), raw_x10 - IN_FLIGHT_MARGIN.get('X10', Decimal('0')))
                bal_lit_real = max(Decimal('0'), raw_lit - IN_FLIGHT_MARGIN.get('Lighter', Decimal('0')))

            available_capital = min(bal_x10_real, bal_lit_real)
            desired_size = safe_decimal(getattr(config, 'DESIRED_NOTIONAL_USD', 80.0))
            final_usd = max(desired_size, min_req)

            leverage = safe_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 1.0))
            required_margin_check = (final_usd / leverage) * Decimal('1.05')

            if required_margin_check > available_capital:
                need_str = required_margin_check.quantize(Decimal('0.01'))
                have_str = available_capital.quantize(Decimal('0.01'))
                logger.warning(f"🛑 {symbol}: Insufficient capital (need ${need_str} margin, have ${have_str})")
                return False

            if final_usd > safe_decimal(config.MAX_TRADE_SIZE_USD):
                final_usd = safe_decimal(config.MAX_TRADE_SIZE_USD)

            logger.info(
                f"📏 SIZE {symbol}: ${final_usd.quantize(Decimal('0.01'))} "
                f"(Lev {leverage}x, Margin ${required_margin_check.quantize(Decimal('0.01'))})"
            )

            # Liquidity check
            l_ex = opp.get('leg1_exchange', 'X10')
            l_side = opp.get('leg1_side', 'BUY')
            lit_side_check = l_side if l_ex == 'Lighter' else ("SELL" if l_side == "BUY" else "BUY")

            liquidity_usd = float(final_usd)
            if not await lighter.check_liquidity(symbol, lit_side_check, liquidity_usd, is_maker=True):
                logger.warning(f"🛑 {symbol}: Insufficient Lighter liquidity")
                return False

            # Reserve margin
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', Decimal('0')) + required_margin_check
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', Decimal('0')) + required_margin_check
                reserved_amount = required_margin_check

        except Exception as e:
            logger.error(f"Sizing error {symbol}: {e}")
            return False

        # Execute trade
        x10_id = None
        lit_id = None
        pending_recorded = False
        try:
            leg1_ex = opp.get('leg1_exchange', 'X10')
            leg1_side = opp.get('leg1_side', 'BUY')
            x10_side = leg1_side if leg1_ex == 'X10' else ("SELL" if leg1_side == "BUY" else "BUY")
            lit_side = leg1_side if leg1_ex == 'Lighter' else ("SELL" if leg1_side == "BUY" else "BUY")

            logger.info(f"🚀 Opening {symbol}: Size=${final_usd.quantize(Decimal('0.1'))}")
            
            async with RECENTLY_OPENED_LOCK:
                RECENTLY_OPENED_TRADES[symbol] = time.time()

            # PENDING Persistence
            try:
                px_mark = await x10.fetch_mark_price(symbol)
                pl_mark = await lighter.fetch_mark_price(symbol)
                entry_price_x10_est = safe_decimal(opp.get("entry_price_x10_est") or px_mark or 0)
                entry_price_lighter_est = safe_decimal(opp.get("entry_price_lighter_est") or pl_mark or 0)
                await add_trade_to_state(
                    {
                        "symbol": symbol,
                        "entry_time": datetime.now(timezone.utc),
                        "notional_usd": float(final_usd),
                        "status": "pending",
                        "leg1_exchange": leg1_ex,
                        "entry_price_x10": float(entry_price_x10_est),
                        "entry_price_lighter": float(entry_price_lighter_est),
                        "is_farm_trade": opp.get("is_farm_trade", False),
                        "account_label": "Main/Main",
                        "side_x10": x10_side,
                        "side_lighter": lit_side,
                    }
                )
                pending_recorded = True
            except Exception as e:
                logger.warning(f"⚠️ Failed to record PENDING trade for {symbol}: {e}")

            # Execute in parallel
            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol=symbol,
                side_x10=x10_side,
                side_lighter=lit_side,
                size_x10=float(final_usd),
                size_lighter=float(final_usd)
            )

            if success:
                # Get actual prices
                entry_price_x10 = Decimal('0')
                entry_price_lighter = Decimal('0')
                try:
                    await asyncio.sleep(0.5)
                    px_pos, lit_pos = await asyncio.gather(
                        x10.fetch_open_positions(),
                        lighter.fetch_open_positions(),
                        return_exceptions=True
                    )
                    px_pos = [] if isinstance(px_pos, Exception) else (px_pos or [])
                    lit_pos = [] if isinstance(lit_pos, Exception) else (lit_pos or [])
                    
                    x10_p = next((p for p in px_pos if p.symbol == symbol), None)
                    if x10_p and x10_p.entry_price > 0:
                        entry_price_x10 = x10_p.entry_price
                    
                    l_p = next((p for p in lit_pos if p.symbol == symbol), None)
                    if l_p and l_p.entry_price > 0:
                        entry_price_lighter = l_p.entry_price
                    
                    if entry_price_x10 <= 0:
                        entry_price_x10 = safe_decimal(await x10.fetch_mark_price(symbol) or 0)
                    if entry_price_lighter <= 0:
                        entry_price_lighter = safe_decimal(await lighter.fetch_mark_price(symbol) or 0)
                except Exception as e:
                    logger.warning(f"⚠️ Error fetching fill prices for {symbol}: {e}")
                    entry_price_x10 = safe_decimal(await x10.fetch_mark_price(symbol) or 0)
                    entry_price_lighter = safe_decimal(await lighter.fetch_mark_price(symbol) or 0)
                
                apy_value = float(opp.get('apy', 0.0))
                
                # Update to OPEN
                try:
                    from src.core.state import get_state_manager
                    sm = await get_state_manager()
                    updated = await sm.update_trade(
                        symbol,
                        {
                            "status": "open",
                            "entry_price_x10": float(entry_price_x10),
                            "entry_price_lighter": float(entry_price_lighter),
                            "x10_order_id": str(x10_id) if x10_id else None,
                            "lighter_order_id": str(lit_id) if lit_id else None,
                            "side_x10": x10_side,
                            "side_lighter": lit_side,
                            "size_usd": float(final_usd),
                            "is_farm_trade": opp.get("is_farm_trade", False),
                            "account_label": "Main/Main",
                        },
                    )
                    if not updated:
                        # Fallback add if update failed
                        trade_data = {
                            'symbol': symbol,
                            'entry_time': datetime.now(timezone.utc),
                            'notional_usd': float(final_usd),
                            'status': 'open',
                            'leg1_exchange': leg1_ex,
                            'entry_price_x10': float(entry_price_x10),
                            'entry_price_lighter': float(entry_price_lighter),
                            'is_farm_trade': opp.get('is_farm_trade', False),
                            'account_label': "Main/Main",
                            'x10_order_id': str(x10_id) if x10_id else None,
                            'lighter_order_id': str(lit_id) if lit_id else None,
                            'side_x10': x10_side,
                            'side_lighter': lit_side,
                        }
                        await add_trade_to_state(trade_data)
                    
                    logger.info(f"✅ OPENED {symbol}: ${final_usd:.2f}")
                    
                    if json_logger:
                        json_logger.trade_entry(
                            symbol=symbol,
                            side=x10_side,
                            size=float(final_usd),
                            price=float(entry_price_x10),
                            exchange="BOTH",
                            x10_order_id=str(x10_id) if x10_id else None,
                            lighter_order_id=str(lit_id)[:30] if lit_id else None,
                            entry_price_x10=float(entry_price_x10),
                            entry_price_lighter=float(entry_price_lighter),
                            apy=apy_value,
                            leg1_exchange=leg1_ex
                        )
                except Exception as db_error:
                    logger.error(f"❌ Failed to record trade {symbol}: {db_error}")
                    await publish_event(CriticalError(
                        message=f"🚨 ORPHAN TRADE: {symbol}",
                        details={
                            "symbol": symbol,
                            "x10_id": x10_id,
                            "lighter_id": lit_id,
                            "size_usd": float(final_usd)
                        }
                    ))
                
                return True
            else:
                logger.warning(f"❌ Trade execution failed for {symbol}")
                if pending_recorded:
                    try:
                        from src.core.state import get_state_manager
                        sm = await get_state_manager()
                        await sm.update_trade(symbol, {"status": "rollback", "closed_at": int(time.time() * 1000)})
                    except Exception:
                        pass
                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.error(f"Execution error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False
        finally:
            if reserved_amount > 0:
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] = max(Decimal('0'), IN_FLIGHT_MARGIN.get('X10', Decimal('0')) - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(Decimal('0'), IN_FLIGHT_MARGIN.get('Lighter', Decimal('0')) - reserved_amount)

                # Best-effort PnL enrichment
                try:
                    if x10_id or lit_id:
                        from src.core.state import get_state_manager
                        sm = await get_state_manager()
                        
                        fee_tasks = []
                        fee_tasks.append(x10.get_order_fee(str(x10_id)) if x10_id else asyncio.sleep(0, result=None))
                        fee_tasks.append(lighter.get_order_fee(str(lit_id)) if lit_id else asyncio.sleep(0, result=None))
                        fee_res = await asyncio.gather(*fee_tasks, return_exceptions=True)
                        
                        enrich_updates = {}
                        if not isinstance(fee_res[0], Exception) and fee_res[0] is not None:
                            enrich_updates["entry_fee_x10"] = safe_float(fee_res[0])
                        if not isinstance(fee_res[1], Exception) and fee_res[1] is not None:
                            enrich_updates["entry_fee_lighter"] = safe_float(fee_res[1])
                            
                        if enrich_updates:
                            await sm.update_trade(symbol, enrich_updates)
                except Exception as enrich_err:
                    logger.debug(f"PnL enrichment skipped for {symbol}: {enrich_err}")


# ============================================================
# CLOSE TRADE
# ============================================================
async def close_trade(trade: Dict, lighter, x10) -> bool:
    """Close trade on both exchanges with Decimal precision"""
    _, _, close_trade_in_state, _, _ = _get_state_functions()
    
    symbol = trade['symbol']
    notional_usd = safe_decimal(trade.get('notional_usd') or trade.get('size_usd') or 0)
    
    logger.info(f"🔻 CLOSING TRADE: {symbol} | Notional: ${float(notional_usd):.2f}")

    # Step 1: Close Lighter
    logger.info(f"📤 [LEG 1] Closing Lighter position for {symbol}...")
    lighter_success = False
    lighter_exit_order_id = None

    try:
        positions: List[Position] = await lighter.fetch_open_positions()
        pos = next((p for p in (positions or []) if p.symbol == symbol), None)
        size = pos.size if pos else Decimal('0')

        if pos and abs(size) > Decimal('1e-10'):
            side = "SELL" if size > 0 else "BUY"
            px = await lighter.fetch_mark_price(symbol)
            if px > 0:
                usd_size = abs(size) * px
                logger.info(f"   Lighter {symbol}: {'LONG' if size > 0 else 'SHORT'} {abs(size):.6f} coins @ ${px:.4f}")
                
                result = await lighter.place_order(
                    symbol=symbol,
                    side=side,
                    order_type="MARKET",
                    size=abs(size),
                    reduce_only=True
                )
                lighter_success = result.success
                lighter_exit_order_id = result.order_id
                
                if lighter_success:
                    logger.info(f"✅ [LEG 1] Lighter {symbol} closed: {lighter_exit_order_id}")
                else:
                    logger.error(f"❌ [LEG 1] Lighter {symbol} close failed: {result.error_message}")
            else:
                logger.error(f"❌ [LEG 1] Lighter close {symbol}: Invalid price (px={px})")
        else:
            logger.info(f"✅ [LEG 1] Lighter {symbol}: No position to close")
            lighter_success = True

    except Exception as e:
        logger.error(f"❌ [LEG 1] Lighter close failed for {symbol}: {e}")

    if not lighter_success:
        logger.error(f"❌ Lighter failed for {symbol}, keeping X10 as hedge")
        return False

    # Step 2: Close X10
    logger.info(f"✅ [LEG 1] Lighter {symbol} closed, closing X10...")

    x10_success = False
    x10_exit_order_id = None
    try:
        x10_exit_order_id = await safe_close_x10_position(x10, symbol, "AUTO", Decimal('0'))
        x10_success = x10_exit_order_id is not None
        if x10_success:
            logger.info(f"✅ [LEG 2] X10 {symbol} closed: {x10_exit_order_id}")
        else:
            logger.error(f"❌ [LEG 2] X10 {symbol} close failed")
    except Exception as e:
        logger.error(f"❌ [LEG 2] X10 close failed for {symbol}: {e}")

    if lighter_success and x10_success:
        logger.info(f"✅ TRADE CLOSED: {symbol}")
        return True
    else:
        logger.warning(f"⚠️ TRADE PARTIALLY CLOSED: {symbol}")
        return False


async def process_symbol(symbol: str, lighter, x10, parallel_exec, lock: asyncio.Lock):
    """Process a single symbol for trading opportunity using Decimal"""
    async with lock:
        try:
            if symbol in getattr(config, 'BLACKLIST_SYMBOLS', []):
                return

            if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 300):
                return

            get_open_trades, _, _, _, _ = _get_state_functions()
            open_trades = await get_open_trades()
            open_symbols = {t['symbol'] for t in open_trades}
            if symbol in open_symbols:
                return

            max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
            if len(open_trades) >= max_trades:
                return

            # Prices and funding rates (Decimal)
            px = await x10.fetch_mark_price(symbol)
            pl = await lighter.fetch_mark_price(symbol)
            rl = await lighter.fetch_funding_rate(symbol)
            rx = await x10.fetch_funding_rate(symbol)

            if rl is None or rx is None or px is None or pl is None:
                return

            # APY (Decimal)
            net = rl - rx
            apy = abs(net) * Decimal('24') * Decimal('365')

            from src.core.adaptive_threshold import get_threshold_manager
            threshold_manager = get_threshold_manager()
            req_apy = safe_decimal(threshold_manager.get_threshold(symbol, is_maker=True))
            if apy < req_apy:
                return

            # Spread (Decimal)
            spread = abs(px - pl) / px
            max_spread = safe_decimal(getattr(config, 'MAX_SPREAD_FILTER_PERCENT', 0.005))
            if spread > max_spread:
                return

            # OI (Decimal)
            oi_x10 = Decimal('0')
            oi_lighter = Decimal('0')
            try:
                # Assuming adapters might need to be updated to return Decimal for OI
                oi_x10 = safe_decimal(await x10.fetch_open_interest(symbol))
            except Exception: pass
            try:
                oi_lighter = safe_decimal(await lighter.fetch_open_interest(symbol))
            except Exception: pass
            
            total_oi = oi_x10 + oi_lighter
            min_oi_usd = safe_decimal(getattr(config, 'MIN_OPEN_INTEREST_USD', 50000))
            if total_oi > 0 and total_oi < min_oi_usd:
                return

            opp = {
                'symbol': symbol,
                'apy': float(apy * Decimal('100')),
                'net_funding_hourly': float(net),
                'leg1_exchange': 'Lighter' if rl > rx else 'X10',
                'leg1_side': 'SELL' if rl > rx else 'BUY',
                'is_farm_trade': False,
                'spread_pct': float(spread),
                'open_interest': float(total_oi),
                'prediction_confidence': 0.5
            }

            logger.info(f"✅ {symbol}: EXECUTING trade APY={opp['apy']:.2f}% spread={float(spread)*100:.2f}%")
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}", exc_info=True)
            await publish_event(NotificationEvent(
                level="ERROR",
                message=f"Symbol Error {symbol}: {e}"
            ))
        finally:
            ACTIVE_TASKS.pop(symbol, None)

