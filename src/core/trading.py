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
from src.kelly_sizing import get_kelly_sizer
from src.fee_manager import get_fee_manager
from src.telegram_bot import get_telegram_bot

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
RECENTLY_OPENED_PROTECTION_SECONDS = 60.0


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
        # Check if position already exists
        try:
            x10_positions = await x10.fetch_open_positions()
            lighter_positions = await lighter.fetch_open_positions()
            
            x10_symbols = {p.get('symbol') for p in (x10_positions or [])}
            lighter_symbols = {p.get('symbol') for p in (lighter_positions or [])}
            
            if symbol in x10_symbols:
                logger.warning(f"⚠️ {symbol} already open on X10 - SKIP")
                return False
            
            if symbol in lighter_symbols:
                logger.warning(f"⚠️ {symbol} already open on Lighter - SKIP")
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

            # Kelly sizing
            available_capital = min(bal_x10_real, bal_lit_real)
            apy = opp.get('apy', 0.0)
            apy_decimal = apy / 100.0 if apy > 1 else apy
            
            kelly_sizer = get_kelly_sizer()
            kelly_result = kelly_sizer.calculate_position_size(
                symbol=symbol,
                available_capital=available_capital,
                current_apy=apy_decimal
            )
            
            logger.info(
                f"🎰 KELLY {symbol}: win_rate={kelly_result.win_rate:.1%}, "
                f"kelly_fraction={kelly_result.kelly_fraction:.4f}"
            )

            # Calculate final size
            if opp.get('is_farm_trade'):
                target_farm = float(getattr(config, 'FARM_NOTIONAL_USD', 12.0))
                if kelly_result.safe_fraction > 0.02:
                    kelly_adjusted = min(float(kelly_result.recommended_size_usd), target_farm * 1.5)
                    final_usd = max(target_farm, kelly_adjusted, min_req)
                else:
                    final_usd = max(target_farm, min_req)
            else:
                final_usd = float(kelly_result.recommended_size_usd)
                if final_usd < min_req:
                    can_afford = min_req <= available_capital * 0.9
                    within_limits = min_req <= config.MAX_TRADE_SIZE_USD and min_req <= max_per_trade
                    if can_afford and within_limits:
                        final_usd = float(min_req)
                    else:
                        return False

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

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol=symbol,
                side_x10=x10_side,
                side_lighter=lit_side,
                size_x10=final_usd,
                size_lighter=final_usd
            )

            if success:
                trade_data = {
                    'symbol': symbol,
                    'entry_time': datetime.now(timezone.utc),
                    'notional_usd': final_usd,
                    'status': 'OPEN',
                    'leg1_exchange': leg1_ex,
                    'entry_price_x10': x10.fetch_mark_price(symbol) or 0.0,
                    'entry_price_lighter': lighter.fetch_mark_price(symbol) or 0.0,
                    'is_farm_trade': opp.get('is_farm_trade', False),
                    'account_label': "Main/Main",
                    'x10_order_id': str(x10_id) if x10_id else None,
                    'lighter_order_id': str(lit_id) if lit_id else None
                }
                await add_trade_to_state(trade_data)
                
                logger.info(f"✅ OPENED {symbol}: ${final_usd:.2f}")
                return True
            else:
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
    Close trade on both exchanges.
    """
    _, _, close_trade_in_state, _, _ = _get_state_functions()
    
    symbol = trade['symbol']
    logger.info(f" 🔻 CLOSING {symbol}...")

    # Step 1: Close Lighter
    lighter_success = False
    lighter_code_error = False
    lighter_exit_order_id = None

    try:
        positions = await lighter.fetch_open_positions()
        pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
        size = safe_float(pos.get('size', 0)) if pos else 0.0

        if pos and abs(size) > 1e-10:
            side = "SELL" if size > 0 else "BUY"
            px = safe_float(lighter.fetch_mark_price(symbol))
            if px > 0:
                usd_size = abs(size) * px
                
                try:
                    result = await lighter.close_live_position(symbol, side, usd_size)
                    if isinstance(result, tuple):
                        lighter_success = result[0]
                        lighter_exit_order_id = result[1] if len(result) > 1 else None
                    else:
                        lighter_success = bool(result)
                except TypeError as type_err:
                    logger.critical(f"🚨 TypeError in Lighter close for {symbol}: {type_err}")
                    lighter_code_error = True
                except Exception as close_err:
                    logger.error(f"❌ Lighter close failed for {symbol}: {close_err}")
            else:
                logger.error(f"❌ Lighter close {symbol}: Invalid price")
        else:
            logger.info(f"✅ Lighter {symbol}: No position to close")
            lighter_success = True

    except TypeError as e:
        logger.critical(f"🚨 TypeError in Lighter close flow for {symbol}: {e}")
        lighter_code_error = True
    except Exception as e:
        logger.error(f"❌ Lighter close failed for {symbol}: {e}")

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

    logger.info(f"✅ Lighter {symbol} OK, closing X10...")

    x10_success = False
    try:
        x10_exit_order_id = await safe_close_x10_position(x10, symbol, "AUTO", 0)
        x10_success = x10_exit_order_id is not None
    except Exception as e:
        logger.error(f"❌ X10 close failed for {symbol}: {e}")

    if lighter_success and x10_success:
        logger.info(f"✅ {symbol} fully closed")
        return True
    else:
        logger.warning(f"⚠️ {symbol} partial: Lighter={lighter_success}, X10={x10_success}")
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

            net = rl - rx
            apy = abs(net) * 24 * 365  # Hourly rates -> 24x per day

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

