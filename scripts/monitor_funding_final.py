# monitor_funding.py
import sys
import os
import time
import asyncio
import aiosqlite
import inspect
import logging
import random
import traceback
import aiohttp  # ← HINZUFÜGEN
import signal
from datetime import datetime, timezone
from typing import Optional, List, Dict, Callable, Awaitable, Any, Set
from decimal import Decimal
from dataclasses import dataclass, field
from enum import Enum

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
from src.open_interest_tracker import OpenInterestTracker  # OI tracking
from src.prediction_v2 import get_predictor
from src.latency_arb import get_detector
from src.funding_tracker import create_funding_tracker  # Funding payment tracking
from src.state_manager import (
    InMemoryStateManager,
    get_state_manager,
    close_state_manager,
    TradeState,
    TradeStatus
)
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.parallel_execution import ParallelExecutionManager
from src.account_manager import get_account_manager
from src.websocket_manager import WebSocketManager
from src.prediction import predict_next_funding_rates
from src.kelly_sizing import get_kelly_sizer, calculate_smart_size, KellyResult
from src.database import (
    get_database,
    get_trade_repository,
    get_funding_repository,
    get_execution_repository,
    close_database
)

# Logging Setup
logger = config.setup_logging(per_run=True, run_id=os.getenv("RUN_ID"))
config.validate_runtime_config(logger)

# Globals
FAILED_COINS = {}
ACTIVE_TASKS: dict[str, asyncio.Task] = {}           # symbol → Task (nur eine pro Symbol gleichzeitig)
SYMBOL_LOCKS: dict[str, asyncio.Lock] = {}           # symbol → asyncio.Lock() für race-free execution
SHUTDOWN_FLAG = False
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 5.0  # 5s for fresher data (was 30s)
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
TASKS_LOCK = asyncio.Lock()
OPPORTUNITY_LOG_CACHE = {}

# ============================================================
# GLOBALS (Ergänzung)
# ============================================================
# Behalte IN_FLIGHT_MARGIN von vorher!
# Define in-flight reservation globals (used when reserving trade notional)
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()

# Funding Tracker (tracks realized profit from funding payments)
FUNDING_TRACKER = None
# ============================================================
# EXIT LOGIC - POSITION CLOSING
# ============================================================
async def check_and_execute_exits(lighter: LighterAdapter, x10: X10Adapter, 
                                   parallel_exec: ParallelExecutionManager) -> int:
    """
    Check all open trades and close positions where spread has converged.
    
    Exit Condition: abs(x10_rate - lighter_rate) < 0.001 (0.1% APY threshold)
    
    Returns: Number of positions closed
    """
    if SHUTDOWN_FLAG:
        return 0
    
    closed_count = 0
    open_trades = await get_open_trades()
    
    if not open_trades:
        return 0
    
    logger.info(f"🔍 Checking {len(open_trades)} open positions for exit conditions...")
    
    for trade in open_trades:
        symbol = trade.get('symbol')
        if not symbol:
            continue
        
        try:
            # Get current funding rates
            rate_x10 = x10.fetch_funding_rate(symbol)
            rate_lighter = lighter.fetch_funding_rate(symbol)
            
            if rate_x10 is None or rate_lighter is None:
                logger.warning(f"⚠️ {symbol}: Missing funding rate (X10={rate_x10}, Lighter={rate_lighter})")
                continue
            
            spread = abs(rate_lighter - rate_x10)
            spread_apy = spread * 3 * 365 * 100  # Convert to APY%
            
            # Exit condition: spread converged below threshold
            EXIT_THRESHOLD = 0.001  # 0.1% APY (configurable)
            
            if spread < EXIT_THRESHOLD:
                logger.info(f"🚪 {symbol}: Spread converged ({spread_apy:.2f}% APY) - Closing position...")
                
                success = await close_position(
                    symbol=symbol,
                    trade=trade,
                    lighter=lighter,
                    x10=x10,
                    reason=f"Spread converged to {spread_apy:.2f}% APY"
                )
                
                if success:
                    closed_count += 1
                    logger.info(f"✅ {symbol}: Position closed successfully")
                else:
                    logger.error(f"❌ {symbol}: Failed to close position")
            else:
                logger.debug(f"📊 {symbol}: Spread still wide ({spread_apy:.2f}% APY) - keeping position")
        
        except Exception as e:
            logger.error(f"❌ {symbol}: Error checking exit condition: {e}")
            import traceback
            traceback.print_exc()
    
    return closed_count


async def close_position(symbol: str, trade: Dict, lighter: LighterAdapter, 
                         x10: X10Adapter, reason: str = "Manual close") -> bool:
    """
    Close a position on both exchanges using reduce-only orders.
    
    Returns: True if both legs closed successfully
    """
    logger.info(f"🔄 {symbol}: Closing position - {reason}")
    
    # Get position details from trade
    side_x10 = trade.get('side_x10', 'BUY')
    side_lighter = trade.get('side_lighter', 'SELL')
    size_usd = trade.get('size_usd', 0)
    
    if size_usd <= 0:
        logger.error(f"❌ {symbol}: Invalid size_usd={size_usd}")
        return False
    
    # Get current prices for closing orders
    price_x10 = await x10.get_price(symbol)
    price_lighter = await lighter.get_price(symbol)
    
    if not price_x10 or not price_lighter:
        logger.error(f"❌ {symbol}: Failed to get prices (X10={price_x10}, Lighter={price_lighter})")
        return False
    
    # Calculate sizes
    size_x10 = size_usd / price_x10
    size_lighter = size_usd / price_lighter
    
    logger.info(f"📐 {symbol}: Closing X10 {side_x10} {size_x10:.4f} @ ${price_x10:.2f}")
    logger.info(f"📐 {symbol}: Closing Lighter {side_lighter} {size_lighter:.4f} @ ${price_lighter:.2f}")
    
    # Track results
    x10_success = False
    lighter_success = False
    x10_order_id = None
    lighter_order_id = None
    
    try:
        # ═══════════════════════════════════════════════════════════════
        # CLOSE X10 POSITION - Use reduce_only=True
        # ═══════════════════════════════════════════════════════════════
        close_side_x10 = "SELL" if side_x10 == "BUY" else "BUY"
        
        logger.info(f"🔻 {symbol}: Sending X10 close order ({close_side_x10}) with reduce_only=True...")
        
        x10_result = await x10.place_order(
            symbol=symbol,
            side=close_side_x10,
            size_usd=size_usd,
            reduce_only=True  # ⚠️ CRITICAL: Prevents flipping to opposite side
        )
        
        if x10_result and x10_result.get('success'):
            x10_order_id = x10_result.get('order_id')
            x10_success = True
            logger.info(f"✅ {symbol}: X10 close order placed - ID: {x10_order_id}")
        else:
            logger.error(f"❌ {symbol}: X10 close failed - {x10_result}")
        
        # ═══════════════════════════════════════════════════════════════
        # CLOSE LIGHTER POSITION - Opposing order with reduce_only
        # ═══════════════════════════════════════════════════════════════
        # Lighter SDK: is_ask=True means SELL, is_ask=False means BUY
        # Close logic: If opened with SELL (is_ask=True), close with BUY (is_ask=False)
        
        is_ask_lighter = (side_lighter == "SELL")
        close_is_ask_lighter = not is_ask_lighter  # Opposite of open side
        
        logger.info(f"🔻 {symbol}: Sending Lighter close order (is_ask={close_is_ask_lighter}) with reduce_only=True...")
        
        lighter_result = await lighter.place_order(
            symbol=symbol,
            is_ask=close_is_ask_lighter,
            size_base=size_lighter,
            price=price_lighter,
            reduce_only=True  # ⚠️ CRITICAL: Prevents flipping to opposite side
        )
        
        if lighter_result and lighter_result.get('success'):
            lighter_order_id = lighter_result.get('order_id')
            lighter_success = True
            logger.info(f"✅ {symbol}: Lighter close order placed - ID: {lighter_order_id}")
        else:
            logger.error(f"❌ {symbol}: Lighter close failed - {lighter_result}")
        
        # ═══════════════════════════════════════════════════════════════
        # DATABASE UPDATE - Only if BOTH legs succeeded
        # ═══════════════════════════════════════════════════════════════
        if x10_success and lighter_success:
            # Calculate PnL (simplified - you may want more sophisticated calculation)
            entry_price_x10 = trade.get('entry_price_x10', price_x10)
            entry_price_lighter = trade.get('entry_price_lighter', price_lighter)
            
            pnl_x10 = (price_x10 - entry_price_x10) * size_x10 if side_x10 == "BUY" else (entry_price_x10 - price_x10) * size_x10
            pnl_lighter = (entry_price_lighter - price_lighter) * size_lighter if side_lighter == "SELL" else (price_lighter - entry_price_lighter) * size_lighter
            total_pnl = pnl_x10 + pnl_lighter
            
            funding_collected = trade.get('funding_collected', 0)
            
            # Close trade in state manager (updates DB in background)
            await close_trade_in_state(symbol, pnl=total_pnl, funding=funding_collected)
            
            logger.info(f"✅ {symbol}: Position closed successfully! PnL=${total_pnl:.2f}, Funding=${funding_collected:.2f}")
            return True
        else:
            # PARTIAL FILL - Log error but don't update DB yet
            logger.error(
                f"⚠️ {symbol}: PARTIAL CLOSE - X10: {x10_success}, Lighter: {lighter_success}\n"
                f"   X10 Order ID: {x10_order_id}\n"
                f"   Lighter Order ID: {lighter_order_id}\n"
                f"   ⚠️ MANUAL INTERVENTION MAY BE REQUIRED"
            )
            return False
    
    except Exception as e:
        logger.error(f"❌ {symbol}: Exception during close: {e}")
        import traceback
        traceback.print_exc()
        return False


