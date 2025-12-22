# src/core/state.py
"""
State management and database wrappers.

This module handles:
- Trade state management (open/close trades)
- Position caching
- Execution locks
- DB archiving
"""

import asyncio
import logging
import time
import aiosqlite
from datetime import datetime
from typing import Dict, Optional, List, Any, Tuple
from decimal import Decimal
import csv
import os

import config
from src.utils import safe_float, safe_decimal, quantize_usd
from src.core.interfaces import StateManagerInterface, TradeState, TradeStatus

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared state)
# ============================================================
_state_manager: Optional[StateManagerInterface] = None
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 10.0
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
SYMBOL_LOCKS = {}


def set_state_manager(sm: StateManagerInterface):
    """Set the global state manager instance (Dependency Injection)"""
    global _state_manager
    _state_manager = sm


async def get_state_manager() -> StateManagerInterface:
    """Get the global state manager instance."""
    global _state_manager
    if _state_manager is None:
        raise RuntimeError("StateManager not initialized. Call set_state_manager() first.")
    return _state_manager


# ============================================================
# STATE MANAGER ACCESS
# ============================================================
def get_local_state_manager():
    """Return the global state manager instance (sync helper)"""
    return state_manager


async def get_open_trades() -> List[Dict[str, Any]]:
    """Get open trades from state manager."""
    sm = await get_state_manager()
    trades = await sm.get_all_open_trades()
    return [t.to_dict() if hasattr(t, 'to_dict') else t for t in trades]


async def add_trade_to_state(trade_data: Dict[str, Any]) -> str:
    """Add trade to in-memory state using Decimal for financial fields."""
    sm = await get_state_manager()
    
    # ═══════════════════════════════════════════════════════════════
    # FIX: Accept both 'size_usd' and 'notional_usd' field names
    # ═══════════════════════════════════════════════════════════════
    size_usd = safe_decimal(trade_data.get('size_usd') or trade_data.get('notional_usd') or 0)
    
    status_raw = trade_data.get("status", TradeStatus.OPEN)
    if isinstance(status_raw, str):
        try:
            status_raw = TradeStatus(status_raw)
        except ValueError:
            status_raw = TradeStatus.OPEN

    trade = TradeState(
        symbol=trade_data['symbol'],
        side_x10=trade_data.get('side_x10', 'BUY'),
        side_lighter=trade_data.get('side_lighter', 'SELL'),
        size_usd=float(size_usd),
        entry_price_x10=float(safe_decimal(trade_data.get('entry_price_x10', 0))),
        entry_price_lighter=float(safe_decimal(trade_data.get('entry_price_lighter', 0))),
        status=status_raw,
        is_farm_trade=trade_data.get('is_farm_trade', False),
        account_label=trade_data.get('account_label', 'Main'),
        x10_order_id=trade_data.get('x10_order_id'),
        lighter_order_id=trade_data.get('lighter_order_id'),
    )
    return await sm.add_trade(trade)


async def close_trade_in_state(symbol: str, pnl: Decimal = Decimal('0'), funding: Decimal = Decimal('0')):
    """Close trade in state using Decimal."""
    sm = await get_state_manager()
    
    logger.info(f"📝 close_trade_in_state({symbol}): PnL=${float(pnl):.4f}, Funding=${float(funding):.4f}")
    
    await sm.close_trade(symbol, pnl, funding)
    logger.info(f"✅ Trade {symbol} closed in state")


async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    """Archive closed trade to trade_history table"""
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
            
            # ═══════════════════════════════════════════════════════════════
            # CSV LOGGING (Added/Restored)
            # ═══════════════════════════════════════════════════════════════
            try:
                await _append_to_realized_pnl_csv(trade_data, pnl_data)
            except Exception as e:
                logger.error(f"Failed to write realized_pnl.csv: {e}")

    except Exception as e:
        logger.error(f"Archive Error: {e}")
        return False


async def _append_to_realized_pnl_csv(trade_data: Dict, pnl_data: Dict):
    """
    Append closed trade to realized_pnl.csv for detailed audit.
    
    Columns: market, size, entry_price, exit_price, trade_pnl, funding_fees, trading_fees, realised_pnl, closed_at
    
    Note: 'trade_pnl' in CSV context usually means 'Net Price PnL' (Price PnL - Fees),
    so that Realized PnL = Trade PnL + Funding Fees.
    """
    file_path = "realized_pnl.csv"
    
    # Calculate derived values
    # price_pnl_total (spread_pnl) is Gross Price PnL.
    # fees is Total Fees.
    # funding_pnl is Net Funding.
    # total_net_pnl is Realized PnL.
    
    spread_pnl = float(pnl_data.get('spread_pnl', 0.0))
    fees = float(pnl_data.get('fees', 0.0))
    funding = float(pnl_data.get('funding_pnl', 0.0))
    total_realized = float(pnl_data.get('total_net_pnl', 0.0))
    
    # trade_pnl = Price PnL - Fees (so matching detailed_analysis logic)
    trade_pnl = spread_pnl - fees
    
    # Prices
    entry_price = float(trade_data.get('entry_price_lighter') or trade_data.get('entry_price_x10') or 0.0)
    # Estimate exit price if not present (simple inversion of PnL logic not possible without size/side)
    exit_price = 0.0 
    
    # Row construction
    row = {
        'market': trade_data.get('symbol', 'UNKNOWN'),
        'size': trade_data.get('size_usd') or trade_data.get('notional_usd') or 0.0,
        'entry_price': f"{entry_price:.6f}",
        'exit_price': f"{exit_price:.6f}",
        'trade_pnl': f"{trade_pnl:.6f}",
        'funding_fees': f"{funding:.6f}",
        'trading_fees': f"{fees:.6f}",
        'realised_pnl': f"{total_realized:.6f}",
        'closed_at': datetime.utcnow().isoformat()
    }
    
    file_exists = os.path.isfile(file_path)
    
    # Run in executor to avoid blocking event loop with file I/O
    await asyncio.get_event_loop().run_in_executor(
        None, 
        lambda: _write_csv_row(file_path, row, file_exists)
    )

