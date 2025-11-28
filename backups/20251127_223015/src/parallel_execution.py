# src/parallel_execution.py - PUNKT 1: PARALLEL EXECUTION MIT OPTIMISTIC ROLLBACK

import asyncio
import logging
from typing import Optional, Tuple, Dict
from decimal import Decimal
from enum import Enum
import time

logger = logging.getLogger(__name__)


class ExecutionState(Enum):
    PENDING = "PENDING"
    LEG1_SENT = "LEG1_SENT"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SENT = "LEG2_SENT"
    COMPLETE = "COMPLETE"
    ROLLBACK_NEEDED = "ROLLBACK_NEEDED"
    ROLLBACK_DONE = "ROLLBACK_DONE"
    FAILED = "FAILED"


class TradeExecution:
    """Tracks state of a single parallel trade execution"""
    __slots__ = (
        'symbol', 'state', 'x10_order_id', 'lighter_order_id',
        'x10_filled', 'lighter_filled', 'start_time', 'error'
    )

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = ExecutionState.PENDING
        self.x10_order_id: Optional[str] = None
        self.lighter_order_id: Optional[str] = None
        self.x10_filled = False
        self.lighter_filled = False
        self.start_time = time.monotonic()
        self.error: Optional[str] = None


class ParallelExecutionManager:
    """
    Manages parallel trade execution with optimistic rollback.
    
    Strategy:
    1. Send Lighter POST-ONLY (maker) first - 100ms head start
    2. Send X10 MARKET (taker) immediately after
    3. If one leg fails -> immediate rollback of successful leg
    4. State machine tracks execution for recovery
    """

    def __init__(self, x10_adapter, lighter_adapter):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.execution_locks: Dict[str, asyncio.Lock] = {}
        self.active_executions: Dict[str, TradeExecution] = {}
        self._rollback_queue: asyncio.Queue = asyncio.Queue()
        self._rollback_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start background rollback processor"""
        if self._rollback_task is None or self._rollback_task.done():
            self._rollback_task = asyncio.create_task(self._rollback_processor())
            logger.info("✅ ParallelExecutionManager: Rollback processor started")

    async def stop(self):
        """Stop background tasks gracefully"""
        if self._rollback_task and not self._rollback_task.done():
            self._rollback_task.cancel()
            try:
                await asyncio.wait_for(self._rollback_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._rollback_task = None
        logger.info("✅ ParallelExecutionManager: Stopped")

    async def _rollback_processor(self):
        """Background task that processes rollback queue"""
        while True:
            try:
                execution = await self._rollback_queue.get()
                if execution is None:
                    break
                await self._execute_rollback(execution)
                self._rollback_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Rollback processor error: {e}")
                await asyncio.sleep(1.0)

    async def execute_trade_parallel(
        self,
        symbol: str,
        side_x10: str,
        side_lighter: str,
        size_x10: Decimal,
        size_lighter: Decimal,
        price_x10: Optional[Decimal] = None,
        price_lighter: Optional[Decimal] = None,
        timeout: float = 10.0
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute hedged trade on both exchanges in parallel.
        
        Returns:
            (success, x10_order_id, lighter_order_id)
        """
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()

        async with self.execution_locks[symbol]:
            execution = TradeExecution(symbol)
            self.active_executions[symbol] = execution

            try:
                # ═══════════════════════════════════════════════════════════
                # PHASE 1: LIGHTER POST-ONLY (Maker) - 100ms HEAD START
                # ═══════════════════════════════════════════════════════════
                logger.info(f"🔄 [PARALLEL] {symbol}: Lighter POST-ONLY first")
                execution.state = ExecutionState.LEG1_SENT

                lighter_task = asyncio.create_task(
                    self._execute_lighter_leg(
                        symbol, side_lighter, float(size_lighter), post_only=True
                    )
                )

                # 100ms head start for Lighter to place maker order
                await asyncio.sleep(0.1)

                # ═══════════════════════════════════════════════════════════
                # PHASE 2: X10 MARKET (Taker) - FILLS IMMEDIATELY
                # ═══════════════════════════════════════════════════════════
                logger.info(f"🔄 [PARALLEL] {symbol}: X10 MARKET order")
                execution.state = ExecutionState.LEG2_SENT

                x10_task = asyncio.create_task(
                    self._execute_x10_leg(
                        symbol, side_x10, float(size_x10), post_only=False
                    )
                )

                # ═══════════════════════════════════════════════════════════
                # PHASE 3: WAIT FOR BOTH WITH TIMEOUT
                # ═══════════════════════════════════════════════════════════
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(lighter_task, x10_task, return_exceptions=True),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.error(f"⏰ [PARALLEL] {symbol}: Execution timeout!")
                    lighter_task.cancel()
                    x10_task.cancel()
                    execution.state = ExecutionState.ROLLBACK_NEEDED
                    execution.error = "Timeout"
                    await self._queue_rollback(execution)
                    return False, None, None

                lighter_result, x10_result = results

                # Parse results
                lighter_success, lighter_order = self._parse_result(lighter_result)
                x10_success, x10_order = self._parse_result(x10_result)

                execution.lighter_order_id = lighter_order
                execution.x10_order_id = x10_order
                execution.lighter_filled = lighter_success
                execution.x10_filled = x10_success

                # ═══════════════════════════════════════════════════════════
                # PHASE 4: EVALUATE & ROLLBACK IF NEEDED
                # ═══════════════════════════════════════════════════════════
                if lighter_success and x10_success:
                    execution.state = ExecutionState.COMPLETE
                    elapsed = (time.monotonic() - execution.start_time) * 1000
                    logger.info(f"✅ [PARALLEL] {symbol}: Both legs filled in {elapsed:.0f}ms")
                    return True, x10_order, lighter_order

                # One or both failed - need rollback
                execution.state = ExecutionState.ROLLBACK_NEEDED

                if lighter_success and not x10_success:
                    execution.error = f"X10 failed: {x10_result}"
                    logger.error(f"🔄 [ROLLBACK] {symbol}: X10 failed, rolling back Lighter")
                    await self._queue_rollback(execution)
                    return False, None, lighter_order

                if x10_success and not lighter_success:
                    execution.error = f"Lighter failed: {lighter_result}"
                    logger.error(f"🔄 [ROLLBACK] {symbol}: Lighter failed, rolling back X10")
                    await self._queue_rollback(execution)
                    return False, x10_order, None

                # Both failed
                execution.state = ExecutionState.FAILED
                execution.error = f"Both failed: X10={x10_result}, Lighter={lighter_result}"
                logger.error(f"❌ [PARALLEL] {symbol}: Both legs failed")
                return False, None, None

            except Exception as e:
                execution.state = ExecutionState.FAILED
                execution.error = str(e)
                logger.error(f"❌ [PARALLEL] {symbol}: Exception: {e}")
                return False, None, None
            finally:
                # Cleanup after delay
                asyncio.get_event_loop().call_later(
                    60.0, lambda s=symbol: self.active_executions.pop(s, None)
                )

    async def _execute_lighter_leg(
        self, symbol: str, side: str, notional_usd: float, post_only: bool
    ) -> Tuple[bool, Optional[str]]:
        """Execute Lighter leg with error handling"""
        try:
            return await self.lighter.open_live_position(
                symbol, side, notional_usd, post_only=post_only
            )
        except Exception as e:
            logger.error(f"Lighter leg error {symbol}: {e}")
            return False, None

    async def _execute_x10_leg(
        self, symbol: str, side: str, notional_usd: float, post_only: bool
    ) -> Tuple[bool, Optional[str]]:
        """Execute X10 leg with error handling"""
        try:
            return await self.x10.open_live_position(
                symbol, side, notional_usd, post_only=post_only
            )
        except Exception as e:
            logger.error(f"X10 leg error {symbol}: {e}")
            return False, None

    def _parse_result(self, result) -> Tuple[bool, Optional[str]]:
        """Parse execution result, handling exceptions"""
        if isinstance(result, Exception):
            return False, None
        if isinstance(result, tuple) and len(result) >= 2:
            return bool(result[0]), result[1]
        return False, None

    async def _queue_rollback(self, execution: TradeExecution):
        """Queue execution for background rollback"""
        await self._rollback_queue.put(execution)

    async def _execute_rollback(self, execution: TradeExecution):
        """Execute rollback for failed trade"""
        symbol = execution.symbol
        logger.info(f"🔄 [ROLLBACK] Processing {symbol}...")

        # Wait for order settlement
        await asyncio.sleep(3.0)

        try:
            if execution.lighter_filled and not execution.x10_filled:
                await self._rollback_lighter(execution)
            elif execution.x10_filled and not execution.lighter_filled:
                await self._rollback_x10(execution)

            execution.state = ExecutionState.ROLLBACK_DONE
            logger.info(f"✅ [ROLLBACK] {symbol}: Complete")

        except Exception as e:
            logger.error(f"❌ [ROLLBACK] {symbol}: Failed: {e}")
            execution.error = f"Rollback failed: {e}"

    async def _rollback_x10(self, execution: TradeExecution):
        """Rollback X10 position"""
        symbol = execution.symbol

        for attempt in range(3):
            try:
                await asyncio.sleep(2.0 * (attempt + 1))

                positions = await self.x10.fetch_open_positions()
                pos = next(
                    (p for p in (positions or []) 
                     if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                    None
                )

                if not pos:
                    logger.info(f"✓ X10 Rollback {symbol}: No position found")
                    return

                actual_size = pos.get('size', 0)
                # Positive = LONG (opened with BUY), Negative = SHORT (opened with SELL)
                original_side = "BUY" if actual_size > 0 else "SELL"
                close_size = abs(actual_size)

                logger.info(
                    f"→ X10 Rollback {symbol}: size={actual_size:.6f}, side={original_side}"
                )

                success, _ = await self.x10.close_live_position(
                    symbol, original_side, close_size
                )

                if success:
                    logger.info(f"✓ X10 rollback {symbol}: Success")
                    return
                else:
                    logger.warning(f"✗ X10 rollback {symbol}: Attempt {attempt+1} failed")

            except Exception as e:
                logger.error(f"✗ X10 rollback {symbol} attempt {attempt+1}: {e}")

        logger.error(f"❌ X10 rollback {symbol}: All attempts failed!")

    async def _rollback_lighter(self, execution: TradeExecution):
        """Rollback Lighter position"""
        symbol = execution.symbol

        for attempt in range(3):
            try:
                await asyncio.sleep(2.0 * (attempt + 1))

                positions = await self.lighter.fetch_open_positions()
                pos = next(
                    (p for p in (positions or [])
                     if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                    None
                )

                if not pos:
                    logger.info(f"✓ Lighter Rollback {symbol}: No position found")
                    return

                actual_size = pos.get('size', 0)
                original_side = "BUY" if actual_size > 0 else "SELL"
                close_size_coins = abs(actual_size)

                # Lighter needs USD notional
                mark_price = self.lighter.fetch_mark_price(symbol)
                if not mark_price or mark_price <= 0:
                    logger.error(f"✗ Lighter rollback {symbol}: No price")
                    continue

                notional_usd = close_size_coins * mark_price

                logger.info(
                    f"→ Lighter Rollback {symbol}: "
                    f"size={actual_size:.6f} @ ${mark_price:.2f} = ${notional_usd:.2f}"
                )

                success, _ = await self.lighter.close_live_position(
                    symbol, original_side, notional_usd
                )

                if success:
                    logger.info(f"✓ Lighter rollback {symbol}: Success")
                    return
                else:
                    logger.warning(f"✗ Lighter rollback {symbol}: Attempt {attempt+1} failed")

            except Exception as e:
                logger.error(f"✗ Lighter rollback {symbol} attempt {attempt+1}: {e}")

        logger.error(f"❌ Lighter rollback {symbol}: All attempts failed!")

    def get_execution_stats(self) -> Dict:
        """Return current execution statistics"""
        states = {}
        for ex in self.active_executions.values():
            state_name = ex.state.value
            states[state_name] = states.get(state_name, 0) + 1
        return {
            "active_executions": len(self.active_executions),
            "states": states,
            "pending_rollbacks": self._rollback_queue.qsize()
        }