async def exit_monitoring_loop(lighter: LighterAdapter, x10: X10Adapter, 
                                parallel_exec: ParallelExecutionManager):
    """
    Periodic loop to check for exit conditions on open positions.
    Runs every 30 seconds to check if spread has converged.
    """
    logger.info("🔍 Exit monitoring loop started")
    
    while not SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            
            if SHUTDOWN_FLAG:
                break
            
            closed_count = await check_and_execute_exits(lighter, x10, parallel_exec)
            
            if closed_count > 0:
                logger.info(f"✅ Closed {closed_count} position(s) due to spread convergence")
        
        except asyncio.CancelledError:
            logger.info("Exit monitoring loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in exit monitoring loop: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(60)  # Wait longer on error


# ============================================================
# PREDICTION SYSTEM STUBS
# ============================================================
async def update_funding_data(symbol: str, rate_x10: float, rate_lighter: float, 
                               mark_price: float, open_interest: float):
    """Stub for prediction system - data already cached in adapters"""
    pass

async def get_best_opportunities(symbols: list, min_apy: float, min_confidence: float, limit: int):
    """Stub - returns empty list, main logic uses find_opportunities fallback"""
    return []

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

async def process_symbol(symbol: str, lighter: LighterAdapter, x10: X10Adapter, 
                          parallel_exec: ParallelExecutionManager, lock: asyncio.Lock):
    """Einzelne Symbol-Verarbeitung mit OI-Integration"""
    async with lock:
        try:
            # Quick guards: blacklist, recent failures
            if symbol in config.BLACKLIST_SYMBOLS:
                logger.debug(f"{symbol}: in BLACKLIST, skip")
                return

            if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 300):
                logger.debug(f"{symbol}: in FAILED_COINS cooldown, skip")
                return

            # Check if already have open position
            open_trades = await get_open_trades()
            open_symbols = {t['symbol'] for t in open_trades}
            if symbol in open_symbols:
                logger.debug(f"{symbol}: already have open position, skip")
                return

            # Check max open trades limit
            if len(open_trades) >= config.MAX_OPEN_TRADES:
                logger.debug(f"{symbol}: MAX_OPEN_TRADES ({config.MAX_OPEN_TRADES}) reached, skip")
                return

            # Prices and funding rates (cached fetches)
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

            # Update volatility monitor with latest price
            try:
                get_volatility_monitor().update_price(symbol, float(px))
            except Exception:
                pass

            net = rl - rx
            apy = abs(net) * 3 * 365  # Funding wird 3x pro Tag gezahlt (8h intervals)

            # Threshold check
            threshold_manager = get_threshold_manager()
            req_apy = threshold_manager.get_threshold(symbol, is_maker=True)
            if apy < req_apy:
                logger.debug(f"{symbol}: APY {apy*100:.2f}% < required {req_apy*100:.2f}%")
                return

            # Price validity & spread
            try:
                px_f = float(px)
                pl_f = float(pl)
                if px_f <= 0 or pl_f <= 0:
                    logger.debug(f"{symbol}: invalid prices px={px_f}, pl={pl_f}")
                    return
                spread = abs(px_f - pl_f) / px_f
                if spread > config.MAX_SPREAD_FILTER_PERCENT:
                    logger.debug(f"{symbol}: spread {spread*100:.2f}% > max {config.MAX_SPREAD_FILTER_PERCENT*100:.2f}%")
                    return
            except Exception as e:
                logger.debug(f"{symbol}: price parsing error: {e}")
                return

            # ════════════════════════════════════════════════════════════════
            # Open Interest Check (VERBESSERT)
            # ════════════════════════════════════════════════════════════════
            try:
                from src.open_interest_tracker import get_oi_tracker
                oi_tracker = get_oi_tracker()
            except Exception:
                oi_tracker = None
            
            total_oi = 0.0
            oi_trend = "UNKNOWN"
            
            # Erst vom Tracker versuchen (schneller, gecached)
            if oi_tracker:
                try:
                    metrics = oi_tracker.get_metrics(symbol)
                    if metrics and metrics.current_oi > 0:
                        total_oi = metrics.current_oi
                        oi_trend = metrics.trend.value
                        logger.debug(f"📊 {symbol}: OI from tracker = ${total_oi:,.0f} ({oi_trend})")
                except Exception:
                    pass
            
            # Fallback: Direkt von Exchanges holen
            if total_oi == 0:
                try:
                    oi_x10 = await x10.fetch_open_interest(symbol)
                    oi_lighter = await lighter.fetch_open_interest(symbol)
                    total_oi = (oi_x10 or 0) + (oi_lighter or 0)
                except Exception:
                    pass
            
            # OI-basierte Validierung
            min_oi_usd = getattr(config, 'MIN_OPEN_INTEREST_USD', 50000)
            if total_oi > 0 and total_oi < min_oi_usd:
                logger.debug(f"{symbol}: OI ${total_oi:,.0f} < min ${min_oi_usd:,.0f}")
                return

            # Zusätzlich: Skip bei stark fallender OI-Entwicklung
            try:
                vel_warn = getattr(config, 'OI_MIN_VELOCITY_WARN', -1000)
                if oi_trend == "FALLING" and oi_tracker:
                    metrics = oi_tracker.get_metrics(symbol)
                    if metrics and metrics.velocity_5m < vel_warn:
                        logger.debug(f"{symbol}: OI rapidly falling (vel={metrics.velocity_5m:.0f}/min)")
                        return
            except Exception:
                pass

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

            # Execute using the deeper routine that handles reservations & cleanup
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)

        except Exception as e:
            logger.error(f"Fehler bei Verarbeitung von {symbol}: {e}", exc_info=e)
            if telegram_bot and telegram_bot.enabled:
                asyncio.create_task(telegram_bot.send_error(f"Symbol Error {symbol}: {e}"))
        finally:
            # WICHTIG: Task aus ACTIVE_TASKS entfernen → cleanup kann laufen
            ACTIVE_TASKS.pop(symbol, None)
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
def get_local_state_manager():
    """Return the global state manager instance (sync helper)"""
    return state_manager

# ============================================================
# STATE & DB WRAPPERS
# ============================================================
state_manager = None

async def get_open_trades() -> list:
    """Get open trades from in-memory state (instant)"""
    global state_manager
    if state_manager is None:
        # Import the REAL async get_state_manager from state_manager module
        from src.state_manager import get_state_manager as get_sm
        state_manager = await get_sm()
    sm = state_manager
    trades = await sm.get_all_open_trades()
    # Convert to dict format for backwards compatibility
    return [t.to_dict() for t in trades]

async def add_trade_to_state(trade_data: dict) -> str:
    """Add trade to in-memory state (writes to DB in background)"""
    sm = await get_state_manager()
    trade = TradeState(
        symbol=trade_data['symbol'],
        side_x10=trade_data.get('side_x10', 'BUY'),
        side_lighter=trade_data.get('side_lighter', 'SELL'),
        size_usd=trade_data.get('size_usd', 0),
        entry_price_x10=trade_data.get('entry_price_x10', 0),
        entry_price_lighter=trade_data.get('entry_price_lighter', 0),
        is_farm_trade=trade_data.get('is_farm_trade', False),
        account_label=trade_data.get('account_label', 'Main'),
        x10_order_id=trade_data.get('x10_order_id'),
        lighter_order_id=trade_data.get('lighter_order_id'),
    )
    return await sm.add_trade(trade)

async def close_trade_in_state(symbol: str, pnl: float = 0, funding: float = 0):
    """Close trade in state (writes to DB in background)"""
    sm = await get_state_manager()
    await sm.close_trade(symbol, pnl, funding)
    logger.info(f"✅ Trade {symbol} closed (PnL: ${pnl:.2f})")

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

async def find_opportunities(lighter, x10, open_syms, is_farm_mode: bool = None) -> List[Dict]:
    """
    Find trading opportunities

    Args:
        lighter: Lighter adapter
        x10: X10 adapter
        open_syms: Set of already open symbols
        is_farm_mode: If True, mark all trades as farm trades. If None, auto-detect from config.
    """
    # Auto-detect farm mode if not specified
    if is_farm_mode is None:
        is_farm_mode = config.VOLUME_FARM_MODE

    # Get OI tracker instance
    from src.open_interest_tracker import get_oi_tracker
    oi_tracker = get_oi_tracker()

    opps: List[Dict] = []
    common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()

    mode_indicator = "🚜 FARM" if is_farm_mode else "💎 ARB"
    logger.info(f"🔍 {mode_indicator} Scanning {len(common)} pairs. Open symbols to skip: {open_syms}")
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

    # Launch concurrent fetchs
    tasks = [asyncio.create_task(fetch_symbol_data(s)) for s in common]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions
    clean_results = []
    x10_rates = {}
    lighter_rates = {}
    
    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"Task exception: {r}")
            continue
        clean_results.append(r)
        
        # Build rate dictionaries for prediction system
        s, lr, xr, px, pl = r
        if xr is not None:
            x10_rates[s] = xr
        if lr is not None:
            lighter_rates[s] = lr

    # Update Predictor with current data
    all_symbols = list(common)
    for symbol in all_symbols:
        rate_x10 = x10_rates.get(symbol, 0)
        rate_lighter = lighter_rates.get(symbol, 0)
        
        if rate_x10 != 0 or rate_lighter != 0:
            await update_funding_data(
                symbol=symbol,
                rate_x10=rate_x10,
                rate_lighter=rate_lighter,
                mark_price=lighter.fetch_mark_price(symbol) or 0,
                open_interest=0  # TODO: Add OI when available
            )
    
    # Get predictions
    predictions = await get_best_opportunities(
        symbols=[s for s in all_symbols if s not in open_syms],
        min_apy=getattr(config, 'MIN_APY_THRESHOLD', 10.0),
        min_confidence=0.5,
        limit=20
    )
    
    # Convert predictions to opportunity format
    opportunities = []
    for pred in predictions:
        if not pred.should_trade:
            continue
        
        opp = {
            'symbol': pred.symbol,
            'apy': pred.predicted_apy,
            'spread': pred.current_spread,
            'confidence': pred.confidence,
            'predicted_direction': pred.predicted_direction,
            'size_multiplier': pred.recommended_size_multiplier,
            'regime': pred.regime.value,
            'signals': pred.signals,
            # Existing fields for compatibility
            'rate_x10': x10_rates.get(pred.symbol, 0),
            'rate_lighter': lighter_rates.get(pred.symbol, 0),
            'is_farm_trade': is_farm_mode or False,
            'net_funding_hourly': lighter_rates.get(pred.symbol, 0) - x10_rates.get(pred.symbol, 0),
            'leg1_exchange': 'Lighter' if pred.predicted_direction == "long_lighter" else 'X10',
            'spread_pct': pred.current_spread,
            'price_x10': x10.fetch_mark_price(pred.symbol) or 0,
            'price_lighter': lighter.fetch_mark_price(pred.symbol) or 0
        }
        
        # Determine sides based on prediction
        if pred.predicted_direction == "long_x10":
            opp['leg1_side'] = 'BUY'   # Long on X10
            opp['leg2_side'] = 'SELL'  # Short on Lighter
        else:
            opp['leg1_side'] = 'SELL'  # Short on X10
            opp['leg2_side'] = 'BUY'   # Long on Lighter
        
        opportunities.append(opp)
    
    # Sort by APY * confidence
    opportunities.sort(key=lambda x: x['apy'] * x.get('confidence', 1.0), reverse=True)
    
    logger.info(f"✅ Found {len(opportunities)} opportunities with predictions (farm_mode={is_farm_mode})")
    
    # NUR returnen wenn wir tatsächlich Opportunities haben
    if opportunities:
        return opportunities[:config.MAX_OPEN_TRADES]

    # Wenn keine Predictions, weiter zum Fallback-Code unten
    logger.info("⚠️ Keine Prediction-Opportunities, nutze Fallback mit echten Funding Rates...")

    # Update threshold manager (old code kept for fallback)
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

        if s in FAILED_COINS and (now_ts - FAILED_COINS[s] < 60):
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
        apy = abs(net) * 3 * 365  # Funding wird 3x pro Tag gezahlt (8h intervals)

        # ↓↓↓ DIESE ZEILE EINFÜGEN ↓↓↓
        if apy > 0.5:  # Nur loggen wenn APY > 50%
            logger.info(f"🔍 DEBUG {s}: rl={rl:.8f}, rx={rx:.8f}, net={net:.8f}, apy={apy*100:.1f}%")

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

        # ════════════════════════════════════════════════════════════════
        # OPEN INTEREST CHECK (NEU!)
        # ════════════════════════════════════════════════════════════════
        oi_value = 0.0
        oi_trend = "UNKNOWN"
        
        # Versuche OI vom Tracker zu holen
        try:
            from src.open_interest_tracker import get_oi_tracker
            tracker = get_oi_tracker()
        except Exception:
            tracker = None
        
        if tracker:
            try:
                metrics = tracker.get_metrics(s)
                if metrics:
                    oi_value = metrics.current_oi
                    oi_trend = metrics.trend.value
                    
                    # Skip wenn OI zu niedrig (illiquide)
                    min_oi = getattr(config, 'MIN_OPEN_INTEREST_USD', 50000)
                    if oi_value > 0 and oi_value < min_oi:
                        logger.debug(f"Skip {s}: OI ${oi_value:,.0f} < min ${min_oi:,.0f}")
                        continue
                    
                    # Skip wenn OI stark fallend (Liquidität verschwindet)
                    vel_warn = getattr(config, 'OI_MIN_VELOCITY_WARN', -1000)
                    if oi_trend == "FALLING" and metrics.velocity_5m < vel_warn:
                        logger.debug(f"Skip {s}: OI rapidly falling (vel={metrics.velocity_5m:.0f}/min)")
                        continue
            except Exception as e:
                logger.debug(f"OI check failed for {s}: {e}")

        # Log high APY opportunities MIT OI
        if apy > 0.5:
            if now_ts - OPPORTUNITY_LOG_CACHE.get(s, 0) > 60:
                # OI Info hinzufügen
                oi_str = ""
                if tracker:
                    try:
                        metrics = tracker.get_metrics(s)
                        if metrics and metrics.current_oi > 0:
                            oi_str = f" | OI: ${metrics.current_oi:,.0f} ({metrics.trend.value})"
                    except:
                        pass
                logger.info(f"💎 {s} | APY: {apy*100:.1f}%{oi_str}")
                OPPORTUNITY_LOG_CACHE[s] = now_ts

        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net,
            'leg1_exchange': 'Lighter' if rl > rx else 'X10',
            'leg1_side': 'SELL' if rl > rx else 'BUY',
            'is_farm_trade': is_farm_mode,
            'spread_pct': spread,
            'price_x10': px_float,
            'price_lighter': pl_float,
            'open_interest': oi_value,  # NEU: OI im Opportunity dict
            'oi_trend': oi_trend         # NEU: OI Trend
        })

    # IMPORTANT: Set farm flag based on config (ensure farm loop state applies)
    farm_mode_active = getattr(config, 'VOLUME_FARM_MODE', False)
    for opp in opps:
        if farm_mode_active:
            opp['is_farm_trade'] = True

    logger.info(f"✅ Found {len(opps)} opportunities from {valid_pairs} valid pairs (farm_mode={farm_mode_active}, scanned={len(clean_results)})")

    opps.sort(key=lambda x: x['apy'], reverse=True)
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
    reserved_amount = 0.0
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

        # Prediction Logic
        predictor = get_predictor()
        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        net_rate = current_rate_lit - current_rate_x10

        pred_rate, delta, conf = await predictor.predict_next_funding_rate(
            symbol=symbol,
            current_lighter_rate=current_rate_lit,
            current_x10_rate=current_rate_x10,
            lighter_adapter=lighter,
            x10_adapter=x10,
            btc_price=x10.fetch_mark_price("BTC-USD")
        )

        # Predictor Filter
        if not opp.get('is_farm_trade'):
            if conf > 0.7 and delta < -0.00001:
                logger.info(f"Skipping {symbol}: predictor negative (delta={delta:.6f})")
                return False
            elif conf < 0.3 and abs(net_rate) < 0.0001:
                return False

        # ═══════════════════════════════════════════════════════════════
        # SIZING & VALIDATION MIT KELLY CRITERION
        # ═══════════════════════════════════════════════════════════════
        global IN_FLIGHT_MARGIN, IN_FLIGHT_LOCK

        # 1. Determine Minimum Requirement
        min_req_x10 = x10.min_notional_usd(symbol)
        min_req_lit = lighter.min_notional_usd(symbol)
        min_req = max(min_req_x10, min_req_lit)

        # 2. Check Global Max Constraints
        max_per_trade = float(getattr(config, 'MAX_NOTIONAL_USD', 18.0))  # FIX: Variable war undefiniert
        
        if min_req > config.MAX_TRADE_SIZE_USD:
            logger.debug(f"Skip {symbol}: Min Req ${min_req:.2f} > Max Config ${config.MAX_TRADE_SIZE_USD}")
            return False

        try:
            # 3. Get Balance
            async with IN_FLIGHT_LOCK:
                raw_x10 = await x10.get_real_available_balance()
                raw_lit = await lighter.get_real_available_balance()
                bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))

            # 4. KELLY CRITERION BERECHNUNG (für ALLE Trades)
            available_capital = min(bal_x10_real, bal_lit_real)
            apy = opp.get('apy', 0.0)
            apy_decimal = apy / 100.0 if apy > 1 else apy  # Normalisiere APY
            
            kelly_sizer = get_kelly_sizer()
            kelly_result = kelly_sizer.calculate_position_size(
                symbol=symbol,
                available_capital=available_capital,
                current_apy=apy_decimal
            )
            
            # KELLY LOGGING (das hat gefehlt!)
            logger.info(
                f"🎰 KELLY {symbol}: win_rate={kelly_result.win_rate:.1%}, "
                f"kelly_fraction={kelly_result.kelly_fraction:.4f}, "
                f"safe_fraction={kelly_result.safe_fraction:.4f}, "
                f"confidence={kelly_result.confidence}, "
                f"samples={kelly_result.sample_size}"
            )

            # 5. Calculate Final Size
            if opp.get('is_farm_trade'):
                target_farm = float(getattr(config, 'FARM_NOTIONAL_USD', 12.0))
                
                # Kelly-Adjustment auch für Farm-Trades
                if kelly_result.safe_fraction > 0.02:
                    # Kelly empfiehlt größere Position
                    kelly_adjusted = min(
                        kelly_result.recommended_size_usd,
                        target_farm * 1.5  # Max 50% über Basis
                    )
                    final_usd = max(target_farm, kelly_adjusted, min_req)
                else:
                    # Kelly ist konservativ - bleibe bei Basis
                    final_usd = max(target_farm, min_req)
                
                logger.info(
                    f"📊 KELLY SIZE {symbol}: base=${target_farm:.1f}, "
                    f"kelly_recommended=${kelly_result.recommended_size_usd:.1f}, "
                    f"final=${final_usd:.1f}"
                )
            else:
                # Non-Farm: Nutze Kelly direkt
                final_usd = kelly_result.recommended_size_usd
                
                if final_usd < min_req:
                    # Upgrade to min_req if within limits AND we have enough balance
                    can_afford = min_req <= available_capital * 0.9  # Max 90% des Kapitals
                    within_limits = min_req <= config.MAX_TRADE_SIZE_USD and min_req <= max_per_trade
                    
                    if can_afford and within_limits:
                        final_usd = float(min_req)
                        logger.info(f"📊 KELLY SIZE {symbol}: upgraded to min_req ${min_req:.1f}")
                    else:
                        reason = "too expensive" if not can_afford else "exceeds limits"
                        logger.debug(f"Skip {symbol}: Size ${final_usd:.2f} < Min ${min_req:.2f} ({reason})")
                        return False
                else:
                    logger.info(f"📊 KELLY SIZE {symbol}: ${final_usd:.1f} (kelly-optimized)")

            # 5. Final Balance Check
            required_x10 = final_usd * 1.05
            required_lit = final_usd * 1.05

            if bal_x10_real < required_x10 or bal_lit_real < required_lit:
                logger.debug(f"Skip {symbol}: Insufficient balance for ${final_usd:.2f}")
                return False

            # 6. Reserve
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + required_x10
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + required_lit
                reserved_amount = final_usd * 1.05 # Track for release

        except Exception as e:
            logger.error(f"Sizing error {symbol}: {e}")
            return False

        # EXECUTE
        try:
            # Determine sides
            leg1_ex = opp.get('leg1_exchange', 'X10')
            leg1_side = opp.get('leg1_side', 'BUY')
            
            x10_side = leg1_side if leg1_ex == 'X10' else ("SELL" if leg1_side == "BUY" else "BUY")
            lit_side = leg1_side if leg1_ex == 'Lighter' else ("SELL" if leg1_side == "BUY" else "BUY")

            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f} (Min=${min_req:.1f})")

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
                    'account_label': f"Main/Main",  # Placeholder until multi-account fix
                    # NEW: Store APY (normalize if >1 it was percent) and initial net funding rate
                    'entry_apy': (opp.get('apy', 0) / 100.0) if opp.get('apy', 0) > 1 else opp.get('apy', 0),
                    'initial_funding_rate_hourly': opp.get('net_funding_hourly', 0)
                }
                await add_trade_to_state(trade_data)
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

