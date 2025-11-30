# src/parallel_execution. py - PUNKT 1: PARALLEL EXECUTION MIT OPTIMISTIC ROLLBACK
# ═══════════════════════════════════════════════════════════════════════════════
# FEATURES:
# ✓ State Machine für Trade Tracking
# ✓ Background Rollback Queue (non-blocking)
# ✓ Retry-Logik mit Exponential Backoff
# ✓ Atomic Symbol-Level Locks
# ✓ Execution Statistics für Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ExecutionState(Enum):
    """State machine states for trade execution tracking"""
    PENDING = "PENDING"
    LEG1_SENT = "LEG1_SENT"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SENT = "LEG2_SENT"
    COMPLETE = "COMPLETE"
    PARTIAL_FILL = "PARTIAL_FILL"
    ROLLBACK_QUEUED = "ROLLBACK_QUEUED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_DONE = "ROLLBACK_DONE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    FAILED = "FAILED"


@dataclass
class TradeExecution:
    """Tracks state of a single parallel trade execution"""
    symbol: str
    state: ExecutionState = ExecutionState. PENDING
    x10_order_id: Optional[str] = None
    lighter_order_id: Optional[str] = None
    x10_filled: bool = False
    lighter_filled: bool = False
    x10_fill_size: float = 0.0
    lighter_fill_size: float = 0.0
    start_time: float = field(default_factory=time.monotonic)
    error: Optional[str] = None
    rollback_attempts: int = 0
    side_x10: str = ""
    side_lighter: str = ""
    size_x10: float = 0.0
    size_lighter: float = 0.0

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_time) * 1000


