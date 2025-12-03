# src/fee_tracker.py
"""
Fee Tracker - Fetches actual fees from API for each trade
"""
import logging
import asyncio
from typing import Optional, Dict, Any
from src.adapters.x10_adapter import X10Adapter
from src.adapters.lighter_adapter import LighterAdapter
from src.state_manager import get_state_manager

logger = logging.getLogger(__name__)


async def fetch_entry_fees(
    symbol: str,
    x10_order_id: Optional[str],
    lighter_order_id: Optional[str],
    x10_adapter: X10Adapter,
    lighter_adapter: LighterAdapter,
    max_retries: int = 3,
    delay_seconds: float = 2.0
) -> Dict[str, Optional[float]]:
    """
    Fetch actual entry fees from both exchanges after order fill.
    
    Args:
        symbol: Trade symbol
        x10_order_id: X10 order ID
        lighter_order_id: Lighter order ID
        x10_adapter: X10 adapter instance
        lighter_adapter: Lighter adapter instance
        max_retries: Maximum retry attempts
        delay_seconds: Delay between retries
        
    Returns:
        Dict with 'x10_fee' and 'lighter_fee' (None if failed)
    """
    result = {'x10_fee': None, 'lighter_fee': None}
    
    # Fetch X10 fee
    if x10_order_id:
        for attempt in range(max_retries):
            try:
                fee = await x10_adapter.get_order_fee(x10_order_id)
                if fee is not None and fee >= 0:
                    result['x10_fee'] = fee
                    logger.debug(f"✅ {symbol}: X10 entry fee = {fee:.6f} (order: {x10_order_id})")
                    break
                elif attempt < max_retries - 1:
                    await asyncio.sleep(delay_seconds * (attempt + 1))
            except Exception as e:
                logger.debug(f"⚠️ {symbol}: X10 fee fetch attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay_seconds * (attempt + 1))
    
    # Fetch Lighter fee
    if lighter_order_id:
        for attempt in range(max_retries):
            try:
                fee = await lighter_adapter.get_order_fee(lighter_order_id)
                if fee is not None and fee >= 0:
                    result['lighter_fee'] = fee
                    logger.debug(f"✅ {symbol}: Lighter entry fee = {fee:.6f} (order: {lighter_order_id})")
                    break
                elif attempt < max_retries - 1:
                    await asyncio.sleep(delay_seconds * (attempt + 1))
            except Exception as e:
                logger.debug(f"⚠️ {symbol}: Lighter fee fetch attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay_seconds * (attempt + 1))
    
    return result


async def fetch_exit_fees(
    symbol: str,
    x10_order_id: Optional[str],
    lighter_order_id: Optional[str],
    x10_adapter: X10Adapter,
    lighter_adapter: LighterAdapter,
    max_retries: int = 3,
    delay_seconds: float = 2.0
) -> Dict[str, Optional[float]]:
    """
    Fetch actual exit fees from both exchanges after order fill.
    
    Same as fetch_entry_fees but for exit orders.
    """
    return await fetch_entry_fees(
        symbol, x10_order_id, lighter_order_id,
        x10_adapter, lighter_adapter, max_retries, delay_seconds
    )


async def update_trade_entry_fees(
    symbol: str,
    x10_order_id: Optional[str],
    lighter_order_id: Optional[str],
    x10_adapter: X10Adapter,
    lighter_adapter: LighterAdapter
) -> bool:
    """
    Fetch and update entry fees for a trade.
    
    Returns:
        True if at least one fee was successfully fetched
    """
    try:
        fees = await fetch_entry_fees(
            symbol, x10_order_id, lighter_order_id,
            x10_adapter, lighter_adapter
        )
        
        if fees['x10_fee'] is not None or fees['lighter_fee'] is not None:
            state_manager = await get_state_manager()
            await state_manager.update_trade(symbol, {
                'entry_fee_x10': fees['x10_fee'],
                'entry_fee_lighter': fees['lighter_fee']
            })
            
            total_fee = (fees['x10_fee'] or 0.0) + (fees['lighter_fee'] or 0.0)
            x10_fee_str = f"{fees['x10_fee']:.6f}" if fees['x10_fee'] is not None else "N/A"
            lighter_fee_str = f"{fees['lighter_fee']:.6f}" if fees['lighter_fee'] is not None else "N/A"
            logger.info(
                f"💰 {symbol}: Entry fees updated - "
                f"X10={x10_fee_str}, "
                f"Lighter={lighter_fee_str}, "
                f"Total=${total_fee:.4f}"
            )
            return True
        
        return False
    except Exception as e:
        logger.warning(f"⚠️ Failed to update entry fees for {symbol}: {e}")
        return False


async def update_trade_exit_fees(
    symbol: str,
    x10_order_id: Optional[str],
    lighter_order_id: Optional[str],
    x10_adapter: X10Adapter,
    lighter_adapter: LighterAdapter
) -> bool:
    """
    Fetch and update exit fees for a trade.
    
    Returns:
        True if at least one fee was successfully fetched
    """
    try:
        fees = await fetch_exit_fees(
            symbol, x10_order_id, lighter_order_id,
            x10_adapter, lighter_adapter
        )
        
        if fees['x10_fee'] is not None or fees['lighter_fee'] is not None:
            state_manager = await get_state_manager()
            await state_manager.update_trade(symbol, {
                'exit_fee_x10': fees['x10_fee'],
                'exit_fee_lighter': fees['lighter_fee']
            })
            
            total_fee = (fees['x10_fee'] or 0.0) + (fees['lighter_fee'] or 0.0)
            x10_fee_str = f"{fees['x10_fee']:.6f}" if fees['x10_fee'] is not None else "N/A"
            lighter_fee_str = f"{fees['lighter_fee']:.6f}" if fees['lighter_fee'] is not None else "N/A"
            logger.info(
                f"💰 {symbol}: Exit fees updated - "
                f"X10={x10_fee_str}, "
                f"Lighter={lighter_fee_str}, "
                f"Total=${total_fee:.4f}"
            )
            return True
        
        return False
    except Exception as e:
        logger.warning(f"⚠️ Failed to update exit fees for {symbol}: {e}")
        return False