async def get_actual_position_size(adapter, symbol: str) -> Optional[float]:
    """Get actual position size in coins from exchange"""
    try:
        positions = await adapter.fetch_open_positions()
        for p in (positions or []):
            if p.get('symbol') == symbol:
                return p.get('size', 0)
        return None
    except Exception as e:
        logger.error(f"Failed to get position size for {symbol}: {e}")
        return None


async def safe_close_x10_position(x10, symbol, side, notional):
    """Close X10 position using ACTUAL position size"""
    try:
        # Wait for settlement
        await asyncio.sleep(3.0)
        
        # Get ACTUAL position
        actual_size = await get_actual_position_size(x10, symbol)
        
        if actual_size is None or abs(actual_size) < 1e-8:
            logger.info(f"✅ No X10 position to close for {symbol}")
            return
        
        actual_size_abs = abs(actual_size)
        
        # Determine side from position
        if actual_size > 0:
            original_side = "BUY"  # LONG
        else:
            original_side = "SELL"  # SHORT
        
        logger.info(f"🔻 Closing X10 {symbol}: size={actual_size_abs:.6f} coins, side={original_side}")
        
        # Close with ACTUAL coin size
        price = x10.fetch_mark_price(symbol)
        if not price:
            logger.error(f"No price for {symbol}")
            return
        
        notional_actual = actual_size_abs * price
        
        success, _ = await x10.close_live_position(symbol, original_side, notional_actual)
        
        if success:
            logger.info(f"✅ X10 {symbol} closed ({actual_size_abs:.6f} coins)")
        else:
            logger.error(f"❌ X10 close failed for {symbol}")
    
    except Exception as e:
        logger.error(f"X10 close exception: {e}")

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
        try:
            sym = t['symbol']
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Daten-Sanitizing für ALLE numerischen Felder
            # ═══════════════════════════════════════════════════════════════
            
            # 1. Notional sanitizen
            notional = t.get('notional_usd')
            notional = float(notional) if notional is not None else 0.0

            # 2. Initial Funding sanitizen (DAS HAT GEFEHLT!)
            init_funding = t.get('initial_funding_rate_hourly')
            init_funding = float(init_funding) if init_funding is not None else 0.0
            
            # ═══════════════════════════════════════════════════════════════
            
            px = x10.fetch_mark_price(sym)
            pl = lighter.fetch_mark_price(sym)

            if px is None or pl is None:
                continue

            rx = x10.fetch_funding_rate(sym) or 0.0
            rl = lighter.fetch_funding_rate(sym) or 0.0
            
            # Net Calc
            base_net = rl - rx
            current_net = -base_net if t.get('leg1_exchange') == 'X10' else base_net

            # PnL & Duration
            entry_time = t.get('entry_time')
            if isinstance(entry_time, str):
                try: entry_time = datetime.fromisoformat(entry_time)
                except: entry_time = datetime.utcnow()
            elif entry_time is None:
                entry_time = datetime.utcnow()

            hold_hours = (datetime.utcnow() - entry_time).total_seconds() / 3600
            funding_pnl = current_net * hold_hours * notional

            # Spread PnL
            ep_x10 = float(t.get('entry_price_x10') or px) 
            ep_lit = float(t.get('entry_price_lighter') or pl)
            
            entry_spread = abs(ep_x10 - ep_lit)
            curr_spread = abs(px - pl)
            
            if px > 0:
                spread_pnl = (entry_spread - curr_spread) / px * notional
            else:
                spread_pnl = 0.0

            # Fees
            # Extra Safety: Ensure fees are floats (prevent NoneType error)
            fee_x10 = getattr(config, 'TAKER_FEE_X10', 0.0006)
            fee_lit = 0.0  # Lighter hat keine/niedrige Taker Fees
            est_fees = notional * (fee_x10 + fee_lit) * 2.0

            total_pnl = funding_pnl + spread_pnl - est_fees

            # Check Exits
            reason = None
            
            # 1. Farm Check
            if not reason and t.get('is_farm_trade'):
                limit_seconds = getattr(config, 'FARM_HOLD_SECONDS', 60)
                if hold_hours * 3600 > limit_seconds:
                    reason = "FARM_COMPLETE"
                    logger.info(f"🚜 EXIT FARM {sym}: Hold={hold_hours*60:.1f}min > {limit_seconds}s")

            # 2. Volatility Panic
            if not reason:
                try:
                    vol_mon = get_volatility_monitor()
                    vol = 0.0
                    if hasattr(vol_mon, 'get_volatility'):
                        val = vol_mon.get_volatility(sym)
                        if val is not None:
                            vol = float(val)
                    
                    if hasattr(vol_mon, 'should_close_due_to'):
                        if vol_mon.should_close_due_to(vol):
                            reason = "VOLATILITY_PANIC"
                except Exception as e:
                    logger.warning(f"VolCheck Warning {sym}: {e}")

            # 3. Stop Loss / Take Profit
            if not reason:
                if notional > 0:
                    if total_pnl < -notional * 0.03:
                        reason = "STOP_LOSS"
                    elif total_pnl > notional * 0.05:
                        reason = "TAKE_PROFIT"
                
                # Funding Flip Check (Nutze jetzt das sichere init_funding)
                if init_funding * current_net < 0:
                    if not t.get('funding_flip_start_time'):
                        t['funding_flip_start_time'] = datetime.utcnow()
                        if state_manager:
                            await state_manager.update_trade(sym, {'funding_flip_start_time': datetime.utcnow()})
                    else:
                        flip_start = t['funding_flip_start_time']
                        if isinstance(flip_start, str): 
                            try: flip_start = datetime.fromisoformat(flip_start)
                            except: flip_start = datetime.utcnow()
                        
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
                    # Kelly Sizer Trade Recording
                    try:
                        kelly_sizer = get_kelly_sizer()
                        # FIX: Robust APY retrieval with fallback
                        stored_apy = t.get('apy') or t.get('entry_apy')
                        if stored_apy is None:
                            init_rate = t.get('initial_funding_rate_hourly') or 0.0
                            try:
                                init_rate_f = float(init_rate)
                            except Exception:
                                init_rate_f = 0.0
                            # Funding paid 3x daily → annualize; minimal default if missing
                            stored_apy = abs(init_rate_f) * 3 * 365 if init_rate_f else 0.1
                        kelly_sizer.record_trade(
                            symbol=sym,
                            pnl_usd=total_pnl,
                            hold_time_seconds=hold_hours * 3600,
                            entry_apy=stored_apy
                        )
                    except Exception as e:
                        logger.error(f"Kelly Sizer record_trade error for {sym}: {e}")
                    telegram = get_telegram_bot()
                    if telegram.enabled:
                        await telegram.send_trade_alert(sym, reason, notional, total_pnl)

        except Exception as e:
            logger.error(f"Trade Loop Error for {t.get('symbol', 'UNKNOWN')}: {e}")
            import traceback
            logger.debug(traceback.format_exc())

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
            # ✅ NEW: Filter out recent trades (< 30s old) to avoid false positives
            real_ghosts = set()
            for sym in ghosts:
                trade = next((t for t in db_trades if t['symbol'] == sym), None)
                if not trade:
                    continue

                entry_time = trade.get('entry_time')
                if isinstance(entry_time, str):
                    try:
                        entry_time = datetime.fromisoformat(entry_time)
                    except:
                        entry_time = datetime.utcnow()
                elif not isinstance(entry_time, datetime):
                    entry_time = datetime.utcnow()

                age_seconds = (datetime.utcnow() - entry_time).total_seconds()

                # Only treat as ghost if trade is older than 30s
                if age_seconds > 30:
                    real_ghosts.add(sym)
                else:
                    logger.debug(f"👻 {sym} is new trade ({age_seconds:.1f}s old), not a ghost")

            if real_ghosts:
                logger.warning(f"👻 REAL GHOSTS DETECTED: {real_ghosts}")
                for sym in real_ghosts:
                    logger.info(f"Closing ghost {sym} in DB")
                    try:
                        await close_trade_in_state(sym)
                    except Exception as e:
                        logger.error(f"Failed to close ghost {sym} in DB: {e}")

    except Exception as e:
        logger.error(f"Zombie Cleanup Error: {e}")