class ParallelExecutionManager:
    """
    Manages parallel trade execution with optimistic rollback. 
    
    ═══════════════════════════════════════════════════════════════════════════
    EXECUTION STRATEGY (Pre-Hedge):
    ═══════════════════════════════════════════════════════════════════════════
    1. Send Lighter POST-ONLY (maker) first - 100ms head start for order book
    2. Send X10 MARKET (taker) immediately after - fills instantly
    3. If one leg fails -> queue background rollback (non-blocking)
    4. State machine tracks execution for recovery & monitoring
    ═══════════════════════════════════════════════════════════════════════════
    """
    
    MAX_ROLLBACK_ATTEMPTS = 3
    ROLLBACK_BASE_DELAY = 2.0  # seconds
    EXECUTION_TIMEOUT = 15.0   # seconds

    def __init__(self, x10_adapter, lighter_adapter):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.execution_locks: Dict[str, asyncio.Lock] = {}
        self. active_executions: Dict[str, TradeExecution] = {}
        self._rollback_queue: asyncio.Queue[Optional[TradeExecution]] = asyncio.Queue()
        self._rollback_task: Optional[asyncio.Task] = None
        self._stats = {
            "total_executions": 0,
            "successful": 0,
            "failed": 0,
            "rollbacks_triggered": 0,
            "rollbacks_successful": 0,
            "rollbacks_failed": 0,
        }

    async def start(self):
        """Start background rollback processor"""
        if self._rollback_task is None or self._rollback_task.done():
            self._rollback_task = asyncio.create_task(
                self._rollback_processor(), 
                name="rollback_processor"
            )
            logger.info("✅ ParallelExecutionManager: Rollback processor started")

    async def stop(self):
        """Stop background tasks gracefully"""
        if self._rollback_task and not self._rollback_task. done():
            # Signal shutdown
            await self._rollback_queue.put(None)
            try:
                await asyncio.wait_for(self._rollback_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._rollback_task.cancel()
                try:
                    await self._rollback_task
                except asyncio.CancelledError:
                    pass
        self._rollback_task = None
        logger.info("✅ ParallelExecutionManager: Stopped")

    async def _rollback_processor(self):
        """Background task that processes rollback queue with retry logic"""
        logger.info("🔄 Rollback processor running...")
        
        while True:
            try:
                execution = await self._rollback_queue.get()
                
                # Shutdown signal
                if execution is None:
                    logger.info("🛑 Rollback processor: Shutdown signal received")
                    break
                
                execution.state = ExecutionState. ROLLBACK_IN_PROGRESS
                success = await self._execute_rollback_with_retry(execution)
                
                if success:
                    execution.state = ExecutionState.ROLLBACK_DONE
                    self._stats["rollbacks_successful"] += 1
                else:
                    execution.state = ExecutionState.ROLLBACK_FAILED
                    self._stats["rollbacks_failed"] += 1
                
                self._rollback_queue.task_done()
                
            except asyncio.CancelledError:
                logger.info("🛑 Rollback processor: Cancelled")
                break
            except Exception as e:
                logger.error(f"❌ Rollback processor error: {e}", exc_info=True)
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
        timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute hedged trade on both exchanges in parallel. 
        
        Returns:
            (success, x10_order_id, lighter_order_id)
        """
        # (Removed temporary debug block for type inspection)
        
        timeout = timeout or self. EXECUTION_TIMEOUT
        
        # Ensure symbol-level lock exists
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()

        async with self. execution_locks[symbol]:
            # Create execution tracker
            execution = TradeExecution(
                symbol=symbol,
                side_x10=side_x10,
                side_lighter=side_lighter,
                size_x10=float(size_x10),
                size_lighter=float(size_lighter),
            )
            self.active_executions[symbol] = execution
            self._stats["total_executions"] += 1

            try:
                result = await self._execute_parallel_internal(
                    execution, timeout
                )
                
                if result[0]:
                    self._stats["successful"] += 1
                else:
                    self._stats["failed"] += 1
                
                return result

            except Exception as e:
                execution.state = ExecutionState. FAILED
                execution.error = str(e)
                self._stats["failed"] += 1
                logger.error(f"❌ [PARALLEL] {symbol}: Exception: {e}", exc_info=True)
                return False, None, None
            
            finally:
                # Schedule cleanup after 60s
                asyncio.get_event_loop(). call_later(
                    60.0, 
                    lambda s=symbol: self. active_executions. pop(s, None)
                )

    async def _verify_execution(
        self,
        symbol: str,
        size_x10_expected: float,
        size_lighter_expected: float
    ) -> Tuple[bool, bool, bool]:
        """
        Verify both exchanges have actual positions after order placement.
        
        Returns:
            (both_ok, x10_has_position, lighter_has_position)
        """
        logger.info(f"🔍 [VERIFY] {symbol}: Waiting 2s for matching engines...")
        await asyncio.sleep(2.0)
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # ROBUST VERIFICATION WITH RETRIES
        # Exchanges sometimes return empty/stale data - retry to confirm ghost trades
        # ═══════════════════════════════════════════════════════════════════════════════
        max_retries = 2
        x10_has_position = False
        lighter_has_position = False
        
        for attempt in range(max_retries):
            try:
                # Fetch X10 positions
                x10_positions = await self.x10.fetch_open_positions()
                
                # Validate response type
                if not isinstance(x10_positions, list):
                    logger.warning(f"⚠️ [VERIFY] {symbol}: X10 returned invalid type: {type(x10_positions)}")
                    x10_positions = []
                
                x10_pos = next(
                    (p for p in (x10_positions or [])
                     if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                    None
                )
                
                if x10_pos:
                    actual_size = abs(x10_pos.get('size', 0))
                    x10_has_position = actual_size > 1e-8
                    logger.info(f"🔍 [VERIFY] {symbol}: X10 position size={actual_size:.6f}")
                    break  # Position found, no need to retry
                else:
                    if attempt < max_retries - 1:
                        logger.debug(f"🔍 [VERIFY] {symbol}: X10 no position (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(1.0)
                    else:
                        logger.warning(f"⚠️ [VERIFY] {symbol}: X10 returned no position after {max_retries} attempts")
                    
            except Exception as e:
                logger.error(f"❌ [VERIFY] {symbol}: X10 fetch error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)
        
        for attempt in range(max_retries):
            try:
                # Fetch Lighter positions
                lighter_positions = await self.lighter.fetch_open_positions()
                
                # Validate response type
                if not isinstance(lighter_positions, list):
                    logger.warning(f"⚠️ [VERIFY] {symbol}: Lighter returned invalid type: {type(lighter_positions)}")
                    lighter_positions = []
                
                lighter_pos = next(
                    (p for p in (lighter_positions or [])
                     if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                    None
                )
                
                if lighter_pos:
                    actual_size = abs(lighter_pos.get('size', 0))
                    lighter_has_position = actual_size > 1e-8
                    logger.info(f"🔍 [VERIFY] {symbol}: Lighter position size={actual_size:.6f}")
                    break  # Position found, no need to retry
                else:
                    if attempt < max_retries - 1:
                        logger.debug(f"🔍 [VERIFY] {symbol}: Lighter no position (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(1.0)
                    else:
                        logger.warning(f"⚠️ [VERIFY] {symbol}: Lighter returned no position after {max_retries} attempts")
                    
            except Exception as e:
                logger.error(f"❌ [VERIFY] {symbol}: Lighter fetch error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # CLASSIFICATION
        # ═══════════════════════════════════════════════════════════════════════════════
        both_ok = x10_has_position and lighter_has_position
        
        if both_ok:
            logger.info(f"✅ [VERIFY] {symbol}: Both exchanges confirmed")
        elif x10_has_position and not lighter_has_position:
            logger.error(f"🚨 [VERIFY] {symbol}: GHOST TRADE - X10 has position, Lighter doesn't!")
            logger.error(f"   This indicates Lighter order failed or was rejected")
        elif lighter_has_position and not x10_has_position:
            logger.error(f"🚨 [VERIFY] {symbol}: GHOST TRADE - Lighter has position, X10 doesn't!")
            logger.error(f"   This indicates X10 order failed or was rejected")
        else:
            logger.error(f"🚨 [VERIFY] {symbol}: TOTAL FAILURE - Neither exchange has position")
            logger.error(f"   Both orders failed - this should trigger rollback")
        
        return both_ok, x10_has_position, lighter_has_position

    async def _execute_parallel_internal(
        self, 
        execution: TradeExecution,
        timeout: float
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Internal parallel execution logic"""
        symbol = execution.symbol
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: LIGHTER POST-ONLY (Maker) - 100ms HEAD START
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"🔄 [PARALLEL] {symbol}: Lighter POST-ONLY first")
        execution.state = ExecutionState.LEG1_SENT

        lighter_task = asyncio.create_task(
            self._execute_lighter_leg(
                symbol, 
                execution.side_lighter, 
                execution.size_lighter,
                post_only=True
            ),
            name=f"lighter_{symbol}"
        )

        # 100ms head start for Lighter order book placement
        await asyncio.sleep(0.1)

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: X10 MARKET (Taker) - FILLS IMMEDIATELY
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"🔄 [PARALLEL] {symbol}: X10 MARKET order")
        execution. state = ExecutionState.LEG2_SENT

        x10_task = asyncio.create_task(
            self._execute_x10_leg(
                symbol, 
                execution. side_x10, 
                execution. size_x10,
                post_only=False
            ),
            name=f"x10_{symbol}"
        )

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: WAIT FOR BOTH WITH TIMEOUT
        # ═══════════════════════════════════════════════════════════════════
        try:
            results = await asyncio. wait_for(
                asyncio.gather(lighter_task, x10_task, return_exceptions=True),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"⏰ [PARALLEL] {symbol}: Execution timeout ({timeout}s)!")
            lighter_task.cancel()
            x10_task.cancel()
            
            # Check what was partially filled
            try:
                lighter_result = lighter_task.result() if lighter_task. done() else None
                x10_result = x10_task.result() if x10_task.done() else None
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                lighter_result = None
                x10_result = None

            execution.lighter_filled = self._check_fill(lighter_result)
            execution.x10_filled = self._check_fill(x10_result)
            execution.error = "Timeout"
            
            if execution.lighter_filled or execution.x10_filled:
                await self._queue_rollback(execution)
            
            return False, None, None

        lighter_result, x10_result = results

        # Parse results
        lighter_success, lighter_order = self._parse_result(lighter_result)
        x10_success, x10_order = self._parse_result(x10_result)

        execution.lighter_order_id = lighter_order
        execution.x10_order_id = x10_order
        execution.lighter_filled = lighter_success
        execution. x10_filled = x10_success

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: ATOMIC VERIFICATION (GHOST TRADE PREVENTION)
        # ═══════════════════════════════════════════════════════════════════
        if lighter_success and x10_success:
            logger.info(
                f"📝 [PARALLEL] {symbol}: Order APIs returned success, verifying positions..."
            )
            
            # Verify actual positions exist before declaring success
            both_ok, x10_ok, lighter_ok = await self._verify_execution(
                symbol,
                execution.size_x10,
                execution.size_lighter
            )
            
            if both_ok:
                execution.state = ExecutionState.COMPLETE
                logger.info(
                    f"✅ [PARALLEL] {symbol}: Both positions verified in {execution.elapsed_ms:.0f}ms"
                )
                return True, x10_order, lighter_order
            
            # Partial verification failure - emergency close the successful side
            if x10_ok and not lighter_ok:
                logger.error(
                    f"🚨 [EMERGENCY] {symbol}: X10 filled but Lighter verification failed! "
                    "Closing X10 immediately..."
                )
                execution.x10_filled = True
                execution.lighter_filled = False
                await self._queue_rollback(execution)
                return False, x10_order, None
            
            if lighter_ok and not x10_ok:
                logger.error(
                    f"🚨 [EMERGENCY] {symbol}: Lighter filled but X10 verification failed! "
                    "Closing Lighter immediately..."
                )
                execution.lighter_filled = True
                execution.x10_filled = False
                await self._queue_rollback(execution)
                return False, None, lighter_order
            
            # Neither verified - both order APIs lied, nothing to rollback
            logger.error(
                f"🚨 [VERIFY FAIL] {symbol}: Order APIs said success but no positions exist!"
            )
            execution.state = ExecutionState.FAILED
            execution.error = "Verification failed: No positions found"
            return False, None, None

        # One or both failed - need rollback
        if lighter_success and not x10_success:
            execution. error = f"X10 failed: {x10_result}"
            logger.error(f"🔄 [ROLLBACK] {symbol}: X10 failed, queueing Lighter rollback")
            await self._queue_rollback(execution)
            return False, None, lighter_order

        if x10_success and not lighter_success:
            execution.error = f"Lighter failed: {lighter_result}"
            logger.error(f"🔄 [ROLLBACK] {symbol}: Lighter failed, queueing X10 rollback")
            await self._queue_rollback(execution)
            return False, x10_order, None

        # Both failed - no rollback needed
        execution.state = ExecutionState.FAILED
        execution.error = f"Both failed: X10={x10_result}, Lighter={lighter_result}"
        logger.error(f"❌ [PARALLEL] {symbol}: Both legs failed")
        return False, None, None

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

    def _parse_result(self, result: Any) -> Tuple[bool, Optional[str]]:
        """Parse execution result, handling exceptions"""
        if isinstance(result, Exception):
            return False, None
        if isinstance(result, tuple) and len(result) >= 2:
            return bool(result[0]), result[1]
        return False, None

    def _check_fill(self, result: Any) -> bool:
        """Check if a result indicates a fill"""
        if result is None:
            return False
        success, _ = self._parse_result(result)
        return success

    async def _queue_rollback(self, execution: TradeExecution):
        """Queue execution for background rollback (non-blocking)"""
        execution.state = ExecutionState. ROLLBACK_QUEUED
        self._stats["rollbacks_triggered"] += 1
        await self._rollback_queue.put(execution)
        logger.info(f"📤 [ROLLBACK] {execution.symbol}: Queued for background processing")

    async def _execute_rollback_with_retry(self, execution: TradeExecution) -> bool:
        """Execute rollback with exponential backoff retry"""
        symbol = execution.symbol
        logger.info(f"🔄 [ROLLBACK] Processing {symbol}...")

        # Initial settlement delay
        await asyncio.sleep(3.0)

        for attempt in range(self.MAX_ROLLBACK_ATTEMPTS):
            try:
                execution.rollback_attempts = attempt + 1
                delay = self.ROLLBACK_BASE_DELAY * (2 ** attempt)
                
                if attempt > 0:
                    logger.info(f"🔄 [ROLLBACK] {symbol}: Retry {attempt + 1}/{self.MAX_ROLLBACK_ATTEMPTS} after {delay}s")
                    await asyncio.sleep(delay)

                success = False
                
                if execution.lighter_filled and not execution.x10_filled:
                    success = await self._rollback_lighter(execution)
                elif execution.x10_filled and not execution.lighter_filled:
                    success = await self._rollback_x10(execution)
                else:
                    # Edge case: both or neither - shouldn't happen
                    logger.warning(f"⚠️ [ROLLBACK] {symbol}: Unexpected state")
                    return True

                if success:
                    logger. info(f"✅ [ROLLBACK] {symbol}: Complete (attempt {attempt + 1})")
                    return True

            except Exception as e:
                logger.error(f"❌ [ROLLBACK] {symbol}: Attempt {attempt + 1} error: {e}")

        logger.error(f"❌ [ROLLBACK] {symbol}: All {self.MAX_ROLLBACK_ATTEMPTS} attempts failed!")
        return False

    async def _rollback_x10(self, execution: TradeExecution) -> bool:
        """Rollback X10 position with actual position verification"""
        symbol = execution.symbol
        
        try:
            positions = await self.x10.fetch_open_positions()
            pos = next(
                (p for p in (positions or []) 
                 if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✓ X10 Rollback {symbol}: No position found (already closed? )")
                return True

            actual_size = pos.get('size', 0)
            # Positive = LONG, Negative = SHORT
            original_side = "BUY" if actual_size > 0 else "SELL"
            close_size = abs(actual_size)

            logger.info(
                f"→ X10 Rollback {symbol}: size={actual_size:.6f}, side={original_side}"
            )

            success, _ = await self.x10.close_live_position(
                symbol, original_side, close_size
            )

            if success:
                logger.info(f"✓ X10 rollback {symbol}: Success ({close_size:.6f} coins)")
                return True
            else:
                logger. warning(f"✗ X10 rollback {symbol}: close_live_position returned False")
                return False

        except Exception as e:
            logger.error(f"✗ X10 rollback {symbol}: Exception: {e}")
            return False

    async def _rollback_lighter(self, execution: TradeExecution) -> bool:
        """Rollback Lighter position with actual position verification"""
        symbol = execution.symbol
        
        try:
            positions = await self.lighter.fetch_open_positions()
            pos = next(
                (p for p in (positions or [])
                 if p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✓ Lighter Rollback {symbol}: No position found (already closed?)")
                return True

            actual_size = pos.get('size', 0)
            original_side = "BUY" if actual_size > 0 else "SELL"
            close_size_coins = abs(actual_size)

            # CRITICAL FIX: Sichere Typ-Konvertierung
            raw_price = self.lighter.fetch_mark_price(symbol)
            try:
                mark_price = float(raw_price) if raw_price is not None else 0.0
            except (ValueError, TypeError):
                mark_price = 0.0
            
            if mark_price <= 0:
                logger.error(f"✗ Lighter rollback {symbol}: No valid price")
                return False

            notional_usd = close_size_coins * mark_price

            logger.info(
                f"→ Lighter Rollback {symbol}: "
                f"size={actual_size:.6f} @ ${mark_price:.2f} = ${notional_usd:.2f}"
            )

            success, _ = await self.lighter.close_live_position(
                symbol, original_side, notional_usd
            )

            if success:
                logger.info(f"✓ Lighter rollback {symbol}: Success (${notional_usd:.2f})")
                return True
            else:
                logger.warning(f"✗ Lighter rollback {symbol}: close_live_position returned False")
                return False

        except Exception as e:
            logger.error(f"✗ Lighter rollback {symbol}: Exception: {e}")
            return False

    def get_execution_stats(self) -> Dict[str, Any]:
        """Return current execution statistics for monitoring"""
        active_states = {}
        for ex in self.active_executions.values():
            state_name = ex.state. value
            active_states[state_name] = active_states. get(state_name, 0) + 1
        
        return {
            "active_executions": len(self.active_executions),
            "pending_rollbacks": self._rollback_queue.qsize(),
            "active_states": active_states,
            **self._stats
        }

    def get_execution(self, symbol: str) -> Optional[TradeExecution]:
        """Get execution state for a symbol"""
        return self.active_executions.get(symbol)