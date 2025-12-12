# src/core/trade_management.py
"""
Trade management - monitoring and managing open trades.

This module handles:
- Managing open trades (PnL tracking, exit conditions)
- Syncing between exchanges
- Zombie position cleanup
- PnL calculation helpers
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

import config
from src.utils import safe_float
from src.fee_manager import get_fee_manager
from src.kelly_sizing import get_kelly_sizer
from src.telegram_bot import get_telegram_bot

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared references - set from main.py)
# ============================================================
SHUTDOWN_FLAG = False
RECENTLY_OPENED_TRADES = {}
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 60.0
ACTIVE_TASKS = {}
TASKS_LOCK = asyncio.Lock()

# Position cache
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 5.0


# ============================================================
# LAZY IMPORTS
# ============================================================
def _get_state_functions():
    """Lazy import to avoid circular dependencies"""
    from src.core.state import (
        get_open_trades,
        close_trade_in_state,
        archive_trade_to_history,
        get_execution_lock,
    )
    return get_open_trades, close_trade_in_state, archive_trade_to_history, get_execution_lock


def _get_trading_functions():
    """Lazy import trading functions"""
    from src.core.trading import close_trade, safe_close_x10_position
    return close_trade, safe_close_x10_position


# ============================================================
# HELPER FUNCTIONS
# ============================================================
def calculate_trade_age(trade: Dict) -> tuple:
    """Calculate trade age in seconds and hours."""
    entry_time = trade.get('entry_time')
    
    if entry_time:
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
    else:
        age_seconds = getattr(config, 'FARM_HOLD_SECONDS', 3600) + 1
    
    return age_seconds, age_seconds / 3600


def parse_iso_time(entry_time) -> Optional[datetime]:
    """Parse entry_time from various formats to datetime."""
    if entry_time is None:
        return None
    
    if isinstance(entry_time, str):
        try:
            entry_time = entry_time.replace('Z', '+00:00')
            entry_time = datetime.fromisoformat(entry_time)
        except (ValueError, TypeError):
            return None
    
    if isinstance(entry_time, (int, float)):
        entry_time = datetime.fromtimestamp(entry_time, tz=timezone.utc)
    
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    
    return entry_time


async def calculate_realized_pnl(trade: Dict, fee_manager, gross_pnl: float = 0.0) -> Decimal:
    """Calculate realized PnL including all entry and exit fees."""
    entry_value = float(trade.get('notional_usd', 0.0))
    exit_value = entry_value
    
    entry_fee_lighter = trade.get('entry_fee_lighter')
    entry_fee_x10 = trade.get('entry_fee_x10')
    exit_fee_lighter = trade.get('exit_fee_lighter')
    exit_fee_x10 = trade.get('exit_fee_x10')
    
    entry_fees = fee_manager.calculate_trade_fees(
        entry_value, 'LIGHTER', 'X10',
        is_maker1=True, is_maker2=False,
        actual_fee1=entry_fee_lighter, actual_fee2=entry_fee_x10
    )
    
    exit_fees = fee_manager.calculate_trade_fees(
        exit_value, 'LIGHTER', 'X10',
        is_maker1=False, is_maker2=False,
        actual_fee1=exit_fee_lighter, actual_fee2=exit_fee_x10
    )
    
    gross_pnl_decimal = Decimal(str(gross_pnl))
    net_pnl = gross_pnl_decimal - Decimal(str(entry_fees)) - Decimal(str(exit_fees))
    
    return net_pnl


def should_farm_quick_exit(symbol: str, trade: Dict, current_spread: float, gross_pnl: float) -> tuple:
    """
    Determine if a farm trade should be closed quickly.
    Returns: (should_exit: bool, reason: str)
    """
    age_seconds, _ = calculate_trade_age(trade)
    
    min_age = getattr(config, 'FARM_MIN_AGE_SECONDS', 300)
    if age_seconds < min_age:
        return False, "too_young"
    
    if gross_pnl < 0:
        return False, "negative_pnl"
    
    spread_threshold = getattr(config, 'FARM_SPREAD_THRESHOLD', 0.02) / 100.0
    min_profit = getattr(config, 'FARM_MIN_PROFIT_USD', 0.01)
    
    if current_spread <= spread_threshold:
        if gross_pnl >= min_profit:
            return True, f"FARM_PROFIT (age={age_seconds:.0f}s, spread={current_spread*100:.3f}%, pnl=${gross_pnl:.4f})"
        
        max_age_breakeven = getattr(config, 'FARM_MAX_AGE_FOR_BREAKEVEN', 1800)
        if age_seconds >= max_age_breakeven and gross_pnl >= 0:
            return True, f"FARM_AGED_OUT (age={age_seconds:.0f}s, pnl=${gross_pnl:.4f})"
        
        return False, "waiting_for_profit"
    
    return False, "spread_too_high"


# ============================================================
# POSITION CACHING
# ============================================================
async def get_cached_positions(lighter, x10, force=False):
    """Fetch positions with caching."""
    global POSITION_CACHE
    
    now = time.time()
    cache_age = now - POSITION_CACHE['last_update']
    cache_empty = (len(POSITION_CACHE['x10']) == 0 and len(POSITION_CACHE['lighter']) == 0)
    
    if not force and not cache_empty and cache_age < POSITION_CACHE_TTL:
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    
    try:
        t1 = asyncio.create_task(x10.fetch_open_positions())
        t2 = asyncio.create_task(lighter.fetch_open_positions())
        
        p_x10, p_lit = await asyncio.wait_for(
            asyncio.gather(t1, t2, return_exceptions=True),
            timeout=10.0
        )
        
        if isinstance(p_x10, Exception):
            p_x10 = POSITION_CACHE.get('x10', [])
        if isinstance(p_lit, Exception):
            p_lit = POSITION_CACHE.get('lighter', [])
        
        p_x10 = p_x10 if isinstance(p_x10, list) else []
        p_lit = p_lit if isinstance(p_lit, list) else []
        
        POSITION_CACHE['x10'] = p_x10
        POSITION_CACHE['lighter'] = p_lit
        POSITION_CACHE['last_update'] = now
        
        logger.info(f"📊 Positions: X10={len(p_x10)}, Lighter={len(p_lit)}")
        return p_x10, p_lit
        
    except asyncio.TimeoutError:
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    except Exception as e:
        logger.error(f"Position cache error: {e}")
        return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])


# ============================================================
# SYNC CHECK  
# ============================================================
async def sync_check_and_fix(lighter, x10, parallel_exec=None):
    """Check if X10 and Lighter positions are synced and fix differences."""
    get_open_trades, _, _, get_execution_lock = _get_state_functions()
    _, safe_close_x10_position = _get_trading_functions()
    
    logger.info("🔍 Starting Exchange Sync Check...")
    
    try:
        # Check for active executions
        active_symbols = set()
        if parallel_exec and hasattr(parallel_exec, 'active_executions'):
            active_symbols = set(parallel_exec.active_executions.keys())
        
        # Get recently opened symbols
        recently_opened = set()
        current_time = time.time()
        
        async with RECENTLY_OPENED_LOCK:
            for sym, open_time in list(RECENTLY_OPENED_TRADES.items()):
                age = current_time - open_time
                if age < RECENTLY_OPENED_PROTECTION_SECONDS:
                    recently_opened.add(sym)
                else:
                    RECENTLY_OPENED_TRADES.pop(sym, None)
        
        # Check DB trades
        try:
            open_trades = await get_open_trades()
            for trade in open_trades:
                symbol = trade.get('symbol')
                if not symbol:
                    continue
                entry_time = parse_iso_time(trade.get('entry_time'))
                if entry_time:
                    age_seconds = current_time - entry_time.timestamp()
                    if age_seconds < 30.0:
                        recently_opened.add(symbol)
        except Exception:
            pass
        
        # Fetch positions
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
        
        x10_symbols = {
            p.get('symbol') for p in (x10_positions or [])
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        lighter_symbols = {
            p.get('symbol') for p in (lighter_positions or [])
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        
        # Find desync
        only_on_x10 = (x10_symbols - lighter_symbols) - recently_opened
        only_on_lighter = (lighter_symbols - x10_symbols) - recently_opened
        
        # Handle orphaned X10 positions
        if only_on_x10:
            logger.error(f"🚨 DESYNC: Positions only on X10: {only_on_x10}")
            for sym in only_on_x10:
                if sym in recently_opened or sym in active_symbols:
                    continue
                try:
                    pos = next((p for p in x10_positions if p.get('symbol') == sym), None)
                    if pos:
                        size = safe_float(pos.get('size', 0))
                        original_side = "BUY" if size > 0 else "SELL"
                        px = safe_float(x10.fetch_mark_price(sym))
                        if px > 0:
                            notional = abs(size) * px
                            await x10.close_live_position(sym, original_side, notional)
                            logger.info(f"✅ Closed orphaned X10 {sym}")
                except Exception as e:
                    logger.error(f"Failed to close X10 orphan {sym}: {e}")
        
        # Handle orphaned Lighter positions
        if only_on_lighter:
            real_orphans = []
            for sym in only_on_lighter:
                if sym in recently_opened or sym in active_symbols:
                    continue
                lock = await get_execution_lock(sym)
                if lock.locked() or sym in ACTIVE_TASKS:
                    continue
                real_orphans.append(sym)
            
            if real_orphans:
                logger.error(f"🚨 DESYNC: Positions only on Lighter: {real_orphans}")
                for sym in real_orphans:
                    try:
                        pos = next((p for p in lighter_positions if p.get('symbol') == sym), None)
                        if pos:
                            size = safe_float(pos.get('size', 0))
                            original_side = "BUY" if size > 0 else "SELL"
                            px = safe_float(lighter.fetch_mark_price(sym))
                            if px > 0:
                                notional = abs(size) * px
                                await lighter.close_live_position(sym, original_side, notional)
                                logger.info(f"✅ Closed orphaned Lighter {sym}")
                    except Exception as e:
                        logger.error(f"Failed to close Lighter orphan {sym}: {e}")
        
        if not only_on_x10 and not only_on_lighter:
            common = x10_symbols & lighter_symbols
            logger.info(f"✅ Exchanges are SYNCED: {len(common)} paired positions")
            
    except Exception as e:
        logger.error(f"Sync check failed: {e}")


# ============================================================
# ZOMBIE CLEANUP
# ============================================================
async def cleanup_zombie_positions(lighter, x10):
    """Clean up zombie positions (open on exchange but closed in DB)."""
    get_open_trades, _, _, _ = _get_state_functions()
    
    try:
        x_pos, l_pos = await get_cached_positions(lighter, x10, force=True)

        x_syms = {p['symbol'] for p in x_pos if abs(safe_float(p.get('size', 0))) > 1e-8}
        l_syms = {p['symbol'] for p in l_pos if abs(safe_float(p.get('size', 0))) > 1e-8}

        db_trades = await get_open_trades()
        db_syms = {t['symbol'] for t in db_trades}

        all_exchange = x_syms | l_syms
        zombies = all_exchange - db_syms

        if zombies:
            logger.warning(f"🧟 ZOMBIES DETECTED: {zombies}")
            for sym in zombies:
                if sym in x_syms:
                    p = next((pos for pos in x_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = safe_float(p.get('size', 0))
                        original_side = "BUY" if position_size > 0 else "SELL"
                        size_usd = abs(position_size) * safe_float(x10.fetch_mark_price(sym))
                        
                        min_x10 = x10.min_notional_usd(sym) if hasattr(x10, 'min_notional_usd') else 5.0
                        if size_usd < min_x10:
                            logger.warning(f"⚠️ X10 zombie {sym} too small (${size_usd:.2f})")
                            continue
                        
                        try:
                            await x10.close_live_position(sym, original_side, size_usd)
                            logger.info(f"✅ Closed X10 zombie {sym}")
                        except Exception as e:
                            logger.error(f"Failed to close X10 zombie {sym}: {e}")

                if sym in l_syms:
                    p = next((pos for pos in l_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = safe_float(p.get('size', 0))
                        original_side = "BUY" if position_size > 0 else "SELL"
                        px = safe_float(lighter.fetch_mark_price(sym))
                        if px > 0:
                            size_usd = abs(position_size) * px
                            try:
                                await lighter.close_live_position(sym, original_side, size_usd)
                                logger.info(f"✅ Closed Lighter zombie {sym}")
                            except Exception as e:
                                logger.error(f"Failed to close Lighter zombie {sym}: {e}")
                                
    except Exception as e:
        logger.error(f"Zombie Cleanup Error: {e}")


# ============================================================
# RECONCILE DB WITH EXCHANGES
# ============================================================
async def reconcile_db_with_exchanges(lighter, x10):
    """
    CRITICAL: Reconcile database state with actual exchange positions.
    Closes DB entries for trades that don't exist on exchanges.
    """
    get_open_trades, close_trade_in_state, _, _ = _get_state_functions()
    
    logger.info("🔍 STATE RECONCILIATION: Checking DB vs Exchange...")
    
    open_trades = await get_open_trades()
    if not open_trades:
        logger.info("✓ No DB trades to reconcile")
        return
    
    try:
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
    except Exception as e:
        logger.error(f"✗ Failed to fetch positions: {e}")
        return
    
    x10_symbols = {p.get('symbol') for p in (x10_positions or [])}
    lighter_symbols = {p.get('symbol') for p in (lighter_positions or [])}
    
    ghost_count = 0
    for trade in open_trades:
        symbol = trade['symbol']
        
        on_x10 = symbol in x10_symbols
        on_lighter = symbol in lighter_symbols
        
        if not on_x10 and not on_lighter:
            logger.warning(
                f"👻 GHOST TRADE: {symbol} in DB but NOT on exchanges - cleaning DB"
            )
            try:
                await close_trade_in_state(symbol)
            except Exception as e:
                logger.error(f"Failed to mark {symbol} closed in state: {e}")
            
            ghost_count += 1
            await asyncio.sleep(0.1)
    
    if ghost_count > 0:
        logger.warning(f"🧹 Cleaned {ghost_count} ghost trades from DB")
    else:
        logger.info("✓ All DB trades match exchange positions")
    
    open_trades_after = await get_open_trades()
    logger.info(f"📊 Final state: {len(open_trades_after)} open trades")


async def reconcile_state_with_exchange(lighter, x10, parallel_exec):
    """
    Aggressive Sync: The exchange is the single source of truth.
    1. Get real positions from Lighter & X10.
    2. If DB says 'trade open' but exchange empty -> delete from DB (Zombie).
    3. If exchange has position but DB empty -> panic close (Orphan).
    """
    get_open_trades, close_trade_in_state, _, _ = _get_state_functions()
    
    try:
        # Skip if busy
        if parallel_exec and hasattr(parallel_exec, 'is_busy') and parallel_exec.is_busy():
            return

        logger.info("🔍 RECONCILE: Starting strict sync check...")
        
        # Fetch real positions
        results = await asyncio.gather(
            lighter.fetch_open_positions(),
            x10.fetch_open_positions(),
            return_exceptions=True
        )
        
        lighter_pos = results[0] if not isinstance(results[0], Exception) else []
        x10_pos = results[1] if not isinstance(results[1], Exception) else []
        
        if lighter_pos is None: lighter_pos = []
        if x10_pos is None: x10_pos = []
        
        real_lighter = {
            p.get('symbol'): float(p.get('size', 0)) 
            for p in lighter_pos 
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        real_x10 = {
            p.get('symbol'): float(p.get('size', 0)) 
            for p in x10_pos 
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        
        # Get DB trades
        from src.state_manager import get_state_manager
        sm = await get_state_manager()
        db_trades = await sm.get_all_open_trades()
        
        # Check for zombies
        current_time = time.time()
        for trade in db_trades:
            symbol = trade.symbol
            
            if symbol in RECENTLY_OPENED_TRADES:
                # ═══════════════════════════════════════════════════════════════
                # FIX: POST_ONLY Orders need more time to fill (they're Maker orders that wait in orderbook)
                # Extended protection to 120s to allow POST_ONLY orders time to fill before reconciliation
                # ═══════════════════════════════════════════════════════════════
                if current_time - RECENTLY_OPENED_TRADES[symbol] < 120:  # Extended from 60s to 120s
                    continue
            
            has_lighter = symbol in real_lighter and abs(real_lighter[symbol]) > 0
            has_x10 = symbol in real_x10 and abs(real_x10[symbol]) > 0

            if not has_lighter and not has_x10:
                logger.warning(f"⚠️ ZOMBIE FOUND: {symbol} is in DB but not on exchange. Closing...")
                await sm.close_trade(symbol, 0.0, 0.0)
                
            elif not has_lighter or not has_x10:
                logger.error(f"⚠️ PARTIAL ZOMBIE {symbol}: Lighter={has_lighter}, X10={has_x10}")
                
                if has_x10:
                    try:
                        size = real_x10[symbol]
                        side = "BUY" if size < 0 else "SELL"
                        await x10.close_live_position(symbol, side, abs(size) * x10.fetch_mark_price(symbol))
                    except Exception as e:
                        logger.error(f"Failed to close X10 part of zombie {symbol}: {e}")

                if has_lighter:
                    try:
                        size = real_lighter[symbol]
                        side = "BUY" if size < 0 else "SELL"
                        await lighter.close_live_position(symbol, side, abs(size) * (lighter.get_price(symbol) or 0))
                    except Exception as e:
                        logger.error(f"Failed to close Lighter part of zombie {symbol}: {e}")
                
                await sm.close_trade(symbol, 0.0, 0.0)

        # Check for orphans
        all_exchange_symbols = set(real_lighter.keys()) | set(real_x10.keys())
        db_symbols = {t.symbol for t in db_trades}
        
        for symbol in all_exchange_symbols:
            if symbol not in db_symbols:
                if symbol in RECENTLY_OPENED_TRADES:
                    # Extended protection for POST_ONLY orders (see comment above)
                    if current_time - RECENTLY_OPENED_TRADES[symbol] < 120:  # Extended from 60s to 120s
                        continue
                        
                l_size = real_lighter.get(symbol, 0)
                x_size = real_x10.get(symbol, 0)
                
                logger.error(f"👻 ORPHAN POSITION: {symbol} found (L={l_size}, X={x_size}) but NOT in DB!")
                
                # Fix #12: Automatically close orphan positions
                try:
                    # Close Lighter position if exists
                    if abs(l_size) > 0:
                        logger.warning(f"🚨 Closing orphan Lighter position {symbol} (size={l_size})...")
                        lighter_position = next((p for p in (lighter_pos or []) if p.get('symbol') == symbol), None)
                        if lighter_position:
                            px = safe_float(lighter.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(l_size) * px
                                original_side = "BUY" if l_size < 0 else "SELL"
                                await lighter.close_live_position(symbol, original_side, notional)
                                logger.info(f"✅ Closed orphaned Lighter {symbol}")
                    
                    # Close X10 position if exists
                    if abs(x_size) > 0:
                        logger.warning(f"🚨 Closing orphan X10 position {symbol} (size={x_size})...")
                        x10_position = next((p for p in (x10_pos or []) if p.get('symbol') == symbol), None)
                        if x10_position:
                            px = safe_float(x10.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(x_size) * px
                                original_side = "BUY" if x_size < 0 else "SELL"
                                await x10.close_live_position(symbol, original_side, notional)
                                logger.info(f"✅ Closed orphaned X10 {symbol}")
                except Exception as e:
                    logger.error(f"❌ Failed to close orphan position {symbol}: {e}")
                    
        logger.info("✅ RECONCILE: Sync complete.")
        
    except Exception as e:
        logger.error(f"Reconcile error: {e}")


# ============================================================
# MAIN TRADE MANAGEMENT FUNCTION
# ============================================================
async def manage_open_trades(lighter, x10, state_manager=None):
    """
    Monitors open trades and closes them based on exit conditions.
    
    Exit conditions:
    - MIN_PROFIT_EXIT_USD reached (profit-only exit)
    - MAX_HOLD_HOURS exceeded (safety override)
    - FARM trades: quick exit on low spread + profit
    - Funding flip detection
    """
    get_open_trades, close_trade_in_state, archive_trade_to_history, _ = _get_state_functions()
    close_trade, _ = _get_trading_functions()
    
    trades = await get_open_trades()
    if not trades:
        return

    try:
        p_x10, p_lit = await get_cached_positions(lighter, x10)
    except:
        return

    current_time = time.time()

    for t in trades:
        try:
            sym = t['symbol']
            
            # ═══════════════════════════════════════════════════════════════
            # Data sanitizing for all numeric fields
            # ═══════════════════════════════════════════════════════════════
            notional = t.get('notional_usd')
            notional = float(notional) if notional is not None else 0.0

            init_funding = t.get('initial_funding_rate_hourly')
            init_funding = float(init_funding) if init_funding is not None else 0.0
            
            # Calculate age
            age_seconds, hold_hours = calculate_trade_age(t)
            
            logger.debug(f"Check {sym}: Age={age_seconds:.1f}s (Limit={getattr(config, 'FARM_HOLD_SECONDS', 3600)}s)")
            
            # ═══════════════════════════════════════════════════════════════
            # Get prices for PnL calculation
            # ═══════════════════════════════════════════════════════════════
            raw_px = x10.fetch_mark_price(sym)
            raw_pl = lighter.fetch_mark_price(sym)
            px = safe_float(raw_px) if raw_px is not None else None
            pl = safe_float(raw_pl) if raw_pl is not None else None

            # REST Fallback if WebSocket has no prices
            if px is None or pl is None or px <= 0 or pl <= 0:
                logger.debug(f"{sym}: WS prices missing, trying REST fallback...")
                try:
                    if px is None or px <= 0:
                        if hasattr(x10, 'get_price_rest'):
                            px = safe_float(await x10.get_price_rest(sym))
                        elif hasattr(x10, 'load_market_cache'):
                            await x10.load_market_cache(force=True)
                            px = safe_float(x10.fetch_mark_price(sym))
                    
                    if pl is None or pl <= 0:
                        if hasattr(lighter, 'get_price_rest'):
                            pl = safe_float(await lighter.get_price_rest(sym))
                        elif hasattr(lighter, 'load_funding_rates_and_prices'):
                            await lighter.load_funding_rates_and_prices()
                            pl = safe_float(lighter.fetch_mark_price(sym))
                    
                    if px is not None and pl is not None and px > 0 and pl > 0:
                        logger.info(f"✅ {sym}: REST Fallback success (X10=${px:.2f}, Lit=${pl:.2f})")
                except Exception as e:
                    logger.warning(f"{sym}: REST Fallback failed: {e}")
                
                if px is None or pl is None or px <= 0 or pl <= 0:
                    logger.debug(f"{sym}: No prices available, skipping")
                    continue

            rx = x10.fetch_funding_rate(sym) or 0.0
            rl = lighter.fetch_funding_rate(sym) or 0.0
            
            # Net calculation
            base_net = rl - rx
            current_net = -base_net if t.get('leg1_exchange') == 'X10' else base_net

            # PnL calculation
            funding_pnl = current_net * hold_hours * notional

            # Spread PnL
            ep_x10 = float(t.get('entry_price_x10') or px) 
            ep_lit = float(t.get('entry_price_lighter') or pl)
            
            entry_spread = abs(ep_x10 - ep_lit)
            curr_spread = abs(px - pl)
            
            if px > 0:
                spread_pnl = (entry_spread - curr_spread) / px * notional
                current_spread_pct = curr_spread / px
            else:
                spread_pnl = 0.0
                current_spread_pct = 0.0

            # Gross PnL (before fees)
            gross_pnl = funding_pnl + spread_pnl
            
            # Calculate Net PnL with proper fees
            try:
                fee_manager = get_fee_manager()
                net_pnl_decimal = await calculate_realized_pnl(t, fee_manager, gross_pnl)
                total_pnl = float(net_pnl_decimal)
                
                # Calculate total fees
                entry_value = float(notional)
                exit_value = entry_value
                
                entry_fees = fee_manager.calculate_trade_fees(
                    entry_value, 'LIGHTER', 'X10',
                    is_maker1=True, is_maker2=False,
                    actual_fee1=t.get('entry_fee_lighter'),
                    actual_fee2=t.get('entry_fee_x10')
                )
                
                exit_fees = fee_manager.calculate_trade_fees(
                    exit_value, 'LIGHTER', 'X10',
                    is_maker1=False, is_maker2=False,
                    actual_fee1=t.get('exit_fee_lighter'),
                    actual_fee2=t.get('exit_fee_x10')
                )
                
                est_fees = entry_fees + exit_fees
                
            except Exception as e:
                logger.debug(f"FeeManager error, using fallback: {e}")
                fee_x10 = getattr(config, 'TAKER_FEE_X10', 0.000225)
                fee_lit = getattr(config, 'FEES_LIGHTER', 0.0)
                est_fees = notional * (fee_x10 + fee_lit) * 2.0
                total_pnl = gross_pnl - est_fees

            # ═══════════════════════════════════════════════════════════════
            # PROFIT-ONLY EXIT LOGIC
            # ═══════════════════════════════════════════════════════════════
            min_profit_exit = getattr(config, 'MIN_PROFIT_EXIT_USD', 0.02)
            max_hold_hours = getattr(config, 'MAX_HOLD_HOURS', 24.0)
            
            reason = None
            force_close = False
            
            # 1. Safety override: MAX_HOLD_HOURS
            if hold_hours >= max_hold_hours:
                reason = "MAX_HOLD_EXPIRED"
                force_close = True
                logger.warning(
                    f"⚠️ [FORCE CLOSE] {sym}: Max hold time reached "
                    f"({hold_hours:.1f}h >= {max_hold_hours}h)"
                )
            
            # 2. Profit check
            if not force_close:
                if total_pnl < min_profit_exit:
                    logger.debug(
                        f"💎 [HODL] {sym}: Net PnL ${total_pnl:.4f} < ${min_profit_exit:.2f}"
                    )
                    continue
                
                logger.info(
                    f"💰 [PROFIT] {sym}: Net PnL ${total_pnl:.4f} >= ${min_profit_exit:.2f}"
                )
                
                # Farm Mode Quick Exit
                if not reason and t.get('is_farm_trade') and getattr(config, 'VOLUME_FARM_MODE', False):
                    should_exit, exit_reason = should_farm_quick_exit(
                        symbol=sym,
                        trade=t,
                        current_spread=current_spread_pct,
                        gross_pnl=gross_pnl
                    )
                    
                    if should_exit:
                        if "FARM_PROFIT" in exit_reason:
                            reason = "FARM_QUICK_PROFIT"
                        elif "FARM_AGED_OUT" in exit_reason:
                            reason = "FARM_AGED_OUT"
                        else:
                            reason = "FARM_EXIT"
                        logger.info(f"🚜 [FARM] Quick Exit {sym}: {exit_reason}")
                
                # Take Profit at high profit
                if not reason and notional > 0:
                    if total_pnl > notional * 0.05:
                        reason = "TAKE_PROFIT"
                
                # Farm hold complete
                if not reason and t.get('is_farm_trade') and getattr(config, 'VOLUME_FARM_MODE', False):
                    farm_hold_seconds = getattr(config, 'FARM_HOLD_SECONDS', 3600)
                    if age_seconds > farm_hold_seconds:
                        reason = "FARM_HOLD_COMPLETE"
                        logger.info(
                            f"✅ [FARM COMPLETE] {sym}: Hold time reached AND profitable!"
                        )
                
                # Funding flip (only if profitable)
                if not reason:
                    if init_funding * current_net < 0:
                        flip_hours_threshold = getattr(config, 'FUNDING_FLIP_HOURS_THRESHOLD', 4.0)
                        if not t.get('funding_flip_start_time'):
                            t['funding_flip_start_time'] = datetime.utcnow()
                        else:
                            flip_start = t['funding_flip_start_time']
                            if isinstance(flip_start, str):
                                try:
                                    flip_start = datetime.fromisoformat(flip_start)
                                except:
                                    flip_start = datetime.utcnow()
                            
                            if (datetime.utcnow() - flip_start).total_seconds() / 3600 > flip_hours_threshold:
                                reason = "FUNDING_FLIP_PROFITABLE"

            if reason:
                # Log exit details
                logger.info(
                    f"💸 EXIT {sym}: {reason} | "
                    f"Gross PnL: ${gross_pnl:.2f} | "
                    f"Fees: ${est_fees:.4f} | "
                    f"Net PnL: ${total_pnl:.2f}"
                )
                
                if await close_trade(t, lighter, x10):
                    await close_trade_in_state(sym, pnl=total_pnl, funding=funding_pnl)
                    await archive_trade_to_history(t, reason, {
                        'total_net_pnl': total_pnl, 
                        'funding_pnl': funding_pnl,
                        'spread_pnl': spread_pnl, 
                        'fees': est_fees
                    })
                    
                    # Kelly Sizer recording
                    try:
                        kelly_sizer = get_kelly_sizer()
                        kelly_sizer.record_trade(
                            symbol=sym,
                            pnl_usd=total_pnl,
                            hold_time_seconds=hold_hours * 3600,
                            entry_apy=t.get('apy', None)
                        )
                    except Exception as e:
                        logger.error(f"Kelly Sizer error for {sym}: {e}")
                    
                    # Telegram notification
                    telegram = get_telegram_bot()
                    if telegram.enabled:
                        await telegram.send_trade_alert(sym, reason, notional, total_pnl)

        except Exception as e:
            logger.error(f"Trade Loop Error for {t.get('symbol', 'UNKNOWN')}: {e}")
            import traceback
            logger.debug(traceback.format_exc())