async def balance_watchdog():
    """KOMPLETT DEAKTIVIERT – X10 Starknet Migration macht Balance-Erkennung unzuverlässig"""
    return  # ← Sofort returnen, keine Logik


async def emergency_position_cleanup(lighter, x10, max_age_hours: float = 48.0, interval_seconds: int = 3600):
    """Emergency background task to perform periodic zombie/ghost cleanup.

    This is a defensive fallback if no external emergency cleanup routine
    is provided elsewhere in the codebase. It simply runs
    `cleanup_zombie_positions` on an interval and logs any exceptions.
    """
    logger.info(f"Starting emergency_position_cleanup (interval={interval_seconds}s, max_age_hours={max_age_hours})")
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

async def farm_loop(lighter, x10, parallel_exec):
    """Volume Farming"""
    # Rate limiter state (local to function to avoid global mutation)
    last_trades: List[float] = []

    # Keep this coroutine alive even when farming is disabled.
    # Return would terminate the background task and leave the bot without the farm loop.
    while True:
        if SHUTDOWN_FLAG:
            logger.info("Farm loop: SHUTDOWN_FLAG detected, exiting")
            break
        try:
            # If farming disabled, sleep and continue — don't exit the task
            if not config.VOLUME_FARM_MODE:
                await asyncio.sleep(60)
                continue

            # Announce when farm mode is active (logged each loop iteration when active)
            logger.info("🚜 Farm Mode ACTIVE")
            if SHUTDOWN_FLAG:
                break
            
            # Clean rate limiter history
            now = time.time()
            last_trades = [t for t in last_trades if now - t < 60]
            
            # Check burst limit (safe default)
            burst_limit = getattr(config, 'FARM_BURST_LIMIT', 10)
            if len(last_trades) >= burst_limit:
                oldest = last_trades[0]
                wait_time = 60 - (now - oldest)
                logger.info(f"🚜 Rate limited: wait {wait_time:.1f}s")
                await asyncio.sleep(max(wait_time, 5))
                continue
            
            # Standard farm logic
            trades = await get_open_trades()
            farm_count = sum(1 for t in trades if t.get('is_farm_trade'))

            # Check concurrency limit
            if farm_count >= getattr(config, 'FARM_MAX_CONCURRENT', 3):
                await asyncio.sleep(10)
                continue

            # Check min interval since last trade
            min_interval = getattr(config, 'FARM_MIN_INTERVAL_SECONDS', 15)
            if last_trades:
                last_trade = last_trades[-1]
                time_since = now - last_trade
                if time_since < min_interval:
                    await asyncio.sleep(max(0.0, min_interval - time_since))

            # Get balance
            bal_x10 = await x10.get_real_available_balance()
            bal_lit = await lighter.get_real_available_balance()
            
            if bal_x10 < getattr(config, 'FARM_NOTIONAL_USD', 0) or bal_lit < getattr(config, 'FARM_NOTIONAL_USD', 0):
                logger.debug(f"🚜 Farm paused: Low balance X10=${bal_x10:.0f} Lit=${bal_lit:.0f}")
                await asyncio.sleep(30)
                continue

            # Find farm opportunity
            common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
            open_syms = {t['symbol'] for t in trades}
            
            for sym in common:
                # Prevent spam / re-triggers: skip if open, already executing, or recently failed
                if sym in open_syms or sym in ACTIVE_TASKS or sym in FAILED_COINS:
                    continue
                
                px = x10.fetch_mark_price(sym)
                pl = lighter.fetch_mark_price(sym)
                
                if not px or not pl:
                    continue
                
                spread = abs(px - pl) / px if px else 1.0
                if spread > getattr(config, 'FARM_MAX_SPREAD_PCT', 0.01):
                    continue
                
                vol_24h = abs(x10.get_24h_change_pct(sym) or 0)
                if vol_24h > getattr(config, 'FARM_MAX_VOLATILITY_24H', 0.05):
                    continue
                
                rl = lighter.fetch_funding_rate(sym) or 0
                rx = x10.fetch_funding_rate(sym) or 0
                apy = abs(rl - rx) * 24 * 365
                
                if apy < getattr(config, 'FARM_MIN_APY', 0.01):
                    continue
                
                # CRITICAL: Mark as farm trade
                opp = {
                    'symbol': sym,
                    'apy': apy * 100,
                    'net_funding_hourly': rl - rx,
                    'leg1_exchange': 'Lighter' if rl > rx else 'X10',
                    'leg1_side': 'SELL' if rl > rx else 'BUY',
                    'is_farm_trade': True,
                    'spread_pct': spread
                }
                
                logger.info(f"🚜 Opening FARM: {sym} APY={apy*100:.1f}%")
                
                ACTIVE_TASKS[sym] = asyncio.create_task(
                    execute_trade_task(opp, lighter, x10, parallel_exec)
                )
                
                # Record this trade attempt in local rate limiter
                last_trades.append(now)

                await asyncio.sleep(10)  # Wait longer between farm trades
                break
            
            await asyncio.sleep(30)  # Longer idle time between scans
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Farm Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(30)  # Long cooldown on error

async def cleanup_finished_tasks():
    """Background task: Remove completed tasks from ACTIVE_TASKS"""
    while not SHUTDOWN_FLAG:
        try:
            async with TASKS_LOCK:
                finished = [sym for sym, task in ACTIVE_TASKS.items() if task.done()]
                for sym in finished:
                    try:
                        del ACTIVE_TASKS[sym]
                    except KeyError:
                        pass
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"cleanup_finished_tasks error: {e}")

    async def emergency_position_cleanup(lighter, x10, max_age_hours: float = 48.0):
        """
        Emergency cleanup für stuck trades die länger als max_age_hours offen sind.
        Wird alle 10 Minuten aufgerufen.
        """
        while not SHUTDOWN_FLAG:
            try:
                await asyncio.sleep(600)  # 10 min
            
                if SHUTDOWN_FLAG:
                    break
                
                open_trades = await get_open_trades()
                now = datetime.now()
            
                for trade in open_trades:
                    entry_time = trade.get('entry_time')
                    if not entry_time:
                        continue
                    
                    if isinstance(entry_time, str):
                        entry_time = datetime.fromisoformat(entry_time)
                
                    age_hours = (now - entry_time).total_seconds() / 3600
                
                    if age_hours > max_age_hours:
                        symbol = trade['symbol']
                        logger.warning(
                            f"🚨 EMERGENCY CLOSE: {symbol} open for {age_hours:.1f}h (max={max_age_hours}h)"
                        )
                    
                        # Force close via position_manager
                        from src.position_manager import close_position_with_reason
                        await close_position_with_reason(
                            symbol, 
                            "EMERGENCY_TIMEOUT", 
                            lighter, 
                            x10
                        )
                        await asyncio.sleep(2)
                    
            except asyncio.CancelledError:
                logger.info("Emergency cleanup cancelled")
                break
            except Exception as e:
                logger.error(f"Emergency cleanup error: {e}")
                await asyncio.sleep(60)