def _write_csv_row(file_path: str, row: Dict, file_exists: bool):
    """Sync helper for CSV writing"""
    fieldnames = [
        'market', 'size', 'entry_price', 'exit_price', 
        'trade_pnl', 'funding_fees', 'trading_fees', 
        'realised_pnl', 'closed_at'
    ]
    
    with open(file_path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ============================================================
# POSITION CACHING
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
        
        return p_x10, p_lit
        
    except asyncio.TimeoutError:
        logger.error("Position fetch timeout, using stale cache")
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    except Exception as e:
        logger.error(f"Position cache error: {e}")
        return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])


# ============================================================
# LOCKS
# ============================================================
async def get_execution_lock(symbol: str) -> asyncio.Lock:
    """Get or create execution lock for symbol"""
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]


def get_symbol_lock(symbol: str) -> asyncio.Lock:
    """Thread-safe / async-safe Lock pro Symbol"""
    if symbol not in SYMBOL_LOCKS:
        SYMBOL_LOCKS[symbol] = asyncio.Lock()
    return SYMBOL_LOCKS[symbol]


# ============================================================
# EXPOSURE CHECK
# ============================================================
def _safe_to_decimal(value) -> Decimal:
    """Safely convert any value to Decimal without relying on external imports."""
    if value is None:
        return Decimal('0')
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal('0')


async def check_total_exposure(
    x10_adapter,
    lighter_adapter,
    new_trade_size=None
) -> Tuple[bool, Decimal, Decimal]:
    """Prüft, ob die Gesamtexposure das erlaubte Maximum überschreitet.

    Alle Berechnungen erfolgen konsequent in ``Decimal``. Bei fehlender Balance
    wird der Trade blockiert statt auf magische Fallbacks zu setzen.

    Args:
        x10_adapter: Adapter für X10-Balance
        lighter_adapter: Adapter für Lighter-Balance
        new_trade_size: Notional des neuen Trades (USD) – Decimal bevorzugt

    Returns:
        Tuple[bool, Decimal, Decimal]: (kann_handeln, aktuelle_Leverage, max_Leverage)
    """
    try:
        # Convert new_trade_size to Decimal safely
        new_trade_size_dec = _safe_to_decimal(new_trade_size)
        
        # Get current open trades
        open_trades = await get_open_trades()
        
        # Calculate current exposure using safe conversion
        current_exposure = Decimal('0')
        for t in open_trades:
            size_val = t.get('size_usd') or t.get('notional_usd') or 0
            current_exposure += _safe_to_decimal(size_val)
        
        # Total capital = X10 + Lighter (user goal is portfolio-level ROI).
        x10_balance = await x10_adapter.get_available_balance()
        lighter_balance = await lighter_adapter.get_available_balance()

        # Safely convert balances
        x10_balance = _safe_to_decimal(x10_balance)
        lighter_balance = _safe_to_decimal(lighter_balance)

        total_balance = x10_balance + lighter_balance
        if total_balance <= 0:
            logger.error("No available balance on either exchange; blocking new trades")
            max_leverage_cfg = _safe_to_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 5))
            return False, Decimal('0'), max_leverage_cfg if max_leverage_cfg > 0 else Decimal('0')

        # Calculate exposure with new trade
        new_total_exposure = current_exposure + new_trade_size_dec

        # CALCULATE LEVERAGE
        leverage_ratio = new_total_exposure / total_balance
        current_leverage = leverage_ratio.quantize(Decimal('0.0001'))
        max_leverage = _safe_to_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 5))
        if max_leverage <= 0:
            max_leverage = Decimal('1')

        can_trade = current_leverage <= max_leverage

        if not can_trade:
            logger.warning(
                "⚠️ EXPOSURE LIMIT: Leverage "
                f"{current_leverage}x > Max {max_leverage}x "
                f"(Exp: ${new_total_exposure}, Bal: ${total_balance})"
            )
        else:
            logger.debug(f"✅ Exposure Check: {current_leverage}x <= {max_leverage}x")

        return can_trade, current_leverage, max_leverage

    except Exception as e:
        logger.error(f"Exposure check failed: {e}", exc_info=True)
        return False, Decimal('999'), _safe_to_decimal(getattr(config, 'LEVERAGE_MULTIPLIER', 5))
