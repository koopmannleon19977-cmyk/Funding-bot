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
from src.telegram_bot import get_telegram_bot
from src.volatility_monitor import get_volatility_monitor
from src.pnl_utils import compute_hedge_pnl, _side_sign

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared references - set from main.py)
# ============================================================
SHUTDOWN_FLAG = False
RECENTLY_OPENED_TRADES = {}
RECENTLY_CLOSED_TRADES = {}  # FIX: Track recently closed trades to avoid orphan false positives
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 60.0
RECENTLY_CLOSED_PROTECTION_SECONDS = 15.0  # FIX: 15s grace period after close
ACTIVE_TASKS = {}
TASKS_LOCK = asyncio.Lock()

# Position cache
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 5.0

# Strict reconciliation throttle
LAST_STRICT_RECONCILE_AT = 0.0


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


async def calculate_realized_pnl(
    trade: Dict, 
    fee_manager, 
    gross_pnl: float = 0.0,
    lighter_position: Optional[Dict] = None
) -> Decimal:
    """
    Estimate net PnL for EXIT DECISION MAKING (not for final accounting).
    
    This function takes an already-computed gross_pnl (price+funding estimate)
    and subtracts estimated entry/exit fees to give a net PnL estimate.
    
    USE CASES:
    - Deciding whether to exit a trade based on estimated net profit
    - Quick PnL estimation during trade monitoring
    
    DO NOT USE FOR:
    - Final accounting/DB persistence (use calculate_realized_close_pnl instead)
    - Reconciliation with exchange data
    
    For final PnL accounting after close, use calculate_realized_close_pnl()
    which uses compute_hedge_pnl from pnl_utils for accurate, sign-correct
    calculations from actual fill data.
    """
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


def _parse_trade_timestamp(ts_val: Any) -> Optional[float]:
    """Parse various trade timestamp formats to unix seconds (float)."""
    if ts_val is None:
        return None
    # numeric (ms or s)
    if isinstance(ts_val, (int, float)):
        v = float(ts_val)
        # heuristic: ms timestamps are usually > 1e12
        return v / 1000.0 if v > 1e12 else v
    # string: iso or numeric
    try:
        s = str(ts_val).strip()
        if not s:
            return None
        if s.isdigit():
            v = float(s)
            return v / 1000.0 if v > 1e12 else v
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


async def _lighter_fill_stats_for_order(
    lighter,
    symbol: str,
    order_id: Optional[str],
    fallback_since_ts: Optional[float] = None,
    max_wait_s: float = 3.0,
) -> Dict[str, float]:
    """
    Best-effort: derive (avg_price, qty, fee_usd) from Lighter /api/v1/accountTrades.
    Prefers matching by order_id. If not available, falls back to trades newer than fallback_since_ts.
    """
    if not symbol:
        return {"price": 0.0, "qty": 0.0, "fee": 0.0}

    oid = str(order_id) if order_id else None
    deadline = time.time() + max(0.0, max_wait_s)
    last_trades = []

    while True:
        try:
            trades = await lighter.fetch_my_trades(symbol, limit=50, force=True)
            last_trades = trades or []
            fills = []
            if oid:
                fills = [t for t in last_trades if str(t.get("order_id") or "") == oid]
            elif fallback_since_ts is not None:
                for t in last_trades:
                    ts = _parse_trade_timestamp(t.get("timestamp"))
                    if ts is not None and ts >= float(fallback_since_ts):
                        fills.append(t)

            if fills:
                total_qty = sum(safe_float(t.get("size", 0.0), 0.0) for t in fills)
                total_value = sum(
                    safe_float(t.get("price", 0.0), 0.0) * safe_float(t.get("size", 0.0), 0.0)
                    for t in fills
                )
                total_fee = sum(safe_float(t.get("fee", 0.0), 0.0) for t in fills)
                avg_price = (total_value / total_qty) if total_qty > 0 else 0.0
                return {"price": avg_price, "qty": total_qty, "fee": total_fee}
        except Exception:
            pass

        if time.time() >= deadline:
            break
        await asyncio.sleep(0.3)

    return {"price": 0.0, "qty": 0.0, "fee": 0.0}