async def close_all_open_positions_on_start(lighter, x10):
    """
    EMERGENCY: Close all open positions on bot start.
    Use this if bot is stuck with 20 open trades blocking capital.
    """
    logger.warning("🚨 EMERGENCY: Closing ALL open positions...")
    
    open_trades = await get_open_trades()
    
    if not open_trades:
        logger.info("✓ No open positions to close")
        return
    
    logger.info(f"⚠️  Found {len(open_trades)} open positions, closing...")
    
    for trade in open_trades:
        symbol = trade['symbol']
        try:
            from src.position_manager import close_position_with_reason
            success = await close_position_with_reason(
                symbol,
                "EMERGENCY_CLEANUP_ON_START",
                lighter,
                x10
            )
            if success:
                logger.info(f"✓ Closed {symbol}")
            else:
                logger.error(f"✗ Failed to close {symbol}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"✗ Error closing {symbol}: {e}")
    
    logger.info("✓ Emergency cleanup complete")

def get_symbol_lock(symbol: str) -> asyncio.Lock:
    """Thread-safe / async-safe Lock pro Symbol"""
    if symbol not in SYMBOL_LOCKS:
        SYMBOL_LOCKS[symbol] = asyncio.Lock()
    return SYMBOL_LOCKS[symbol]


async def reconcile_db_with_exchanges(lighter, x10):
    """
    CRITICAL: Reconcile database state with actual exchange positions.
    
    Emergency Sync Strategy:
    1. Retry position fetches (exchanges may return empty on transient errors)
    2. Only mark as ghost if BOTH exchanges confirm no position after retries
    3. Mark ghost trades as CLOSED in DB to free up slots
    
    This prevents false positives from API glitches.
    """
    logger.info("🔍 STATE RECONCILIATION: Checking DB vs Exchange...")
    
    # Get DB trades
    open_trades = await get_open_trades()
    if not open_trades:
        logger.info("✓ No DB trades to reconcile")
        return
    
    logger.info(f"📊 Reconciling {len(open_trades)} DB trades with exchanges")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # ROBUST POSITION FETCHING WITH RETRIES
    # Exchanges sometimes return empty lists on transient errors - retry to confirm
    # ═══════════════════════════════════════════════════════════════════════════════
    max_retries = 3
    x10_positions = None
    lighter_positions = None
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"Fetching positions (attempt {attempt + 1}/{max_retries})...")
            
            # Fetch in parallel for speed
            x10_task = asyncio.create_task(x10.fetch_open_positions())
            lighter_task = asyncio.create_task(lighter.fetch_open_positions())
            
            x10_positions, lighter_positions = await asyncio.gather(
                x10_task, lighter_task, return_exceptions=False
            )
            
            # Validate results (ensure they're lists, not exceptions)
            if not isinstance(x10_positions, list):
                x10_positions = []
            if not isinstance(lighter_positions, list):
                lighter_positions = []
            
            logger.info(
                f"✅ Fetched positions: X10={len(x10_positions)}, "
                f"Lighter={len(lighter_positions)}"
            )
            break
            
        except Exception as e:
            logger.warning(f"⚠️ Position fetch attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2.0 * (attempt + 1))  # Exponential backoff
                continue
            else:
                logger.error("❌ Failed to fetch positions after retries - ABORTING reconciliation")
                return
    
    # Build symbol sets from actual positions
    x10_symbols = {p.get('symbol') for p in (x10_positions or []) if p.get('symbol')}
    lighter_symbols = {p.get('symbol') for p in (lighter_positions or []) if p.get('symbol')}
    
    logger.debug(f"X10 positions: {sorted(x10_symbols)}")
    logger.debug(f"Lighter positions: {sorted(lighter_symbols)}")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # EMERGENCY SYNC: DETECT AND FIX GHOST TRADES
    # ═══════════════════════════════════════════════════════════════════════════════
    ghost_count = 0
    partial_ghost_count = 0
    
    for trade in open_trades:
        symbol = trade['symbol']
        
        # Check if position exists on exchanges
        on_x10 = symbol in x10_symbols
        on_lighter = symbol in lighter_symbols
        
        # CASE 1: Total Ghost - Not on either exchange
        if not on_x10 and not on_lighter:
            logger.warning(
                f"👻 GHOST TRADE DETECTED: {symbol} in DB but NOT on ANY exchange"
            )
            logger.warning(f"   DB entry: {trade.get('entry_time')} | Expected on both X10 and Lighter")
            
            # Emergency sync: Mark as CLOSED in DB
            try:
                await close_trade_in_state(symbol)
                logger.info(f"🧹 EMERGENCY SYNC: Marked {symbol} as CLOSED in DB")
                ghost_count += 1
            except Exception as e:
                logger.error(f"❌ Failed to close ghost trade {symbol}: {e}")
                # Fallback to state_manager
                try:
                    if state_manager:
                        await state_manager.close_trade(symbol)
                        logger.info(f"🧹 EMERGENCY SYNC (fallback): Closed {symbol}")
                        ghost_count += 1
                except Exception as e2:
                    logger.error(f"❌ Fallback also failed for {symbol}: {e2}")
            
            await asyncio.sleep(0.1)
        
        # CASE 2: Partial Ghost - Only on one exchange (CRITICAL MISMATCH!)
        elif on_x10 != on_lighter:
            if on_x10 and not on_lighter:
                logger.error(
                    f"🚨 PARTIAL GHOST: {symbol} exists on X10 but NOT on Lighter!"
                )
            else:
                logger.error(
                    f"🚨 PARTIAL GHOST: {symbol} exists on Lighter but NOT on X10!"
                )
            logger.error(f"   This indicates a failed hedge or incomplete execution.")
            partial_ghost_count += 1
            # Don't auto-close partial ghosts - they need manual intervention
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════════
    if ghost_count > 0:
        logger.warning(f"🧹 EMERGENCY SYNC: Cleaned {ghost_count} ghost trades from DB")
    
    if partial_ghost_count > 0:
        logger.error(
            f"🚨 CRITICAL: {partial_ghost_count} partial ghost trades detected - "
            f"manual intervention required!"
        )
    
    if ghost_count == 0 and partial_ghost_count == 0:
        logger.info("✅ All DB trades match exchange positions")
    
    # Re-check after cleanup
    open_trades_after = await get_open_trades()
    logger.info(
        f"📊 Reconciliation complete: {len(open_trades)} → {len(open_trades_after)} trades"
    )


async def logic_loop(lighter, x10, price_event, parallel_exec):
    """Main trading loop with opportunity detection and execution"""
    REFRESH_DELAY = getattr(config, 'REFRESH_DELAY_SECONDS', 5)
    logger.info(f"Logic Loop gestartet – REFRESH alle {REFRESH_DELAY}s")

    while not SHUTDOWN_FLAG:
        try:
            open_trades = await get_open_trades()
            open_syms = {t['symbol'] for t in open_trades}
            
            max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
            if len(open_trades) >= max_trades:
                logger.debug(f"MAX_OPEN_TRADES ({max_trades}) reached, waiting...")
                await asyncio.sleep(REFRESH_DELAY)
                continue

            opportunities = await find_opportunities(lighter, x10, open_syms, is_farm_mode=None)
            
            if opportunities:
                logger.info(f"🎯 Found {len(opportunities)} opportunities, executing best...")
                
                executed_count = 0
                max_per_cycle = 3
                
                # ═══════════════════════════════════════════════════════════════
                # Balance Check mit robuster Exception Handling
                # ═══════════════════════════════════════════════════════════════
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

                logger.info(f"X10: Detected balance = ${bal_x10:.2f}")

                # KRITISCH: Bot NICHT stoppen wenn Balance-Erkennung fehlschlägt!
                # Stattdessen: Warning + weiter versuchen
                if bal_x10 < 0.01:
                    logger.warning(
                        "⚠️ X10 Balance = $0 (API-Problem, NICHT echte Balance!) "
                        "→ Bot läuft weiter, Balance-Check wird übersprungen"
                    )
                    # Setze einen Fallback-Wert basierend auf historischen Daten oder Skip
                    bal_x10 = 50.0  # Annahme: Es gibt Balance, API kann sie nur nicht lesen
                    
                elif bal_x10 < 5.0:
                    logger.warning(
                        f"⚠️ X10 Balance niedrig (${bal_x10:.2f}) aber Bot läuft weiter. "
                        "Neue Trades werden pausiert bis Balance steigt."
                    )
                    # KEIN return! Bot läuft weiter, aber keine neuen Trades

                open_trades_count = len(open_trades)
                
                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Only reserve capital for ACTUAL trades
                # Not for ghost DB entries
                # ═══════════════════════════════════════════════════════════════
                avg_trade_size = getattr(config, 'DESIRED_NOTIONAL_USD', 12)
                
                # Verify trades actually exist on exchanges
                try:
                    x10_positions = await x10.fetch_open_positions()
                    lighter_positions = await lighter.fetch_open_positions()
                    actual_open_count = len(set(
                        [p.get('symbol') for p in (x10_positions or [])] +
                        [p.get('symbol') for p in (lighter_positions or [])]
                    ))
                except:
                    actual_open_count = open_trades_count  # Fallback
                
                # Use ACTUAL open count, not DB count
                locked_per_exchange = actual_open_count * avg_trade_size * 0.6
                
                async with IN_FLIGHT_LOCK:
                    available_x10 = bal_x10 - IN_FLIGHT_MARGIN.get('X10', 0) - locked_per_exchange
                    available_lit = bal_lit - IN_FLIGHT_MARGIN.get('Lighter', 0) - locked_per_exchange
                
                # Safety buffer
                available_x10 = max(0, available_x10 - 3.0)
                available_lit = max(0, available_lit - 3.0)
                
                logger.info(
                    f"💰 Balance: X10=${bal_x10:.1f} (free=${available_x10:.1f}), "
                    f"Lit=${bal_lit:.1f} (free=${available_lit:.1f}) | "
                    f"DB_Trades={open_trades_count}, Real_Positions={actual_open_count}, "
                    f"Locked=${locked_per_exchange:.0f}/ex"
                )
                
                if available_x10 < avg_trade_size or available_lit < avg_trade_size:
                    if available_x10 < 0 or available_lit < 0:
                        logger.warning(
                            f"⚠️  NEGATIVE available balance detected! "
                            f"This indicates ghost trades or stale DB entries."
                        )
                    logger.debug(
                        f"⏸️  Paused: Insufficient available balance "
                        f"(need ${avg_trade_size:.0f}, have X10=${available_x10:.1f}, Lit=${available_lit:.1f})"
                    )
                    await asyncio.sleep(REFRESH_DELAY * 3)
                    continue
                
                for opp in opportunities:
                    if executed_count >= max_per_cycle:
                        break
                        
                    symbol = opp['symbol']
                    
                    if symbol in ACTIVE_TASKS:
                        continue
                    
                    if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 180):
                        continue
                    
                    if symbol in open_syms:
                        continue
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Check balance BEFORE launching task — use opportunity's
                    # recommended notional when available, otherwise fall back
                    # to farm/desired config defaults.
                    is_farm = opp.get('is_farm_trade', False)
                    base_default = getattr(config, 'FARM_NOTIONAL_USD', 12) if is_farm else getattr(config, 'DESIRED_NOTIONAL_USD', 16)

                    # Prefer an explicit size provided by the opportunity payload
                    trade_size = None
                    for key in ('trade_size', 'notional_usd', 'trade_notional_usd', 'desired_notional_usd'):
                        if key in opp and isinstance(opp[key], (int, float)):
                            trade_size = float(opp[key])
                            break

                    if trade_size is None:
                        trade_size = float(base_default)

                    required_margin = trade_size * 1.2  # 20% buffer
                    
                    if available_x10 < required_margin or available_lit < required_margin:
                        logger.debug(f"Skip {symbol}: Insufficient pre-check balance (X10=${available_x10:.1f}, Lit=${available_lit:.1f}, Need=${required_margin:.1f})")
                        continue
                    
                    # Deduct from available balance for next iteration
                    available_x10 -= required_margin
                    available_lit -= required_margin
                    
                    logger.info(f"🚀 Launching trade for {symbol} (APY={opp['apy']:.1f}%)")

                    # ═══════════════════════════════════════════════════════════════════════════════
                    # CRITICAL: Symbol-Level Lock verhindert doppelte Trades
                    # ═══════════════════════════════════════════════════════════════════════════════
                    async with TASKS_LOCK:
                        if symbol in ACTIVE_TASKS:
                            logger.debug(f"⏸️  {symbol} already has active task, skipping")
                            continue

                    lock = get_symbol_lock(symbol)

                    async def _handle_with_lock():
                        async with lock:
                            try:
                                # Directly perform the execution routine for the opportunity
                                await execute_trade_parallel(opp, lighter, x10, parallel_exec)
                            finally:
                                async with TASKS_LOCK:
                                    ACTIVE_TASKS.pop(symbol, None)

                    # CRITICAL: Don't start new tasks during shutdown
                    if SHUTDOWN_FLAG:
                        logger.debug(f"Shutdown active, skipping task for {symbol}")
                        continue

                    task = asyncio.create_task(_handle_with_lock())
                    async with TASKS_LOCK:
                        if SHUTDOWN_FLAG:
                            task.cancel()
                            continue
                        ACTIVE_TASKS[symbol] = task

                    executed_count += 1
                    
                    await asyncio.sleep(0.5)
                
                if executed_count > 0:
                    logger.info(f"✅ Launched {executed_count} trade tasks this cycle")
            
            # CRITICAL: Check SHUTDOWN_FLAG before sleeping
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

async def trade_management_loop(lighter, x10):
    """Überwacht offene Trades und schließt sie nach Kriterien (Farm-Timer, TP/SL, etc.)"""
    logger.info("🛡️ Trade Management Loop gestartet")
    while not SHUTDOWN_FLAG:
        try:
            # Ruft die Logik auf, die du bereits hast, aber die nie lief
            await manage_open_trades(lighter, x10)
            await asyncio.sleep(1)  # Jede Sekunde prüfen
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Trade Management Error: {e}")
            await asyncio.sleep(5)

async def maintenance_loop(lighter, x10):
    """Background tasks: Funding rates refresh + neue Coins erkennen"""
    last_market_refresh = 0
    market_refresh_interval = 300  # Alle 5 Minuten neue Coins checken
    
    while True:
        try:
            now = time.time()
            
            # === NEUE COINS ERKENNEN ===
            if now - last_market_refresh > market_refresh_interval:
                old_lighter_count = len(lighter.market_info)
                old_x10_count = len(x10.market_info)
                
                await lighter.load_market_cache(force=True)
                await x10.load_market_cache(force=True)
                
                new_lighter = len(lighter.market_info) - old_lighter_count
                new_x10 = len(x10.market_info) - old_x10_count
                
                if new_lighter > 0 or new_x10 > 0:
                    logger.info(f"🆕 New markets detected: Lighter +{new_lighter}, X10 +{new_x10}")
                
                last_market_refresh = now
            
            # === FUNDING RATES REFRESH ===
            await lighter.load_funding_rates_and_prices()
            await x10.load_market_cache(force=True)
            
            # --- Update predictor with fresh funding/OB/OI data ---
            try:
                predictor = get_predictor()
                common = set(getattr(lighter, 'market_info', {}).keys()) & set(getattr(x10, 'market_info', {}).keys())
                
                for symbol in list(common)[:50]:  # Limit to 50 symbols per cycle
                    try:
                        r_l = lighter.fetch_funding_rate(symbol) or 0.0
                        r_x = x10.fetch_funding_rate(symbol) or 0.0
                        current_rate = (float(r_l) + float(r_x)) / 2.0

                        # OI
                        oi = 0.0
                        try:
                            oi = await x10.fetch_open_interest(symbol)
                        except Exception:
                            pass
                        if oi == 0.0:
                            try:
                                oi = await lighter.fetch_open_interest(symbol)
                            except Exception:
                                pass

                        predictor._update_history(predictor.rate_history, symbol, float(current_rate), now)
                        predictor.update_oi_velocity(symbol, float(oi))
                        
                    except Exception:
                        continue
            except Exception:
                pass
            
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")
            await asyncio.sleep(30)

