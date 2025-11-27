# src/parallel_execution.py - Kompletter Ersatz

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
        self.execution_locks = {}
    
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
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()
        
        async with self.execution_locks[symbol]:
            try:
                # Fire both legs simultaneously (Gemini + Claude)
                x10_task = asyncio.create_task(
                    self.x10.open_live_position(symbol, side_x10, float(size_x10), post_only=True)
                )
                lighter_task = asyncio.create_task(
                    self.lighter.open_live_position(symbol, side_lighter, float(size_lighter), post_only=False)
                )
                
                # Use return_exceptions to catch failures (Gemini)
                results = await asyncio.gather(x10_task, lighter_task, return_exceptions=True)
                x10_result, lighter_result = results
                
                # Check for exceptions (Gemini)
                x10_success = not isinstance(x10_result, Exception) and x10_result[0]
                lighter_success = not isinstance(lighter_result, Exception) and lighter_result[0]
                
                x10_order = x10_result[1] if x10_success else None
                lighter_order = lighter_result[1] if lighter_success else None
                
                if x10_success and lighter_success:
                    logger.info(f" [PARALLEL] Both legs filled for {symbol}")
                    return True, x10_order, lighter_order
                
                # AGGRESSIVE ROLLBACK (Gemini - verhindert Zombies)
                if x10_success and not lighter_success:
                    logger.error(f" [ROLLBACK] Lighter failed, CLOSING X10 immediately for {symbol}")
                    await asyncio.sleep(0.5)
                    await self._rollback_x10(symbol, side_x10, size_x10)
                    return False, x10_order, None
                
                if lighter_success and not x10_success:
                    logger.error(f" [ROLLBACK] X10 failed, CLOSING Lighter immediately for {symbol}")
                    await asyncio.sleep(0.5)
                    await self._rollback_lighter(symbol, side_lighter, size_lighter)
                    return False, None, None
                
                logger.error(f" [PARALLEL] Both legs failed for {symbol}")
                return False, None, None
                
            except Exception as e:
                logger.error(f" [PARALLEL] Exception: {e}")
                return False, None, None
    
    async def _rollback_x10(self, symbol: str, original_side: str, size: Decimal):
        try:
            # Wait longer for position to fully settle
            await asyncio.sleep(5.0)

            positions = await self.x10.fetch_open_positions()
            has_pos = any(p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8 for p in (positions or []))

            if not has_pos:
                logger.info(f"✓ X10 Rollback skipped: No position for {symbol}")
                return

            # CRITICAL FIX: Get actual position from exchange
            actual_pos = next(p for p in positions if p.get('symbol') == symbol)
            actual_size = actual_pos.get('size', 0)

            # Determine ORIGINAL side based on current position
            # Positive size = LONG (opened with BUY) → original_side = "BUY"
            # Negative size = SHORT (opened with SELL) → original_side = "SELL"
            if actual_size > 0:
                original_side_corrected = "BUY"
            else:
                original_side_corrected = "SELL"

            # ═══════════════════════════════════════════════════════════════
            # CRITICAL FIX: Use ACTUAL position size, not notional USD!
            # ═══════════════════════════════════════════════════════════════
            actual_size_abs = abs(actual_size)

            logger.info(
                f"→ X10 Rollback {symbol}: "
                f"actual_size={actual_size:.6f} coins, "
                f"side={original_side_corrected}, "
                f"requested_notional=${float(size):.2f}"
            )

            # Pass actual coin size, NOT notional USD
            success, _ = await self.x10.close_live_position(
                symbol,
                original_side_corrected,
                actual_size_abs  # Coins – korrekt!
            )

            if success:
                logger.info(f"✓ X10 rollback executed for {symbol} ({actual_size_abs:.6f} coins)")
            else:
                logger.error(f"✗ X10 rollback FAILED for {symbol}")

        except Exception as e:
            logger.error(f"✗ X10 rollback exception for {symbol}: {e}")

    async def _rollback_lighter(self, symbol: str, original_side: str, size: Decimal):
        try:
            # Wait longer for position to fully settle
            await asyncio.sleep(5.0)

            positions = await self.lighter.fetch_open_positions()
            has_pos = any(p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8 for p in (positions or []))

            if not has_pos:
                logger.info(f"✓ Lighter Rollback skipped: No position for {symbol}")
                return

            # CRITICAL FIX: Get actual position from exchange
            actual_pos = next(p for p in positions if p.get('symbol') == symbol)
            actual_size = actual_pos.get('size', 0)

            # Determine ORIGINAL side based on current position
            if actual_size > 0:
                original_side_corrected = "BUY"
            else:
                original_side_corrected = "SELL"

            # ═══════════════════════════════════════════════════════════════
            # CRITICAL FIX: Use ACTUAL position size, not notional USD!
            # ═══════════════════════════════════════════════════════════════
            actual_size_abs = abs(actual_size)

            logger.info(
                f"→ Lighter Rollback {symbol}: "
                f"actual_size={actual_size:.6f} coins, "
                f"side={original_side_corrected}, "
                f"requested_notional=${float(size):.2f}"
            )

            # Pass actual coin size, NOT notional USD
            success, _ = await self.lighter.close_live_position(
                symbol,
                original_side_corrected,
                actual_size_abs  # Coins – korrekt!
            )

            if success:
                logger.info(f"✓ Lighter rollback executed for {symbol} ({actual_size_abs:.6f} coins)")
            else:
                logger.error(f"✗ Lighter rollback FAILED for {symbol}")

        except Exception as e:
            logger.error(f"✗ Lighter rollback exception for {symbol}: {e}")