async def calculate_realized_close_pnl(trade: Dict, lighter, x10) -> Dict[str, float]:
    """
    Compute realized PnL (for DB/accounting) from best-available entry/exit data.
    Returns a dict with breakdown fields (price_pnl, fees, funding, total).
    
    Uses compute_hedge_pnl from pnl_utils for accurate, sign-correct calculations.
    
    H4 Enhancement: Also fetches EXACT PnL breakdown from X10 API for verification.
    """
    symbol = trade.get("symbol")
    notional = safe_float(trade.get("notional_usd") or trade.get("size_usd") or 0.0, 0.0)

    entry_px_x10 = safe_float(trade.get("entry_price_x10") or 0.0, 0.0)
    entry_px_lit = safe_float(trade.get("entry_price_lighter") or 0.0, 0.0)

    # Prefer captured entry quantities, otherwise approximate from notional and entry price
    entry_qty_x10 = safe_float(trade.get("entry_qty_x10") or 0.0, 0.0)
    entry_qty_lit = safe_float(trade.get("entry_qty_lighter") or 0.0, 0.0)
    if entry_qty_x10 <= 0 and entry_px_x10 > 0 and notional > 0:
        entry_qty_x10 = notional / entry_px_x10
    if entry_qty_lit <= 0 and entry_px_lit > 0 and notional > 0:
        entry_qty_lit = notional / entry_px_lit

    # Funding: we store profit-positive net funding in trade.funding_collected (best-effort)
    funding_total = safe_float(trade.get("funding_collected") or 0.0, 0.0)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # H4: Fetch EXACT X10 PnL Breakdown from API (wenn verfügbar)
    # Gibt uns tradePnl, fundingFees, openFees, closeFees exakt von der Börse
    # ═══════════════════════════════════════════════════════════════════════════════
    x10_breakdown = None
    if x10 and hasattr(x10, "get_realised_pnl_breakdown"):
        try:
            # Wait a bit for position to be fully closed on X10 side
            await asyncio.sleep(0.5)
            x10_breakdown = await x10.get_realised_pnl_breakdown(symbol, limit=5)
            
            if x10_breakdown:
                # Use X10's EXACT funding if available (more accurate than our tracking)
                x10_funding = x10_breakdown.get("funding_fees", 0.0)
                if abs(x10_funding) > 0:
                    funding_diff = abs(funding_total - x10_funding)
                    if funding_diff > 0.001:  # More than $0.001 difference
                        logger.info(
                            f"📊 {symbol} Funding Correction: "
                            f"Bot tracked ${funding_total:.4f}, X10 API says ${x10_funding:.4f} "
                            f"(diff=${funding_diff:.4f}) - using X10 value"
                        )
                    funding_total = x10_funding
                    
        except Exception as e:
            logger.debug(f"{symbol}: X10 PnL breakdown fetch failed (non-critical): {e}")

    # Exit snapshots
    x10_exit_px = 0.0
    x10_exit_qty = 0.0
    x10_exit_fee_usd = 0.0
    if x10 and hasattr(x10, "get_last_close_price"):
        try:
            x10_exit_px, x10_exit_qty, x10_exit_fee_usd = x10.get_last_close_price(symbol)
            x10_exit_px = safe_float(x10_exit_px, 0.0)
            x10_exit_qty = safe_float(x10_exit_qty, 0.0)
            x10_exit_fee_usd = safe_float(x10_exit_fee_usd, 0.0)
        except Exception:
            pass

    # Lighter exit: prefer matching the close order id (set by trading.close_trade)
    lighter_exit_oid = trade.get("lighter_exit_order_id")
    entry_time_dt = parse_iso_time(trade.get("entry_time"))
    since_ts = entry_time_dt.timestamp() if entry_time_dt else None
    lit_stats = await _lighter_fill_stats_for_order(lighter, symbol, lighter_exit_oid, fallback_since_ts=since_ts)
    lit_exit_px = safe_float(lit_stats.get("price"), 0.0)
    lit_exit_qty = safe_float(lit_stats.get("qty"), 0.0)
    lit_exit_fee_usd = safe_float(lit_stats.get("fee"), 0.0)

    # Fallback prices if we couldn't find fills
    if x10_exit_px <= 0:
        try:
            x10_exit_px = safe_float(x10.fetch_mark_price(symbol), 0.0)
        except Exception:
            x10_exit_px = 0.0
    if lit_exit_px <= 0:
        try:
            lit_exit_px = safe_float(lighter.fetch_mark_price(symbol), 0.0)
        except Exception:
            lit_exit_px = 0.0

    # Pick quantities for PnL calc: prefer exit qty (actual), else entry qty
    qty_x10 = x10_exit_qty if x10_exit_qty > 0 else entry_qty_x10
    qty_lit = lit_exit_qty if lit_exit_qty > 0 else entry_qty_lit

    # Fees: use actual close fee for X10 if available; otherwise estimate from fee rates.
    fee_manager = get_fee_manager()

    # Entry fee estimates (rate-based)
    entry_fee_x10_rate = trade.get("entry_fee_x10")
    entry_fee_lit_rate = trade.get("entry_fee_lighter")
    try:
        entry_fees = float(
            fee_manager.calculate_trade_fees(
                notional, "X10", "LIGHTER",
                is_maker1=False, is_maker2=True,
                actual_fee1=entry_fee_x10_rate,
                actual_fee2=entry_fee_lit_rate,
            )
        )
    except Exception:
        entry_fees = 0.0

    # Exit fees: prefer actual close fees if we have them, else rate-based estimate
    exit_fee_x10_rate = trade.get("exit_fee_x10")
    exit_fee_lit_rate = trade.get("exit_fee_lighter")
    try:
        exit_fees_est = float(
            fee_manager.calculate_trade_fees(
                notional, "X10", "LIGHTER",
                is_maker1=False, is_maker2=False,
                actual_fee1=exit_fee_x10_rate,
                actual_fee2=exit_fee_lit_rate,
            )
        )
    except Exception:
        exit_fees_est = 0.0

    # Combine using best available per-leg fees
    exit_fees = x10_exit_fee_usd + lit_exit_fee_usd
    if exit_fees <= 0:
        exit_fees = exit_fees_est

    fees_total = safe_float(entry_fees, 0.0) + safe_float(exit_fees, 0.0)

    # Use compute_hedge_pnl for accurate, sign-correct calculation
    hedge_result = compute_hedge_pnl(
        symbol=symbol,
        lighter_side=trade.get("side_lighter", ""),
        x10_side=trade.get("side_x10", ""),
        lighter_entry_price=entry_px_lit,
        lighter_close_price=lit_exit_px,
        lighter_qty=qty_lit,
        x10_entry_price=entry_px_x10,
        x10_close_price=x10_exit_px,
        x10_qty=qty_x10,
        lighter_fees=entry_fees / 2 + lit_exit_fee_usd,  # Approximate split
        x10_fees=entry_fees / 2 + x10_exit_fee_usd,
        funding_collected=funding_total,
    )

    logger.info(
        f"📊 {symbol} Realized PnL: "
        f"lighter=${float(hedge_result['lighter_pnl']):.4f}, x10=${float(hedge_result['x10_pnl']):.4f}, "
        f"fees=${float(hedge_result['fee_total']):.4f}, funding=${funding_total:.4f}, "
        f"total=${float(hedge_result['total_pnl']):.4f}"
    )

    # ═══════════════════════════════════════════════════════════════════════════════
    # H4: Cross-check with X10's exact breakdown (if available)
    # ═══════════════════════════════════════════════════════════════════════════════
    if x10_breakdown:
        x10_exact_total = x10_breakdown.get("total_realised_pnl", 0.0)
        x10_exact_trade_pnl = x10_breakdown.get("trade_pnl", 0.0)
        x10_exact_fees = x10_breakdown.get("open_fees", 0.0) + x10_breakdown.get("close_fees", 0.0)
        
        # Compare our X10 leg calculation with their exact value
        our_x10_pnl = float(hedge_result["x10_pnl"])
        pnl_diff = abs(our_x10_pnl - x10_exact_trade_pnl)
        
        if pnl_diff > 0.01:  # More than $0.01 difference
            logger.warning(
                f"⚠️ {symbol} X10 PnL Mismatch: "
                f"Bot calculated ${our_x10_pnl:.4f}, X10 API says tradePnL=${x10_exact_trade_pnl:.4f} "
                f"(diff=${pnl_diff:.4f})"
            )
        else:
            logger.debug(f"✅ {symbol} X10 PnL verified: Bot=${our_x10_pnl:.4f} ≈ API=${x10_exact_trade_pnl:.4f}")

    # Convert Decimal values to float for return dict (API compatibility)
    return {
        "price_pnl_x10": float(hedge_result["x10_pnl"]),
        "price_pnl_lighter": float(hedge_result["lighter_pnl"]),
        "price_pnl_total": float(hedge_result["price_pnl_total"]),
        "fees_total": float(hedge_result["fee_total"]),
        "funding_total": funding_total,
        "total_pnl": float(hedge_result["total_pnl"]),
        "exit_price_x10": x10_exit_px,
        "exit_price_lighter": lit_exit_px,
        "exit_qty_x10": qty_x10,
        "exit_qty_lighter": qty_lit,
        "exit_fee_x10": x10_exit_fee_usd,
        "exit_fee_lighter": lit_exit_fee_usd,
        "x10_breakdown": x10_breakdown,  # H4: Include exact breakdown for audit
    }


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
                            ok, _ = await x10.close_live_position(sym, original_side, notional)
                            if ok:
                                logger.info(f"✅ Closed orphaned X10 {sym}")
                            else:
                                logger.warning(f"⚠️ Failed to close orphaned X10 {sym} (close_live_position returned False)")
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
                                ok, _ = await lighter.close_live_position(sym, original_side, notional)
                                if ok:
                                    logger.info(f"✅ Closed orphaned Lighter {sym}")
                                else:
                                    logger.warning(f"⚠️ Failed to close orphaned Lighter {sym} (close_live_position returned False)")
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
                            ok, _ = await x10.close_live_position(sym, original_side, size_usd)
                            if ok:
                                logger.info(f"✅ Closed X10 zombie {sym}")
                            else:
                                logger.warning(f"⚠️ Failed to close X10 zombie {sym} (close_live_position returned False)")
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
                                ok, _ = await lighter.close_live_position(sym, original_side, size_usd)
                                if ok:
                                    logger.info(f"✅ Closed Lighter zombie {sym}")
                                else:
                                    logger.warning(f"⚠️ Failed to close Lighter zombie {sym} (close_live_position returned False)")
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

    # Throttle: this is an expensive safety check (2x REST position fetch + DB scan).
    # Running it every logic-loop tick can overload the event loop and the APIs.
    global LAST_STRICT_RECONCILE_AT
    try:
        min_interval = float(getattr(config, 'STRICT_RECONCILE_MIN_INTERVAL_SECONDS', 30))
    except Exception:
        min_interval = 30.0
    now = time.time()
    if (now - LAST_STRICT_RECONCILE_AT) < min_interval:
        return
    LAST_STRICT_RECONCILE_AT = now
    
    try:
        # Skip if busy
        if parallel_exec and hasattr(parallel_exec, 'is_busy') and parallel_exec.is_busy():
            return

        # FIX: Cleanup old entries from RECENTLY_CLOSED_TRADES
        current_time = time.time()
        for sym in list(RECENTLY_CLOSED_TRADES.keys()):
            if current_time - RECENTLY_CLOSED_TRADES[sym] > RECENTLY_CLOSED_PROTECTION_SECONDS:
                RECENTLY_CLOSED_TRADES.pop(sym, None)

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
            if (not p.get('is_ghost')) and abs(safe_float(p.get('size', 0))) > 1e-8
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
        
        # Throttle noisy orphan/dust handling to avoid log storms and wasted cycles.
        # Stored on function object to persist across calls without globals.
        if not hasattr(reconcile_state_with_exchange, "_orphan_throttle_next_ts"):
            reconcile_state_with_exchange._orphan_throttle_next_ts = {}
        orphan_throttle_next_ts = reconcile_state_with_exchange._orphan_throttle_next_ts

        for symbol in all_exchange_symbols:
            if symbol not in db_symbols:
                if symbol in RECENTLY_OPENED_TRADES:
                    # Extended protection for POST_ONLY orders (see comment above)
                    if current_time - RECENTLY_OPENED_TRADES[symbol] < 120:  # Extended from 60s to 120s
                        continue

                # FIX: Skip recently closed trades (Lighter API can lag showing closed positions)
                if symbol in RECENTLY_CLOSED_TRADES:
                    if current_time - RECENTLY_CLOSED_TRADES[symbol] < RECENTLY_CLOSED_PROTECTION_SECONDS:
                        logger.debug(f"⏭️ Skipping {symbol}: Recently closed (grace period {RECENTLY_CLOSED_PROTECTION_SECONDS}s)")
                        continue

                l_size = real_lighter.get(symbol, 0)
                x_size = real_x10.get(symbol, 0)
                
                now_ts = time.time()
                next_allowed = float(orphan_throttle_next_ts.get(symbol, 0.0) or 0.0)
                if now_ts < next_allowed:
                    continue

                logger.error(f"👻 ORPHAN POSITION: {symbol} found (L={l_size}, X={x_size}) but NOT in DB!")
                orphan_throttle_next_ts[symbol] = now_ts + 30.0  # default backoff (overridden below)
                 
                # Fix #12: Automatically close orphan positions
                try:
                    # Close Lighter position if exists
                    if abs(l_size) > 0:
                        logger.warning(f"🚨 Closing orphan Lighter position {symbol} (size={l_size})...")
                        lighter_position = next((p for p in (lighter_pos or []) if p.get('symbol') == symbol), None)
                        if lighter_position:
                            if lighter_position.get('is_ghost'):
                                logger.debug(f"👻 Skipping orphan close for {symbol}: Ghost Guardian synthetic position")
                                lighter_position = None
                        if lighter_position:
                            px = safe_float(lighter.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(l_size) * px
                                original_side = "BUY" if l_size < 0 else "SELL"
                                # Avoid infinite loops on uncloseable dust lots
                                if lighter_position.get('is_dust') and not getattr(config, 'IS_SHUTTING_DOWN', False):
                                    logger.warning(
                                        f"🧹 Orphan Lighter {symbol} is dust (notional≈${notional:.4f}) - skipping close attempt (throttled)"
                                    )
                                    orphan_throttle_next_ts[symbol] = now_ts + 600.0  # 10m backoff
                                else:
                                    ok, _ = await lighter.close_live_position(symbol, original_side, notional)
                                    if ok:
                                        logger.info(f"✅ Closed orphaned Lighter {symbol}")
                                    else:
                                        logger.warning(f"⚠️ Failed to close orphaned Lighter {symbol} (close_live_position returned False)")
                                    orphan_throttle_next_ts[symbol] = now_ts + 30.0
                    
                    # Close X10 position if exists
                    if abs(x_size) > 0:
                        logger.warning(f"🚨 Closing orphan X10 position {symbol} (size={x_size})...")
                        x10_position = next((p for p in (x10_pos or []) if p.get('symbol') == symbol), None)
                        if x10_position:
                            px = safe_float(x10.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(x_size) * px
                                original_side = "BUY" if x_size < 0 else "SELL"
                                ok, _ = await x10.close_live_position(symbol, original_side, notional)
                                if ok:
                                    logger.info(f"✅ Closed orphaned X10 {symbol}")
                                else:
                                    logger.warning(f"⚠️ Failed to close orphaned X10 {symbol} (close_live_position returned False)")
                                orphan_throttle_next_ts[symbol] = now_ts + 30.0
                except Exception as e:
                    logger.error(f"❌ Failed to close orphan position {symbol}: {e}")
                    orphan_throttle_next_ts[symbol] = now_ts + 60.0
                    
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
            # CRITICAL: Skip trades that are not fully hedged
            # Prevents BASIS_CLOSING/PRICE_DIVERGENCE triggers on partial/pending trades.
            # A trade is only considered "open & manageable" if BOTH legs are filled.
            # ═══════════════════════════════════════════════════════════════
            x10_order_id = t.get('x10_order_id')
            lighter_order_id = t.get('lighter_order_id')

            if not x10_order_id or not lighter_order_id:
                logger.debug(
                    f"⏭️ Skipping {sym}: Not fully hedged "
                    f"(x10_id={x10_order_id}, lighter_id={lighter_order_id})"
                )
                continue

            # ═══════════════════════════════════════════════════════════════
            # Data sanitizing for all numeric fields
            # FIX: Accept both 'notional_usd' and 'size_usd' field names
            # DB uses 'size_usd', but some code paths use 'notional_usd'
            # ═══════════════════════════════════════════════════════════════
            notional = t.get('notional_usd') or t.get('size_usd') or t.get('size_usd_decimal')
            notional = float(notional) if notional is not None else 0.0

            init_funding = t.get('initial_funding_rate_hourly')
            init_funding = float(init_funding) if init_funding is not None else 0.0
            
            # ═══════════════════════════════════════════════════════════════
            # PNL FIX: Extract Lighter position with REAL PnL data
            # The Lighter API provides accurate unrealized_pnl, realized_pnl,
            # avg_entry_price, and total_funding_paid values
            # ═══════════════════════════════════════════════════════════════
            lighter_position = None
            for pos in p_lit:
                if pos.get('symbol') == sym:
                    lighter_position = pos
                    break
            
            # Log if we have real Lighter PnL data
            if lighter_position and lighter_position.get('unrealized_pnl', 0.0) != 0.0:
                logger.debug(
                    f"📊 {sym} Lighter Position: uPnL=${lighter_position.get('unrealized_pnl', 0):.4f}, "
                    f"funding=${lighter_position.get('total_funding_paid', 0):.4f}, "
                    f"entry=${lighter_position.get('avg_entry_price', 0):.6f}"
                )
            
            # Calculate age
            age_seconds, hold_hours = calculate_trade_age(t)
            
            logger.debug(f"Check {sym}: Age={age_seconds:.1f}s (Limit={getattr(config, 'FARM_HOLD_SECONDS', 3600)}s)")
            
            # ═══════════════════════════════════════════════════════════════
            # Get prices for PnL calculation
            # ═══════════════════════════════════════════════════════════════
            # FIX: Use Orderbook Mid-Price instead of Mark Price if available
            # Mark Price can lag or be manipulated, leading to false PnL.
            # Mid Price (avg of best bid/ask) reflects real liquidity.
            # ═══════════════════════════════════════════════════════════════
            
            # X10 Price
            raw_px = None
            if hasattr(x10, 'get_orderbook_mid_price'):
                raw_px = await x10.get_orderbook_mid_price(sym)
            if raw_px is None or raw_px <= 0:
                raw_px = x10.fetch_mark_price(sym)
            
            # Lighter Price
            raw_pl = None
            if hasattr(lighter, 'get_orderbook_mid_price'):
                raw_pl = await lighter.get_orderbook_mid_price(sym)
            if raw_pl is None or raw_pl <= 0:
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

            # ═══════════════════════════════════════════════════════════════
            # Strategy PnL ESTIMATE (for exit decisions only)
            # - Funding: depends on which side we hold on each exchange (BUY=long, SELL=short)
            # - Price/Basis: depends on long/short legs (NOT absolute spread!)
            # ═══════════════════════════════════════════════════════════════
            sx = _side_sign(t.get("side_x10"))
            sl = _side_sign(t.get("side_lighter"))

            # Funding per hour (profit-positive):
            # funding_cashflow = -(sign * rate) * notional
            if sx != 0 and sl != 0:
                current_net = -((sx * rx) + (sl * rl))
            else:
                # Fallback for older records missing sides
                base_net = rl - rx
                current_net = -base_net if t.get('leg1_exchange') == 'X10' else base_net
            
            funding_pnl_est = current_net * hold_hours * notional
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Use ACTUAL collected funding from FundingTracker if available
            # The estimate (current_net * hours) assumes constant rate, which is often wrong.
            # FundingTracker provides the sum of actual payments received.
            # ═══════════════════════════════════════════════════════════════
            funding_collected = float(t.get('funding_collected') or 0.0)
            
            if abs(funding_collected) > 0.0001:
                funding_pnl = funding_collected
                # Add accrued estimate for current hour? (Optional, keeping it simple for now)
            else:
                funding_pnl = funding_pnl_est

            # Price/Basis PnL estimate (profit-positive)
            ep_x10 = float(t.get('entry_price_x10') or px)
            ep_lit = float(t.get('entry_price_lighter') or pl)
            basis_entry = ep_lit - ep_x10
            basis_curr = pl - px
            qty_est = (notional / px) if px > 0 else 0.0

            # Common hedge configurations:
            # - long X10 / short Lighter => PnL ~= qty * (basis_entry - basis_curr)
            # - short X10 / long Lighter => PnL ~= qty * (basis_curr - basis_entry)
            if sx == 1 and sl == -1:
                spread_pnl = qty_est * (basis_entry - basis_curr)
            elif sx == -1 and sl == 1:
                spread_pnl = qty_est * (basis_curr - basis_entry)
            else:
                # Best-effort fallback: assume long X10 / short Lighter shape
                spread_pnl = qty_est * (basis_entry - basis_curr)

            current_spread_pct = abs(basis_curr) / px if px > 0 else 0.0

            # Gross PnL (before fees)
            gross_pnl = funding_pnl + spread_pnl
            
            # ═══════════════════════════════════════════════════════════════
            # DEBUG LOGGING: Show exactly what the bot sees
            # ═══════════════════════════════════════════════════════════════
            if abs(gross_pnl) > 0.1:
                logger.info(
                    f"🔍 {sym} PnL Check: "
                    f"X10=${px:.5f}, Lit=${pl:.5f} | "
                    f"SpreadPnL=${spread_pnl:.4f}, Funding=${funding_pnl:.4f} | "
                    f"Gross=${gross_pnl:.4f}"
                )
            
            # ═══════════════════════════════════════════════════════════════
            # ADVANCED EXIT OPTIMIZATION (16.12.2025)
            # Dynamische Slippage-Berechnung aus echtem Orderbook
            # ═══════════════════════════════════════════════════════════════
            use_dynamic_slippage = getattr(config, 'USE_DYNAMIC_SLIPPAGE', True)
            dynamic_slippage_cost = 0.0
            actual_spread_pct = 0.0
            orderbook_depth_ok = True
            
            if use_dynamic_slippage:
                try:
                    # Hole echte Spreads aus beiden Orderbooks
                    x10_spread_pct = 0.0
                    lighter_spread_pct = 0.0
                    
                    # X10 Orderbook Spread
                    if hasattr(x10, 'fetch_orderbook'):
                        x10_ob = await x10.fetch_orderbook(sym, limit=5)
                        if x10_ob and x10_ob.get('bids') and x10_ob.get('asks'):
                            x10_best_bid = float(x10_ob['bids'][0][0])
                            x10_best_ask = float(x10_ob['asks'][0][0])
                            if x10_best_bid > 0 and x10_best_ask > 0:
                                x10_spread_pct = (x10_best_ask - x10_best_bid) / x10_best_bid
                                # Check Depth für unsere Trade-Größe
                                x10_depth = sum(float(b[0]) * float(b[1]) for b in x10_ob['bids'][:5])
                                min_depth = getattr(config, 'MIN_ORDERBOOK_DEPTH_USD', 100.0)
                                if x10_depth < min_depth:
                                    orderbook_depth_ok = False
                                    logger.debug(f"⚠️ {sym} X10: Niedrige Liquidität ${x10_depth:.0f} < ${min_depth:.0f}")
                    
                    # Lighter Orderbook Spread
                    if hasattr(lighter, 'fetch_orderbook'):
                        lit_ob = await lighter.fetch_orderbook(sym, limit=5)
                        if lit_ob and lit_ob.get('bids') and lit_ob.get('asks'):
                            lit_best_bid = float(lit_ob['bids'][0][0])
                            lit_best_ask = float(lit_ob['asks'][0][0])
                            if lit_best_bid > 0 and lit_best_ask > 0:
                                lighter_spread_pct = (lit_best_ask - lit_best_bid) / lit_best_bid
                                # Check Depth
                                lit_depth = sum(float(b[0]) * float(b[1]) for b in lit_ob['bids'][:5])
                                if lit_depth < min_depth:
                                    orderbook_depth_ok = False
                                    logger.debug(f"⚠️ {sym} Lighter: Niedrige Liquidität ${lit_depth:.0f} < ${min_depth:.0f}")
                    
                    # Kombinierter Exit-Spread (beide Seiten müssen gekreuzt werden)
                    actual_spread_pct = (x10_spread_pct + lighter_spread_pct) / 2
                    
                    # Slippage = halber Spread pro Seite (wir kreuzen den Spread beim Exit)
                    dynamic_slippage_cost = notional * actual_spread_pct * 0.5
                    
                    if actual_spread_pct > 0:
                        logger.debug(
                            f"📊 {sym} Dynamic Slippage: X10={x10_spread_pct*100:.3f}%, "
                            f"Lighter={lighter_spread_pct*100:.3f}%, Combined=${dynamic_slippage_cost:.4f}"
                        )
                        
                except Exception as e:
                    logger.debug(f"Dynamic slippage calc error: {e}, using fallback")
                    actual_spread_pct = 0.0
            
            # Calculate Net PnL with proper fees
            # PNL FIX: Pass lighter_position to use REAL Lighter API PnL data
            try:
                fee_manager = get_fee_manager()
                net_pnl_decimal = await calculate_realized_pnl(
                    t, fee_manager, gross_pnl, lighter_position=lighter_position
                )
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
                
                # ═══════════════════════════════════════════════════════════════
                # SMART SLIPPAGE: Use dynamic if available, else fallback
                # ═══════════════════════════════════════════════════════════════
                if dynamic_slippage_cost > 0:
                    slippage_cost = dynamic_slippage_cost
                else:
                    # Fallback zu statischem Buffer
                    slippage_buffer_pct = getattr(config, 'EXIT_SLIPPAGE_BUFFER_PCT', 0.0015)
                    slippage_cost = notional * slippage_buffer_pct
                
                # Safety Margin auf Exit-Kosten
                exit_cost_safety = getattr(config, 'EXIT_COST_SAFETY_MARGIN', 1.1)
                slippage_cost *= exit_cost_safety
                
                total_pnl -= slippage_cost
                
            except Exception as e:
                logger.debug(f"FeeManager error, using fallback: {e}")
                fee_x10 = getattr(config, 'TAKER_FEE_X10', 0.000225)
                fee_lit_entry = getattr(config, 'MAKER_FEE_LIGHTER', 0.0)
                fee_lit_exit = getattr(config, 'TAKER_FEE_LIGHTER', 0.0)
                est_fees = notional * (fee_x10 * 2.0 + fee_lit_entry + fee_lit_exit)
                total_pnl = gross_pnl - est_fees

            # ═══════════════════════════════════════════════════════════════
            # PROFITABILITY FIX (16.12.2025): MINIMUM HOLD TIME + FUNDING CHECK
            # Problem: 89.4% der Trades bekamen NULL Funding (zu schnell geschlossen)
            # Lösung: Mindestens 2h halten + Mindest-Funding vor Exit
            # ═══════════════════════════════════════════════════════════════
            min_profit_exit = getattr(config, 'MIN_PROFIT_EXIT_USD', 0.10)
            max_hold_hours = getattr(config, 'MAX_HOLD_HOURS', 72.0)
            min_maintenance_apy = getattr(config, 'MIN_MAINTENANCE_APY', 0.20)
            minimum_hold_seconds = getattr(config, 'MINIMUM_HOLD_SECONDS', 7200)  # 2h default
            min_funding_before_exit = getattr(config, 'MIN_FUNDING_BEFORE_EXIT_USD', 0.03)
            
            # CROSS-EXCHANGE ARBITRAGE: Preisdifferenz-Profit nutzen!
            price_divergence_enabled = getattr(config, 'PRICE_DIVERGENCE_EXIT_ENABLED', True)
            min_divergence_profit = getattr(config, 'MIN_PRICE_DIVERGENCE_PROFIT_USD', 0.50)
            # Calculate Current APY
            # current_net is hourly rate. APY = rate * 24 * 365
            current_apy = current_net * 24 * 365
            
            # ═══════════════════════════════════════════════════════════════
            # NEW (2025-12-17 Audit): Volatility Panic Check
            # ═══════════════════════════════════════════════════════════════
            reason = None
            force_close = False
            try:
                vol_monitor = get_volatility_monitor()
                # Update with current price
                vol_monitor.update_price(sym, px)
                
                if vol_monitor.should_close_due_to_volatility(sym):
                    logger.warning(
                        f"🚨 PANIC CLOSE {sym}: Extreme Volatility! "
                        f"Regime: {vol_monitor.current_regimes.get(sym, 'UNKNOWN')}"
                    )
                    reason = "VOLATILITY_PANIC"
                    force_close = True
            except Exception as vol_err:
                logger.debug(f"Volatility check error for {sym}: {vol_err}")

            # ═══════════════════════════════════════════════════════════════
            # BASIS EXIT ENGINE (DegeniusQ): Wait for basis to close
            # + basis stop-loss to avoid funding trades bleeding on price.
            # Uses basis_entry/basis_curr computed above (profit-positive spread_pnl).
            # ═══════════════════════════════════════════════════════════════
            if not reason:
                try:
                    basis_exit_enabled = getattr(config, "BASIS_EXIT_ENABLED", True)
                    basis_close_fraction = float(getattr(config, "BASIS_CLOSE_FRACTION", 0.20))
                    basis_target = float(getattr(config, "BASIS_EXIT_TARGET_USD", 0.0))
                    basis_stop_loss_usd = float(getattr(config, "BASIS_STOP_LOSS_USD", 0.50))

                    if basis_exit_enabled and basis_close_fraction > 0 and basis_close_fraction < 1:
                        # Exit when the remaining basis is <= fraction of entry basis (towards target).
                        if sx == 1 and sl == -1 and (basis_entry - basis_target) > 0:
                            threshold = basis_target + (basis_entry - basis_target) * basis_close_fraction
                            if basis_curr <= threshold:
                                reason = f"BASIS_CLOSING (basis={basis_curr:.6f}<=thr={threshold:.6f})"
                        elif sx == -1 and sl == 1 and (basis_target - basis_entry) > 0:
                            threshold = basis_target - (basis_target - basis_entry) * basis_close_fraction
                            if basis_curr >= threshold:
                                reason = f"BASIS_CLOSING (basis={basis_curr:.6f}>=thr={threshold:.6f})"

                    # BASIS_STOP_LOSS: DISABLED
                    # Reason: Bei gehedgten Trades ist negative SpreadPnL normal (Execution Slippage, Basis Mean-Reversion)
                    # Der Hedge schützt uns vor Preis-Risiko. Wir wollen nur profitieren von:
                    # 1. Funding Collection (Hauptziel)
                    # 2. Positive Preisdifferenz (PRICE_DIVERGENCE_PROFIT)
                    # Stop-Loss würde profitable Trades zu früh schließen.
                except Exception as basis_err:
                    logger.debug(f"{sym}: Basis exit engine error: {basis_err}")

            # 1. Force close criteria (Time, Safety)
            if not reason:
                # Du MUSST mindestens 2h halten um Funding zu bekommen!
                # ═══════════════════════════════════════════════════════════════
                if age_seconds < minimum_hold_seconds and not getattr(config, 'IS_SHUTTING_DOWN', False):
                    # Ausnahme: Bei sehr hohem Preisdifferenz-Profit trotzdem schließen
                    if price_divergence_enabled and spread_pnl >= min_divergence_profit:
                        logger.info(
                            f"💎🎁 [PRICE DIVERGENCE] {sym}: Spread PnL ${spread_pnl:.2f} >= ${min_divergence_profit:.2f} - "
                            f"Geschenkter Profit durch Preisdifferenz! (ignoriere MINIMUM_HOLD)"
                        )
                        reason = "PRICE_DIVERGENCE_PROFIT"
                        # Do NOT bypass profit-protection: this is only a candidate reason.
                        # Net-profit after exit costs is validated below.
                        force_close = False
                    else:
                        remaining_hold = (minimum_hold_seconds - age_seconds) / 60
                        logger.debug(
                            f"⏰ [HODL] {sym}: Minimum Hold nicht erreicht ({age_seconds/60:.0f}min < {minimum_hold_seconds/60:.0f}min). "
                            f"Noch {remaining_hold:.0f}min warten für Funding!"
                        )
                        continue
            
                # ═══════════════════════════════════════════════════════════════
                # NEU: FUNDING CHECK - Mindestens etwas Funding gesammelt?
                # ═══════════════════════════════════════════════════════════════
                if funding_collected < min_funding_before_exit and not force_close:
                    if not getattr(config, 'IS_SHUTTING_DOWN', False):
                        # Ausnahme: Bei Preisdifferenz-Profit trotzdem erlauben
                        if price_divergence_enabled and spread_pnl >= min_divergence_profit:
                            logger.info(
                                f"💎🎁 [PRICE DIVERGENCE] {sym}: Spread PnL ${spread_pnl:.2f} kompensiert fehlendes Funding"
                            )
                        else:
                            logger.debug(
                                f"💸 [HODL] {sym}: Noch kein Funding (${funding_collected:.4f} < ${min_funding_before_exit:.2f}). "
                                f"Warte auf nächstes Funding Payment!"
                            )
                            continue
                
                # ═══════════════════════════════════════════════════════════════
                # PROFIT PROTECTION: Nur Exit wenn wirklich profitabel!
                # Verhindert Verluste durch zu frühe Exits
                # ═══════════════════════════════════════════════════════════════
                require_positive = getattr(config, 'REQUIRE_POSITIVE_EXPECTED_PNL', True)
                min_net_profit = getattr(config, 'MIN_NET_PROFIT_EXIT_USD', 0.05)
                
                # Berechne erwarteten Net-Profit nach allen Kosten
                # FIX (2025-12-17): Convert Decimal to float to prevent TypeError
                est_fees_f = float(est_fees) if hasattr(est_fees, '__float__') else est_fees
                slippage_cost_f = float(slippage_cost) if hasattr(slippage_cost, '__float__') else slippage_cost
                expected_exit_cost = est_fees_f + slippage_cost_f
                expected_net_pnl = gross_pnl - expected_exit_cost
                
                if require_positive and not force_close and not getattr(config, 'IS_SHUTTING_DOWN', False):
                    # Check: Ist der Trade nach allen Kosten wirklich profitabel?
                    if expected_net_pnl < min_net_profit:
                        logger.debug(
                            f"🛡️ [PROFIT PROTECTION] {sym}: Expected Net ${expected_net_pnl:.4f} < ${min_net_profit:.2f}. "
                            f"Halte Trade bis profitabel! (Gross=${gross_pnl:.4f}, Costs=${expected_exit_cost:.4f})"
                        )
                        continue
                
                # ═══════════════════════════════════════════════════════════════
                # LIQUIDITY CHECK: Genug Orderbook-Tiefe für Exit?
                # ═══════════════════════════════════════════════════════════════
                smart_exit = getattr(config, 'SMART_EXIT_ENABLED', True)
                if smart_exit and not orderbook_depth_ok and not force_close:
                    logger.warning(
                        f"⚠️ [LOW LIQUIDITY] {sym}: Orderbook zu dünn für ${notional:.0f} Exit. "
                        f"Warte auf bessere Liquidität..."
                    )
                    # Trotzdem Exit erlauben wenn Trade sehr profitabel oder sehr alt
                    if total_pnl < notional * 0.02 and hold_hours < max_hold_hours * 0.8:
                        continue
            
            if not reason:
                # 1. Safety override: MAX_HOLD_HOURS
                if hold_hours >= max_hold_hours:
                    reason = "MAX_HOLD_EXPIRED"
                    force_close = True
                    logger.warning(
                        f"⚠️ [FORCE CLOSE] {sym}: Max hold time reached "
                        f"({hold_hours:.1f}h >= {max_hold_hours}h)"
                    )
                
                # ═══════════════════════════════════════════════════════════════
                # NEU: CROSS-EXCHANGE ARBITRAGE EXIT
                # Wenn du durch Preisdifferenz schon im Plus bist = GESCHENKTER PROFIT!
                # ═══════════════════════════════════════════════════════════════
                if not force_close and price_divergence_enabled:
                    if spread_pnl >= min_divergence_profit:
                        reason = "PRICE_DIVERGENCE_PROFIT"
                        logger.info(
                            f"💎🎁 [GESCHENKT!] {sym}: Preisdifferenz-Profit ${spread_pnl:.2f}! "
                            f"Closing für geschenkten Gewinn (unabhängig von Funding)"
                        )
                
                # 2. Profit check
                if not force_close and not reason:
                    if total_pnl < min_profit_exit:
                        logger.debug(
                            f"💎 [HODL] {sym}: Net PnL ${total_pnl:.4f} < ${min_profit_exit:.2f}"
                        )
                        # Skip 'continue' here to allow other reason checks? 
                        # No, usually if not profitable we wait.
                    else:
                        logger.info(
                            f"💰 [PROFIT] {sym}: Net PnL ${total_pnl:.4f} >= ${min_profit_exit:.2f} | "
                            f"Funding=${funding_collected:.4f}, SpreadPnL=${spread_pnl:.4f}"
                        )

                        # Smart Rotation: Exit if APY drops below maintenance threshold
                        if not reason and current_apy < min_maintenance_apy:
                            reason = f"LOW_APY_EXIT ({current_apy*100:.1f}% < {min_maintenance_apy*100:.1f}%)"
                            logger.info(f"📉 [SMART ROTATION] {sym}: APY dropped to {current_apy*100:.1f}%. Exiting to free capital.")
                        
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
                    # After close: compute REAL realized PnL from entry/exit fills.
                    # This is what gets persisted to DB (accounting truth).
                    try:
                        # Best-effort: fetch exit fee rates (if order ids are available)
                        if t.get("x10_exit_order_id") and hasattr(x10, "get_order_fee"):
                            try:
                                t["exit_fee_x10"] = safe_float(await x10.get_order_fee(str(t["x10_exit_order_id"])), 0.0)
                            except Exception:
                                pass
                        if t.get("lighter_exit_order_id") and hasattr(lighter, "get_order_fee"):
                            try:
                                t["exit_fee_lighter"] = safe_float(await lighter.get_order_fee(str(t["lighter_exit_order_id"])), 0.0)
                            except Exception:
                                pass

                        realized = await calculate_realized_close_pnl(t, lighter, x10)
                    except Exception as e:
                        logger.warning(f"{sym}: Realized PnL calc failed, using estimate. err={e}")
                        realized = {
                            "total_pnl": total_pnl,
                            "funding_total": safe_float(t.get("funding_collected") or funding_pnl, 0.0),
                            "price_pnl_total": spread_pnl,
                            "fees_total": est_fees,
                        }

                    realized_total = safe_float(realized.get("total_pnl"), total_pnl)
                    realized_funding = safe_float(realized.get("funding_total"), safe_float(t.get("funding_collected") or 0.0, 0.0))
                    realized_price = safe_float(realized.get("price_pnl_total"), spread_pnl)
                    realized_fees = safe_float(realized.get("fees_total"), est_fees)

                    await close_trade_in_state(sym, pnl=realized_total, funding=realized_funding)
                    await archive_trade_to_history(t, reason, {
                        'total_net_pnl': realized_total,
                        'funding_pnl': realized_funding,
                        'spread_pnl': realized_price,
                        'fees': realized_fees
                    })

                    # FIX: Track recently closed trades to avoid orphan false positives
                    RECENTLY_CLOSED_TRADES[sym] = time.time()

                    # Telegram notification
                    telegram = get_telegram_bot()
                    if telegram.enabled:
                        await telegram.send_trade_alert(sym, reason, notional, realized_total)

        except Exception as e:
            logger.error(f"Trade Loop Error for {t.get('symbol', 'UNKNOWN')}: {e}")
            import traceback
            logger.debug(traceback.format_exc())