async def setup_database():
    """Initialize async database"""
    db = await get_database()
    logger.info("✅ Async database initialized")
#
async def migrate_database():
    """Migrate DB schema for new columns"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            # FIRST: Create trade_history table if it doesn't exist
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    hold_duration_hours REAL,
                    close_reason TEXT,
                    final_pnl_usd REAL,
                    funding_pnl_usd REAL,
                    spread_pnl_usd REAL,
                    fees_usd REAL,
                    account_label TEXT DEFAULT 'Main'
                )
            """)
            await conn.commit()
            
            # Check if migration needed for existing table
            cursor = await conn.execute("PRAGMA table_info(trade_history)")
            columns = await cursor.fetchall()
            has_account_label = any(col[1] == 'account_label' for col in columns)
            
            if not has_account_label:
                logger.info("🔄 Migrating database schema...")
                try:
                    await conn.execute("ALTER TABLE trade_history ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass  # Column might already exist
                try:
                    await conn.execute("ALTER TABLE trades ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass  # Column might already exist
                await conn.commit()
                logger.info("✅ Database migration complete")
            else:
                logger.debug("✅ Database schema up to date")
                
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        # DON'T delete the database - just continue
        logger.warning("⚠️ Continuing without migration...")

# ═══════════════════════════════════════════════════════════════════════════════
# PUNKT 2: NON-BLOCKING MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
# Umbau auf asyncio Task-Management mit:
# - Structured Concurrency (TaskGroup-ähnlich)
# - Graceful Shutdown mit Signal Handlers
# - Task Supervision & Auto-Restart
# - Health Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

class TaskState(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ManagedTask:
    """Tracks state of a managed background task"""
    name: str
    coro_factory: Callable[[], Awaitable[Any]]
    task: Optional[asyncio.Task] = None
    state: TaskState = TaskState.PENDING
    restart_count: int = 0
    max_restarts: int = 5
    restart_delay: float = 5.0
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    critical: bool = True  # If True, shutdown bot on permanent failure


class TaskSupervisor:
    """
    Manages async tasks with supervision, restart, and graceful shutdown. 
    
    Features:
    - Auto-restart failed tasks with exponential backoff
    - Graceful shutdown on SIGINT/SIGTERM
    - Health monitoring
    - Task isolation (one crash doesn't kill others)
    """
    
    def __init__(self):
        self.tasks: Dict[str, ManagedTask] = {}
        self.shutdown_event = asyncio.Event()
        self._shutdown_timeout = 30.0
        self._health_check_interval = 10.0
        self._signal_handlers_installed = False
        self._supervisor_task: Optional[asyncio.Task] = None
        
    def register(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable[Any]],
        critical: bool = True,
        max_restarts: int = 5,
        restart_delay: float = 5.0
    ):
        """Register a task to be supervised"""
        self.tasks[name] = ManagedTask(
            name=name,
            coro_factory=coro_factory,
            critical=critical,
            max_restarts=max_restarts,
            restart_delay=restart_delay
        )
        logger.info(f"📝 Registered task: {name} (critical={critical})")
        
    def _install_signal_handlers(self):
        """Install signal handlers for graceful shutdown"""
        if self._signal_handlers_installed:
            return
            
        loop = asyncio.get_running_loop()
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._signal_handler(s))
                )
                logger.debug(f"Installed handler for {sig.name}")
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: asyncio.create_task(self._signal_handler(s)))
                
        self._signal_handlers_installed = True
        logger.info("✅ Signal handlers installed (SIGINT, SIGTERM)")
        
    async def _signal_handler(self, sig):
        """Handle shutdown signal"""
        sig_name = signal.Signals(sig).name if isinstance(sig, int) else str(sig)
        logger.warning(f"🛑 Received {sig_name}, initiating graceful shutdown...")
        self.shutdown_event.set()
        
    async def start_all(self):
        """Start all registered tasks"""
        self._install_signal_handlers()
        
        for name, managed in self.tasks.items():
            await self._start_task(managed)
            
        # Start supervisor loop
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(),
            name="supervisor"
        )
        
        logger.info(f"🚀 Started {len(self.tasks)} tasks")
        
    async def _start_task(self, managed: ManagedTask):
        """Start a single managed task"""
        try:
            managed.task = asyncio.create_task(
                self._task_wrapper(managed),
                name=managed.name
            )
            managed.state = TaskState.RUNNING
            managed.started_at = time.monotonic()
            logger.info(f"▶️  Started task: {managed.name}")
        except Exception as e:
            managed.state = TaskState.FAILED
            managed.last_error = str(e)
            logger.error(f"❌ Failed to start task {managed.name}: {e}")
            
    async def _task_wrapper(self, managed: ManagedTask):
        """Wrapper that catches exceptions and enables restart"""
        try:
            await managed.coro_factory()
        except asyncio.CancelledError:
            managed.state = TaskState.CANCELLED
            logger.info(f"🛑 Task {managed.name} cancelled")
            raise
        except Exception as e:
            managed.state = TaskState.FAILED
            managed.last_error = str(e)
            logger.error(f"❌ Task {managed.name} crashed: {e}", exc_info=True)
            raise
            
    async def _supervisor_loop(self):
        """Monitor tasks and restart failed ones"""
        logger.info("🔍 Supervisor loop started")
        
        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(self._health_check_interval)
                
                for name, managed in self.tasks.items():
                    if managed.task is None:
                        continue
                        
                    if managed.task.done():
                        # Task finished or crashed
                        if managed.state == TaskState.CANCELLED:
                            continue
                            
                        # Check for exception
                        try:
                            exc = managed.task.exception()
                            if exc:
                                managed.last_error = str(exc)
                        except asyncio.CancelledError:
                            continue
                            
                        # Attempt restart
                        if managed.restart_count < managed.max_restarts:
                            managed.restart_count += 1
                            managed.state = TaskState.RESTARTING
                            
                            delay = managed.restart_delay * (2 ** (managed.restart_count - 1))
                            delay = min(delay, 60.0)  # Cap at 60s
                            
                            logger.warning(
                                f"🔄 Restarting {name} in {delay:.1f}s "
                                f"(attempt {managed.restart_count}/{managed.max_restarts})"
                            )
                            
                            await asyncio.sleep(delay)
                            
                            if not self.shutdown_event.is_set():
                                await self._start_task(managed)
                        else:
                            managed.state = TaskState.FAILED
                            logger.error(
                                f"❌ Task {name} exceeded max restarts ({managed.max_restarts})"
                            )
                            
                            if managed.critical:
                                logger.critical(f"💀 Critical task {name} failed, initiating shutdown")
                                self.shutdown_event.set()
                                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervisor error: {e}")
                
        logger.info("🔍 Supervisor loop stopped")
        
    async def wait_for_shutdown(self):
        """Wait for shutdown signal"""
        await self.shutdown_event.wait()
        
    async def shutdown(self):
        """Gracefully shutdown all tasks"""
        logger.info("🛑 Initiating graceful shutdown...")
        self.shutdown_event.set()
        
        # Cancel supervisor first
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
                
        # Cancel all managed tasks
        tasks_to_cancel = []
        for name, managed in self.tasks.items():
            if managed.task and not managed.task.done():
                managed.task.cancel()
                tasks_to_cancel.append(managed.task)
                logger.info(f"🛑 Cancelling {name}...")
                
        if tasks_to_cancel:
            # Wait for all tasks with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=self._shutdown_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Shutdown timeout ({self._shutdown_timeout}s) exceeded")
                
        # Update states
        for managed in self.tasks.values():
            if managed.state == TaskState.RUNNING:
                managed.state = TaskState.STOPPED
                
        logger.info("✅ All tasks stopped")
        
    def get_health_report(self) -> Dict[str, Any]:
        """Get health status of all tasks"""
        report = {
            "shutdown_requested": self.shutdown_event.is_set(),
            "tasks": {}
        }
        
        for name, managed in self.tasks.items():
            task_info = {
                "state": managed.state.value,
                "restart_count": managed.restart_count,
                "critical": managed.critical,
            }
            
            if managed.started_at:
                task_info["uptime_seconds"] = time.monotonic() - managed.started_at
                
            if managed.last_error:
                task_info["last_error"] = managed.last_error
                
            if managed.task:
                task_info["done"] = managed.task.done()
                
            report["tasks"][name] = task_info
            
        return report


# ═══════════════════════════════════════════════════════════════════════════════
# OI DIAGNOSTIC FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

