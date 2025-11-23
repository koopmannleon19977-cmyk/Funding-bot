# src/parallel_execution.py
import asyncio
import logging
from typing import Optional, Tuple
from decimal import Decimal

logger = logging.getLogger(__name__)


class ParallelExecutionManager:
    """Manages parallel trade execution with optimistic rollback"""
    
    def __init__(self, x10_adapter, lighter_adapter):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.execution_locks = {}  # symbol -> Lock
    
    async def execute_trade_parallel(
        self,
        symbol: str,
        side_x10: str,
        side_lighter: str,
        size_x10: Decimal,
        size_lighter: Decimal,
        price_x10: Optional[Decimal] = None,
        price_lighter: Optional[Decimal] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute both legs in parallel with rollback on failure
        
        Returns:
            (success, x10_order_id, lighter_order_id)
        """
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()
        
        async with self.execution_locks[symbol]:
            try:
                # Fire both orders simultaneously
                x10_task = asyncio.create_task(
                    self.x10.open_live_position(symbol, side_x10, float(size_x10), post_only=True)
                )
                lighter_task = asyncio.create_task(
                    self.lighter.open_live_position(symbol, side_lighter, float(size_lighter), post_only=False)
                )
                
                # Wait for both
                results = await asyncio.gather(x10_task, lighter_task, return_exceptions=True)
                x10_result, lighter_result = results
                
                # Check success
                x10_success = not isinstance(x10_result, Exception) and x10_result[0]
                lighter_success = not isinstance(lighter_result, Exception) and lighter_result[0]
                
                x10_order = x10_result[1] if x10_success else None
                lighter_order = lighter_result[1] if lighter_success else None
                
                if x10_success and lighter_success:
                    logger.info(f"[PARALLEL] Both legs filled for {symbol}")
                    return True, x10_order, lighter_order
                
                # Rollback logic
                if x10_success and not lighter_success:
                    logger.warning(f"[ROLLBACK] Lighter failed, closing X10 for {symbol}")
                    await self._rollback_x10(symbol, side_x10, size_x10)
                    return False, None, None
                
                if lighter_success and not x10_success:
                    logger.warning(f"[ROLLBACK] X10 failed, closing Lighter for {symbol}")
                    await self._rollback_lighter(symbol, side_lighter, size_lighter)
                    return False, None, None
                
                # Both failed
                logger.error(f"[PARALLEL] Both legs failed for {symbol}")
                return False, None, None
                
            except Exception as e:
                logger.error(f"[PARALLEL] Exception: {e}")
                return False, None, None
    
    async def _rollback_x10(self, symbol: str, side: str, size: Decimal):
        """Close X10 position immediately"""
        try:
            # Market close opposite side
            opposite_side = "SELL" if side == "BUY" else "BUY"
            await self.x10.close_live_position(symbol, opposite_side, float(size))
            logger.info(f"X10 rollback executed for {symbol}")
        except Exception as e:
            logger.error(f"X10 rollback failed: {e}")
    
    async def _rollback_lighter(self, symbol: str, side: str, size: Decimal):
        """Close Lighter position immediately"""
        try:
            opposite_side = "SELL" if side == "BUY" else "BUY"
            await self.lighter.close_live_position(symbol, opposite_side, float(size))
            logger.info(f"Lighter rollback executed for {symbol}")
        except Exception as e:
            logger.error(f"Lighter rollback failed: {e}")