async def diagnose_oi_tracking(lighter, x10, oi_tracker):
    """
    DIAGNOSTIC: Verifiziert dass OI Tracking funktioniert. 
    Ruft OI von beiden Exchanges ab und vergleicht mit Tracker Cache.
    """
    logger.info("=" * 60)
    logger.info("🔬 OI DIAGNOSTIC TEST STARTING...")
    logger.info("=" * 60)
    
    test_symbols = ["BTC-USD", "ETH-USD", "SOL-USD", "APT-USD", "BERA-USD"]
    
    results = []
    
    for symbol in test_symbols:
        result = {
            "symbol": symbol,
            "x10_oi": None,
            "lighter_oi": None,
            "tracker_oi": None,
            "status": "❓"
        }
        
        # 1. Direct X10 OI Fetch
        try:
            oi_x10 = await x10.fetch_open_interest(symbol)
            result["x10_oi"] = oi_x10
        except Exception as e:
            result["x10_oi"] = f"ERROR: {e}"
        
        await asyncio.sleep(0.5)  # Rate limit protection
        
        # 2. Direct Lighter OI Fetch
        try:
            oi_lighter = await lighter.fetch_open_interest(symbol)
            result["lighter_oi"] = oi_lighter
        except Exception as e:
            result["lighter_oi"] = f"ERROR: {e}"
        
        await asyncio.sleep(0.5)
        
        # 3. OI Tracker Cache
        if oi_tracker:
            try:
                if hasattr(oi_tracker, '_oi_cache'):
                    result["tracker_oi"] = oi_tracker._oi_cache.get(symbol, "NOT IN CACHE")
                elif hasattr(oi_tracker, 'get_oi'):
                    result["tracker_oi"] = oi_tracker.get_oi(symbol)
                else:
                    result["tracker_oi"] = "NO METHOD"
            except Exception as e:
                result["tracker_oi"] = f"ERROR: {e}"
        else:
            result["tracker_oi"] = "TRACKER NOT INITIALIZED"
        
        # Determine status
        x10_ok = isinstance(result["x10_oi"], (int, float)) and result["x10_oi"] > 0
        lit_ok = isinstance(result["lighter_oi"], (int, float)) and result["lighter_oi"] > 0
        
        if x10_ok and lit_ok:
            result["status"] = "✅ BOTH"
        elif x10_ok:
            result["status"] = "⚠️ X10 ONLY"
        elif lit_ok:
            result["status"] = "⚠️ LIGHTER ONLY"
        else:
            result["status"] = "❌ NONE"
        
        results.append(result)
    
    # Print Results
    logger.info("")
    logger.info("📊 OI DIAGNOSTIC RESULTS:")
    logger.info("-" * 80)
    logger.info(f"{'Symbol':<15} {'X10 OI':>15} {'Lighter OI':>15} {'Tracker':>15} {'Status':<10}")
    logger.info("-" * 80)
    
    for r in results:
        x10_str = f"${r['x10_oi']:,.0f}" if isinstance(r['x10_oi'], (int, float)) else str(r['x10_oi'])[:12]
        lit_str = f"${r['lighter_oi']:,.0f}" if isinstance(r['lighter_oi'], (int, float)) else str(r['lighter_oi'])[:12]
        trk_str = f"${r['tracker_oi']:,.0f}" if isinstance(r['tracker_oi'], (int, float)) else str(r['tracker_oi'])[:12]
        
        logger.info(f"{r['symbol']:<15} {x10_str:>15} {lit_str:>15} {trk_str:>15} {r['status']:<10}")
    
    logger.info("-" * 80)
    
    # Summary
    working = sum(1 for r in results if "✅" in r["status"])
    partial = sum(1 for r in results if "⚠️" in r["status"])
    failed = sum(1 for r in results if "❌" in r["status"])
    
    logger.info(f"📈 SUMMARY: {working} working, {partial} partial, {failed} failed")
    
    # Check OI Tracker internals
    if oi_tracker:
        logger.info("")
        logger.info("🔧 OI TRACKER INTERNALS:")
        logger.info(f"   - _running: {getattr(oi_tracker, '_running', 'N/A')}")
        logger.info(f"   - _symbols count: {len(getattr(oi_tracker, '_symbols', []))}")
        logger.info(f"   - _oi_cache size: {len(getattr(oi_tracker, '_oi_cache', {}))}")
        
        # Show top 5 cached OI values
        if hasattr(oi_tracker, '_oi_cache') and oi_tracker._oi_cache:
            top5 = sorted(oi_tracker._oi_cache.items(), key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0, reverse=True)[:5]
            logger.info("   - Top 5 cached OI:")
            for sym, oi in top5:
                logger.info(f"      {sym}: ${oi:,.0f}" if isinstance(oi, (int, float)) else f"      {sym}: {oi}")
    
    logger.info("=" * 60)
    logger.info("🔬 OI DIAGNOSTIC TEST COMPLETE")
    logger.info("=" * 60)
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT RUNNER V5 (Task Supervised)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_bot_v5():
    """
    Main bot entry point with full task supervision.
    
    This replaces the old run_bot_v4() with proper:
    - Task supervision & auto-restart
    - Signal handling (SIGINT/SIGTERM)
    - Graceful shutdown
    - Health monitoring
    """
    global SHUTDOWN_FLAG, state_manager, telegram_bot
    
    logger.info("🔥 BOT V5 (Task Supervised) STARTING...")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 1. INIT INFRASTRUCTURE
    # ═══════════════════════════════════════════════════════════════════════════
    state_manager = await get_state_manager()
    logger.info("✅ State Manager started")
    
    telegram_bot = get_telegram_bot()
    if telegram_bot.enabled:
        await telegram_bot.start()
        logger.info("📱 Telegram Bot connected")
        
    await setup_database()
    await migrate_database()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 2. INIT ADAPTERS
    # ═══════════════════════════════════════════════════════════════════════════
    x10 = X10Adapter()
    lighter = LighterAdapter()
    
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Load Market Data FIRST (CRITICAL - Required for WebSocket subscriptions)
    # ═══════════════════════════════════════════════════════════════════════════
    logger.info("📊 Loading Market Data...")
    oi_tracker = None
    try:
        # ═══════════════════════════════════════════════════════════════
        # SEQUENTIAL LOADING mit Delays (verhindert 429 Rate Limits)
        # ═══════════════════════════════════════════════════════════════

        # Step 1: X10 Markets (schneller, weniger Rate-Limit-sensitiv)
        logger.info("📊 Step 1/5: Loading X10 markets...")
        await x10.load_market_cache(force=True)
        x10_count = len(x10.market_info)
        logger.info(f"✅ X10: {x10_count} markets loaded")

        await asyncio.sleep(2.0)  # Kurze Pause

        # Step 2: Lighter Markets (Rate-Limit-sensitiv!)
        logger.info("📊 Step 2/5: Loading Lighter markets...")
        await lighter.load_market_cache(force=True)
        lit_count = len(lighter.market_info)
        logger.info(f"✅ Lighter: {lit_count} markets loaded")

        await asyncio.sleep(3.0)  # Längere Pause für Lighter

        # Step 3: Lighter Funding Rates
        logger.info("📊 Step 3/5: Loading Lighter funding rates...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for fr in data.get("funding_rates", []):
                            if fr.get("exchange") == "lighter":
                                symbol = fr.get("symbol")
                                rate = fr.get("rate")
                                if symbol and rate is not None:
                                    if not symbol.endswith("-USD"):
                                        symbol = f"{symbol}-USD"
                                    lighter.funding_cache[symbol] = float(rate)
                        logger.info(f"✅ Lighter funding rates: {len(lighter.funding_cache)}")
                    else:
                        logger.warning(f"⚠️ Lighter funding rates HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Lighter funding preload failed: {e}")

        await asyncio.sleep(3.0)  # Pause nach Funding Rates

        # Step 4: OI Tracker (macht KEINE sofortigen API calls)
        logger.info("📊 Step 4/5: Initializing OI Tracker...")
        try:
            common_symbols = list(set(x10.market_info.keys()) & set(lighter.market_info.keys()))
            oi_tracker = OpenInterestTracker(x10_adapter=x10, lighter_adapter=lighter)
            for sym in common_symbols:
                oi_tracker.track_symbol(sym)
            # NICHT sofort starten - später im Background
            logger.info(f"✅ OI Tracker initialized (symbols={len(common_symbols)})")
        except Exception as e:
            logger.warning(f"⚠️ OI Tracker init failed: {e}")
            oi_tracker = None

        await asyncio.sleep(2.0)

        # Step 5: X10 Funding Rates (bereits in market_cache)
        logger.info("📊 Step 5/5: Refreshing X10 funding rates...")
        # X10 hat die Rates bereits in market_info - nur loggen
        logger.info(f"✅ X10 funding rates: {len(x10.funding_cache)}")

        logger.info(f"✅ All market data loaded: X10={x10_count}, Lighter={lit_count}")

    except asyncio.TimeoutError:
        logger.error("❌ Market loading TIMEOUT")
        raise
    except Exception as e:
        logger.critical(f"❌ Market loading failed: {e}")
        raise

    # ═══════════════════════════════════════════════════════════════
    # LIGHTER PRICES PRE-LOAD
    # ═══════════════════════════════════════════════════════════════
    logger.info("📊 Pre-loading Lighter prices from REST...")
    prices_loaded = await lighter.load_prices_from_rest()
    if prices_loaded == 0:
        logger.warning("⚠️ No Lighter prices loaded - will retry...")
        await asyncio.sleep(2)
        prices_loaded = await lighter.load_prices_from_rest()

    logger.info(f"✅ Pre-loaded {prices_loaded} Lighter prices, cache size: {len(lighter.price_cache)}")

    # ═══════════════════════════════════════════════════════════════
    # WebSocket Start - NACH allen REST calls
    # ═══════════════════════════════════════════════════════════════
    await asyncio.sleep(5.0)  # Sicherheits-Pause vor WebSocket

    logger.info("🌐 Starting WebSocket streams...")
    ws_manager = WebSocketManager()
    ws_manager.set_adapters(x10, lighter)

    try:
        await asyncio.wait_for(ws_manager.start(), timeout=30.0)
        logger.info("✅ WebSocket streams connected")
    except asyncio.TimeoutError:
        logger.warning("⚠️ WebSocket connection timeout (30s)")
    except Exception as e:
        logger.error(f"❌ WebSocket Manager failed: {e}")

    # OI Tracker starten - TEMPORÄR kürzere Intervalle für Debugging
    if oi_tracker:
        # TEMP: Debug-friendly settings
        oi_tracker._fetch_interval = 30.0   # ← War 60, jetzt 30s
        oi_tracker._batch_size = 5          # ← War 3, jetzt 5
        oi_tracker._batch_delay = 2.0       # ← War 5, jetzt 2s

        try:
            await oi_tracker.start(update_interval=30.0)
            logger.info("✅ OI Tracker started (interval=30s, batch=5, delay=2s)")
        except Exception as e:
            logger.warning(f"⚠️ OI Tracker start failed: {e}")

    # ═══════════════════════════════════════════════════════════════
    # Reconciliation - LETZTE REST-Operation
    # Längere Pause um 429 zu vermeiden!
    # ═══════════════════════════════════════════════════════════════
    logger.info("⏳ Waiting 30s before reconciliation to avoid rate limits...")
    await asyncio.sleep(30.0)  # ← ERHÖHT von 5 auf 30 Sekunden!

    try:
        await reconcile_db_with_exchanges(lighter, x10)
    except Exception as e:
        logger.exception(f"Reconciliation failed: {e}")
        
    # ═══════════════════════════════════════════════════════════════════════════
    # 4. INIT PARALLEL EXECUTION MANAGER
    # ═══════════════════════════════════════════════════════════════════════════
    parallel_exec = ParallelExecutionManager(x10, lighter)
    await parallel_exec.start()
    logger.info("✅ ParallelExecutionManager started")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 5. CREATE TASK SUPERVISOR
    # ═══════════════════════════════════════════════════════════════════════════
    supervisor = TaskSupervisor()
    
    # Register core tasks
    supervisor.register(
        "logic_loop",
        lambda: logic_loop(lighter, x10, price_event, parallel_exec),
        critical=True,
        max_restarts=10
    )
    
    supervisor.register(
        "farm_loop",
        lambda: farm_loop(lighter, x10, parallel_exec),
        critical=False,
        max_restarts=5
    )
    
    supervisor.register(
        "maintenance_loop",
        lambda: maintenance_loop(lighter, x10),
        critical=False,
        max_restarts=5
    )
    
    supervisor.register(
        "trade_management_loop",
        lambda: trade_management_loop(lighter, x10),
        critical=True,
        max_restarts=10
    )
    
    supervisor.register(
        "cleanup_finished_tasks",
        lambda: cleanup_finished_tasks(),
        critical=False,
        max_restarts=3
    )
    
    supervisor.register(
        "health_reporter",
        lambda: health_reporter(supervisor, parallel_exec),
        critical=False,
        max_restarts=3
    )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 6. START ALL TASKS
    # ═══════════════════════════════════════════════════════════════════════════
    await supervisor.start_all()
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.send_message(
            "🚀 **Funding Bot V5 Started**\n"
            f"Tasks: {len(supervisor.tasks)}\n"
            "Task Supervision: ✅"
        )
    
    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("   BOT RUNNING 24/7 - SUPERVISED | Ctrl+C = Graceful Stop   ")
    logger.info("═══════════════════════════════════════════════════════════")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 7. WAIT FOR SHUTDOWN
    # ═══════════════════════════════════════════════════════════════════════════
    try:
        await supervisor.wait_for_shutdown()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("🛑 Shutdown requested via keyboard")
        
    # ═══════════════════════════════════════════════════════════════════════════
    # 8. GRACEFUL SHUTDOWN
    # ═══════════════════════════════════════════════════════════════════════════
    SHUTDOWN_FLAG = True
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.send_message("🛑 **Bot shutting down...**")
        
    await supervisor.shutdown()
    await parallel_exec.stop()
    
    # Close state manager
    await close_state_manager()
    logger.info("✅ State Manager stopped")
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.stop()
    
    # Close database connections
    await close_database()
    logger.info("✅ Database closed")
        
    logger.info("✅ Bot shutdown complete")


async def health_reporter(supervisor: TaskSupervisor, parallel_exec):
    """Periodic health status reporter"""
    interval = 3600  # 1 hour
    telegram = get_telegram_bot()
    
    while not supervisor.shutdown_event.is_set():
        try:
            await asyncio.sleep(interval)
            
            health = supervisor.get_health_report()
            exec_stats = parallel_exec.get_execution_stats()
            
            # Get prediction stats
            predictor = get_predictor()
            pred_stats = predictor.get_stats()
            
            # Log health
            running = sum(1 for t in health["tasks"].values() if t["state"] == "RUNNING")
            total = len(health["tasks"])
            
            logger.info(
                f"📊 Health: {running}/{total} tasks running, "
                f"{exec_stats.get('total_executions', 0)} trades, "
                f"{exec_stats.get('pending_rollbacks', 0)} pending rollbacks"
            )
            
            logger.info(
                f"📊 Prediction: {pred_stats['predictions_made']} predictions, "
                f"avg confidence: {pred_stats['avg_confidence']:.2f}"
            )
            
            # Send to Telegram every 6 hours
            if interval >= 3600 and telegram.enabled:
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
# MAIN (Original V4 - kept for compatibility)
# ============================================================
async def main():
    global SHUTDOWN_FLAG, state_manager
    logger.info(" 🔥 BOT V4 (Full Architected) STARTING...")
    oi_tracker = None  # Open Interest Tracker instance

    # 1. Init Infrastructure
    from src.state_manager import get_state_manager as get_sm_async
    state_manager = await get_sm_async()
    logger.info("✅ State Manager started")
    global telegram_bot
    telegram_bot = get_telegram_bot()
    if telegram_bot.enabled:
        await telegram_bot.start()
        logger.info("📱 Telegram Bot connected")
    await setup_database()
    await migrate_database()
    
    # 2. Init Adapters
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # 3. Load Market Data FIRST (CRITICAL - Required for WebSocket subscriptions)
    logger.info("📊 Loading Market Data...")
    try:
        # PARALLEL mit TIMEOUT statt sequential
        load_results = await asyncio.wait_for(
            asyncio.gather(
                x10.load_market_cache(force=True),
                lighter.load_market_cache(force=True),
                return_exceptions=True
            ),
            timeout=60.0  # 60 Sekunden max für beide
        )

        # Check for errors from the parallel load
        for i, result in enumerate(load_results):
            if isinstance(result, Exception):
                exchange = "X10" if i == 0 else "Lighter"
                logger.error(f"❌ {exchange} market load failed: {result}")

        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)

        logger.info(f"✅ Markets loaded: X10={x10_count}, Lighter={lit_count}")

        # Nach "✅ Markets loaded" und VOR WebSocket-Start: Funding Rates vorladen
        logger.info("📈 Pre-loading funding rates...")

        # Lighter funding rates laden
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for fr in data.get("funding_rates", []):
                            if fr.get("exchange") == "lighter":
                                symbol = fr.get("symbol")
                                rate = fr.get("rate")
                                if symbol and rate is not None:
                                    # Normalize symbol to include -USD
                                    if not symbol.endswith("-USD"):
                                        symbol = f"{symbol}-USD"
                                    lighter.funding_cache[symbol] = float(rate)
                        logger.info(f"✅ Lighter funding rates pre-loaded: {len(lighter.funding_cache)}")
        except Exception as e:
            logger.warning(f"⚠️ Lighter funding preload: {e}")

        # X10 funding rates (bereits in market_cache enthalten)
        await x10.load_market_cache(force=True)
        logger.info(f"✅ X10 funding rates: {len(x10.funding_cache)}")

        # ============================================================
        # 3.5 INIT OPEN INTEREST TRACKER (after markets + funding loaded)
        # ============================================================
        try:
            common_symbols = list(set(x10.market_info.keys()) & set(lighter.market_info.keys()))
            oi_tracker = OpenInterestTracker(x10_adapter=x10, lighter_adapter=lighter)
            for sym in common_symbols:
                oi_tracker.track_symbol(sym)
            await oi_tracker.start()
            logger.info(f"✅ OpenInterestTracker started (symbols={len(common_symbols)})")
        except Exception as e:
            logger.warning(f"⚠️ OI Tracker start failed: {e}")
            oi_tracker = None

    except asyncio.TimeoutError:
        logger.error("❌ Market loading TIMEOUT (60s) - continuing with partial data")
        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)
        logger.info(f"⚠️ Partial markets: X10={x10_count}, Lighter={lit_count}")
        
        if x10_count == 0 and lit_count == 0:
            raise ValueError("No markets loaded from any exchange after timeout")

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
    ws_manager = WebSocketManager()
    ws_manager.set_adapters(x10, lighter)
    cleanup_task = None

    # DEBUG: Log before start
    logger.info("DEBUG: About to call ws_manager.start()...")

    try:
        await asyncio.wait_for(ws_manager.start(), timeout=30.0)
        logger.info("✅ WebSocket streams connected")
    except asyncio.TimeoutError:
        logger.warning("⚠️ WebSocket connection timeout (30s) - continuing with REST fallback")
    except Exception as e:
        logger.error(f"❌ WebSocket Manager failed to start: {e}")
        import traceback
        traceback.print_exc()

    # DEBUG: Log after start
    logger.info("DEBUG: ws_manager.start() completed or failed, continuing...")

    # ═══════════════════════════════════════════════════════════════════════════════
    # CRITICAL: Task Cleanup verhindert Memory Leaks
    # ═══════════════════════════════════════════════════════════════════════════════
    # cleanup_task wird später in `tasks` erstellt; initialisiert als None weiter oben

    # ═══════════════════════════════════════════════════════════════
    # CRITICAL: Reconcile DB state with exchange reality
    # ═══════════════════════════════════════════════════════════════
    try:
        await reconcile_db_with_exchanges(lighter, x10)
    except Exception as e:
        logger.exception(f"Reconciliation failed: {e}")
    await asyncio.sleep(2)

    # ═══════════════════════════════════════════════════════════════
    # EMERGENCY: Close all positions on start if enabled
    # Set EMERGENCY_CLOSE_ON_START=True in config to activate
    # ═══════════════════════════════════════════════════════════════
    if getattr(config, 'EMERGENCY_CLOSE_ON_START', False):
        await close_all_open_positions_on_start(lighter, x10)
        await asyncio.sleep(5)

    # ═══════════════════════════════════════════════════════════════
    # CRITICAL: Emergency cleanup for stuck trades
    # ═══════════════════════════════════════════════════════════════
    emergency_task = asyncio.create_task(
        emergency_position_cleanup(lighter, x10, max_age_hours=48.0)
    )
    
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
            # Refresh missing prices for symbols without trades
            logger.info("📊 Refreshing missing prices...")
            try:
                await x10.refresh_missing_prices()
            except Exception as e:
                logger.warning(f"Price refresh warning: {e}")
            break
            
        if check == 2:
            logger.warning("⚠️ WebSocket streams not receiving data - proceeding anyway")

    # 5. Init Managers
    parallel_exec = ParallelExecutionManager(x10, lighter)
    
    # ═══════════════════════════════════════════════════════════════════════
    # STARTUP RECONCILIATION - Sync DB with Exchange State
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("🔄 Running startup reconciliation...")
    try:
        from utils.startup_sync import synchronize_state
        
        trade_repo = get_trade_repository()
        db = get_database()
        
        reconcile_stats = await synchronize_state(
            x10_adapter=x10,
            lighter_adapter=lighter,
            trade_repository=trade_repo,
            db=db
        )
        
        if reconcile_stats['recovered'] + reconcile_stats['closed'] + reconcile_stats['updated'] > 0:
            logger.warning(
                f"⚠️ Database was out of sync! Fixed:\n"
                f"   📥 Recovered: {reconcile_stats['recovered']} trades\n"
                f"   🗑️ Closed: {reconcile_stats['closed']} ghosts\n"
                f"   🔄 Updated: {reconcile_stats['updated']} sizes"
            )
        else:
            logger.info("✅ Database state is clean - no corrections needed")
            
    except Exception as e:
        logger.error(f"❌ Startup reconciliation failed: {e}", exc_info=True)
        logger.warning("⚠️ Continuing without reconciliation - manual check recommended")
    
    # ═══════════════════════════════════════════════════════════════════════
    # FUNDING TRACKER INITIALIZATION
    # ═══════════════════════════════════════════════════════════════════════
    global FUNDING_TRACKER
    logger.info("💰 Initializing Funding Tracker...")
    try:
        trade_repo = get_trade_repository()
        FUNDING_TRACKER = await create_funding_tracker(
            x10_adapter=x10,
            lighter_adapter=lighter,
            trade_repository=trade_repo,
            update_interval_hours=1.0  # Update every hour
        )
        logger.info("✅ Funding Tracker started (updates every 1 hour)")
    except Exception as e:
        logger.error(f"❌ Funding Tracker initialization failed: {e}")
        FUNDING_TRACKER = None
    
    # ===============================================
    # PUNKT 1 – KORREKTE ENDLESS TASK SUPERVISION
    # ===============================================
    tasks = [
        asyncio.create_task(logic_loop(lighter, x10, price_event, parallel_exec), name="logic_loop"),
        asyncio.create_task(farm_loop(lighter, x10, parallel_exec), name="farm_loop"),
        asyncio.create_task(maintenance_loop(lighter, x10), name="maintenance_loop"),
        asyncio.create_task(cleanup_finished_tasks(), name="cleanup_finished_tasks"),   # ← PUNKT 2

        # ➤ NEU: Exit monitoring loop - checks for spread convergence and closes positions
        asyncio.create_task(exit_monitoring_loop(lighter, x10, parallel_exec), name="exit_monitoring_loop"),

        # ➤ NEU: Dieser Task hat gefehlt! Er schließt die Trades.
        asyncio.create_task(trade_management_loop(lighter, x10), name="trade_management_loop")
    ]

    # Task-Überwachung: Eine Task crasht → Bot läuft weiter
    def task_watcher(task: asyncio.Task):
        if task.cancelled():
            logger.info(f"[SUPERVISOR] Task {task.get_name()} cancelled")
            return
        if exc := task.exception():
            logger.critical(f"[SUPERVISOR] Task {task.get_name()} crashed!", exc_info=exc)
            if telegram_bot and telegram_bot.enabled:
                asyncio.create_task(telegram_bot.send_error(f"Task Crash: {task.get_name()}\n{exc}"))

    for t in tasks:
        t.add_done_callback(task_watcher)

    logger.info("BOT LÄUFT 24/7 – Tasks überwacht | Ctrl+C = Stop")

    # WICHTIG: Kein gather mehr! Nur Endlosschleife bis Ctrl+C
    try:
        while not SHUTDOWN_FLAG:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown angefordert – beende sauber...")
    finally:
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: SHUTDOWN_FLAG ZUERST setzen!
        # ═══════════════════════════════════════════════════════════════
        SHUTDOWN_FLAG = True
        logger.info("SHUTDOWN_FLAG gesetzt - keine neuen Trades mehr")
        
        # Warte kurz damit alle Loops das Flag sehen
        await asyncio.sleep(0.5)

        # ═══════════════════════════════════════════════════════════════
        # STEP 1: Cancel ALLE Haupt-Tasks ZUERST
        # ═══════════════════════════════════════════════════════════════
        logger.info("Cancelling main tasks...")
        for task in tasks:
            if not task.done():
                task.cancel()
        
        # Warte auf alle Haupt-Tasks
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, asyncio.CancelledError):
                    logger.debug(f"Task {tasks[i].get_name()} cancelled OK")
                elif isinstance(result, Exception):
                    logger.error(f"Task {tasks[i].get_name()} error: {result}")

        # ═══════════════════════════════════════════════════════════════
        # STEP 2: Cancel ALLE Symbol-Tasks
        # ═══════════════════════════════════════════════════════════════
        logger.info("Cancelling symbol tasks...")
        async with TASKS_LOCK:
            for sym, task in list(ACTIVE_TASKS.items()):
                if not task.done():
                    task.cancel()
            if ACTIVE_TASKS:
                await asyncio.gather(*ACTIVE_TASKS.values(), return_exceptions=True)
            ACTIVE_TASKS.clear()
            SYMBOL_LOCKS.clear()

        # ═══════════════════════════════════════════════════════════════
        # STEP 2.5: Clear IN_FLIGHT_MARGIN to prevent leaks
        # ═══════════════════════════════════════════════════════════════
        async with IN_FLIGHT_LOCK:
            old_x10 = IN_FLIGHT_MARGIN.get('X10', 0)
            old_lit = IN_FLIGHT_MARGIN.get('Lighter', 0)
            if old_x10 > 0 or old_lit > 0:
                logger.info(f"🔓 Clearing IN_FLIGHT_MARGIN: X10=${old_x10:.1f}, Lit=${old_lit:.1f}")
            IN_FLIGHT_MARGIN.clear()
        # ═══════════════════════════════════════════════════════════════
        # STEP 3: Stoppe Manager (WebSocket, State, Telegram)
        # ═══════════════════════════════════════════════════════════════
        logger.info("Stopping Managers...")
        
        # Stop Funding Tracker first
        if FUNDING_TRACKER:
            logger.info("💰 Stopping Funding Tracker...")
            try:
                await FUNDING_TRACKER.stop()
                logger.info("✅ Funding Tracker stopped")
            except Exception as e:
                logger.error(f"❌ Error stopping Funding Tracker: {e}")
        
        await ws_manager.stop()

        # Cancel the cleanup task started earlier (Zeile 2017)
        if cleanup_task and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel emergency cleanup
        if 'emergency_task' in locals() and not emergency_task.done():
            emergency_task.cancel()
            try:
                await emergency_task
            except asyncio.CancelledError:
                pass

        await state_manager.stop()

        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_message("Bot Shutdown")
            await telegram_bot.stop()

        # ═══════════════════════════════════════════════════════════════
        # STEP 4: Schließe Adapter
        # ═══════════════════════════════════════════════════════════════
        logger.info("Closing adapters...")
        try:
            await x10.aclose()
        except Exception as e:
            logger.error(f"Error closing X10: {e}")

        try:
            await lighter.aclose()
        except Exception as e:
            logger.error(f"Error closing Lighter: {e}")

        # Kurz warten damit aiohttp Sessions sauber schließen
        await asyncio.sleep(0.5)

        logger.info("BOT SHUTDOWN COMPLETE.")

    # Ensure OI tracker stopped if started
    try:
        if 'oi_tracker' in locals() and getattr(oi_tracker, '_running', False):
            await oi_tracker.stop()
    except Exception:
        pass

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run_bot_v5())
    except KeyboardInterrupt:
        pass