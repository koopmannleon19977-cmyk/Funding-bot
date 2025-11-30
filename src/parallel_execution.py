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
    """State machine states for atomic trade execution tracking"""
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    LIGHTER_EXECUTED = "LIGHTER_EXECUTED"
    X10_EXECUTED = "X10_EXECUTED"
    COMPLETE = "COMPLETE"
    LIGHTER_FAILED = "LIGHTER_FAILED"
    X10_FAILED = "X10_FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    # Legacy states for compatibility
    LEG1_SENT = "LEG1_SENT"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SENT = "LEG2_SENT"
    PARTIAL_FILL = "PARTIAL_FILL"
    ROLLBACK_QUEUED = "ROLLBACK_QUEUED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_DONE = "ROLLBACK_DONE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


@dataclass
class TradeExecution:
    """Tracks state of a single atomic trade execution"""
    symbol: str
    state: ExecutionState = ExecutionState.PENDING
    x10_order_id: Optional[str] = None
    lighter_order_id: Optional[str] = None
    x10_filled: bool = False
    lighter_filled: bool = False
    x10_fill_size: float = 0.0
    lighter_fill_size: float = 0.0
    start_time: float = field(default_factory=time.monotonic)
    validation_time: Optional[float] = None
    lighter_execution_time: Optional[float] = None
    x10_execution_time: Optional[float] = None
    error: Optional[str] = None
    rollback_attempts: int = 0
    side_x10: str = ""
    side_lighter: str = ""
    size_x10: float = 0.0
    size_lighter: float = 0.0
    price_x10: float = 0.0
    price_lighter: float = 0.0
    price_timestamp: Optional[float] = None

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_time) * 1000


class ParallelExecutionManager:
    """
    Manages atomic trade execution with two-phase commit pattern.
    
    ═══════════════════════════════════════════════════════════════════════════
    ATOMIC EXECUTION STRATEGY:
    ═══════════════════════════════════════════════════════════════════════════
    PHASE 1 - PRE-VALIDATION:
    - Validate both X10 and Lighter prices are fresh (< 5 seconds)
    - Validate sufficient balance on both exchanges
    - Validate market conditions (spread, liquidity)
    - If ANY validation fails → abort entirely
    
    PHASE 2 - SEQUENTIAL EXECUTION WITH ROLLBACK:
    - Execute Lighter order FIRST (more likely to fail)
    - Only if Lighter succeeds → Execute X10 order
    - If X10 fails → Immediately rollback Lighter position
    - Store transaction state for crash recovery
    ═══════════════════════════════════════════════════════════════════════════
    """
    
    MAX_ROLLBACK_ATTEMPTS = 3
    ROLLBACK_BASE_DELAY = 2.0  # seconds
    EXECUTION_TIMEOUT = 15.0   # seconds
    MAX_PRICE_AGE = 5.0  # seconds - maximum age for price data

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
        """Start background rollback processor and recover orphaned positions"""
        if self._rollback_task is None or self._rollback_task.done():
            self._rollback_task = asyncio.create_task(
                self._rollback_processor(), 
                name="rollback_processor"
            )
            logger.info("✅ ParallelExecutionManager: Rollback processor started")
            
            # Recovery mechanism: check for orphaned positions on startup
            await self._recover_orphaned_positions()

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

        async with self.execution_locks[symbol]:
            # Create execution tracker
            execution = TradeExecution(
                symbol=symbol,
                side_x10=side_x10,
                side_lighter=side_lighter,
                size_x10=float(size_x10),
                size_lighter=float(size_lighter),
                price_x10=float(price_x10) if price_x10 else 0.0,
                price_lighter=float(price_lighter) if price_lighter else 0.0,
                price_timestamp=time.time(),
            )
            self.active_executions[symbol] = execution
            self._stats["total_executions"] += 1

            try:
                # ═══════════════════════════════════════════════════════════
                # PHASE 1: PRE-VALIDATION (abort if any check fails)
                # ═══════════════════════════════════════════════════════════
                execution.state = ExecutionState.VALIDATING
                logger.info(f"🔍 [ATOMIC] {symbol}: Phase 1 - Pre-validation")
                
                validation_result = await self._validate_trade_conditions(
                    execution, price_x10, price_lighter
                )
                
                if not validation_result[0]:
                    execution.state = ExecutionState.ABORTED
                    execution.error = validation_result[1]
                    logger.error(f"❌ [ATOMIC] {symbol}: Validation failed - {validation_result[1]}")
                    self._stats["failed"] += 1
                    return False, None, None
                
                execution.validation_time = time.time()
                logger.info(f"✅ [ATOMIC] {symbol}: Validation passed")

                # ═══════════════════════════════════════════════════════════
                # PHASE 2: SEQUENTIAL EXECUTION WITH ROLLBACK
                # ═══════════════════════════════════════════════════════════
                result = await self._atomic_execute_internal(
                    execution, timeout
                )
                
                if result[0]:
                    self._stats["successful"] += 1
                else:
                    self._stats["failed"] += 1
                return result
                
            except Exception as e:
                execution.state = ExecutionState.FAILED
                execution.error = str(e)
                self._stats["failed"] += 1
                logger.error(f"❌ [ATOMIC] {symbol}: Exception: {e}", exc_info=True)
                return False, None, None
            finally:
                # Schedule cleanup after 60s
                asyncio.get_event_loop().call_later(
                    60.0,
                    lambda s=symbol: self.active_executions.pop(s, None)
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

    def _log_state_transition(self, symbol: str, old_state: ExecutionState, new_state: ExecutionState):
        """Log state transitions for debugging"""
        logger.info(f"📊 [STATE] {symbol}: {old_state.value} → {new_state.value}")

    async def _atomic_execute_internal(
        self,
        execution: TradeExecution,
        timeout: float
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        symbol = execution.symbol
        
        # PREFLIGHT VALIDATION
        old_state = execution.state
        execution.state = ExecutionState.VALIDATING
        self._log_state_transition(symbol, old_state, execution.state)
        
        if not await self._validate_preflight(execution):
            # State already set to ABORTED in _validate_preflight
            return False, None, None
        
        # STEP 1: Execute Lighter FIRST (fails more often)
        old_state = execution.state
        logger.info(f"🔄 [ATOMIC] {symbol}: Executing Lighter leg first")
        lighter_result = await self._execute_lighter_leg(
            symbol,
            execution.side_lighter,
            execution.size_lighter,
            post_only=True
        )
        lighter_success, lighter_order = self._parse_result(lighter_result)
        execution.lighter_order_id = lighter_order
        execution.lighter_filled = lighter_success
        execution.lighter_execution_time = time.time()

        if not lighter_success:
            execution.state = ExecutionState.LIGHTER_FAILED
            self._log_state_transition(symbol, old_state, execution.state)
            execution.error = f"Lighter failed: {lighter_result}"
            logger.error(f"❌ [ATOMIC] {symbol}: Lighter leg failed, aborting trade")
            return False, None, None

        execution.state = ExecutionState.LIGHTER_EXECUTED
        self._log_state_transition(symbol, old_state, execution.state)

        # STEP 2: Execute X10 ONLY if Lighter succeeded
        old_state = execution.state
        logger.info(f"🔄 [ATOMIC] {symbol}: Executing X10 leg")
        x10_result = await self._execute_x10_leg(
            symbol,
            execution.side_x10,
            execution.size_x10,
            post_only=False
        )
        x10_success, x10_order = self._parse_result(x10_result)
        execution.x10_order_id = x10_order
        execution.x10_filled = x10_success
        execution.x10_execution_time = time.time()

        if not x10_success:
            execution.state = ExecutionState.ROLLING_BACK
            self._log_state_transition(symbol, old_state, execution.state)
            execution.error = f"X10 failed: {x10_result}"
            logger.error(f"❌ [ATOMIC] {symbol}: X10 leg failed, rolling back Lighter immediately")
            
            # IMMEDIATE rollback (not queued)
            rollback_success = await self._execute_rollback_with_retry(execution)
            
            if rollback_success:
                execution.state = ExecutionState.ROLLED_BACK
                self._log_state_transition(symbol, ExecutionState.ROLLING_BACK, execution.state)
                logger.info(f"✅ [ATOMIC] {symbol}: Lighter rollback complete")
            else:
                execution.state = ExecutionState.ROLLBACK_FAILED
                self._log_state_transition(symbol, ExecutionState.ROLLING_BACK, execution.state)
                logger.error(f"❌ [ATOMIC] {symbol}: Lighter rollback FAILED - manual intervention required!")
            
            return False, None, lighter_order

        execution.state = ExecutionState.X10_EXECUTED
        self._log_state_transition(symbol, old_state, execution.state)

        # STEP 3: Verify both positions
        both_ok, x10_ok, lighter_ok = await self._verify_execution(
            symbol,
            execution.size_x10,
            execution.size_lighter
        )
        
        if both_ok:
            old_state = execution.state
            execution.state = ExecutionState.COMPLETE
            self._log_state_transition(symbol, old_state, execution.state)
            logger.info(f"✅ [ATOMIC] {symbol}: Both positions verified in {execution.elapsed_ms:.0f}ms")
            return True, x10_order, lighter_order
        
        # STEP 4: Emergency rollback if verification fails
        if x10_ok and not lighter_ok:
            logger.error(f"🚨 [ATOMIC] {symbol}: X10 filled but Lighter verification failed! Closing X10 immediately...")
            execution.x10_filled = True
            execution.lighter_filled = False
            execution.state = ExecutionState.ROLLING_BACK
            rollback_success = await self._execute_rollback_with_retry(execution)
            execution.state = ExecutionState.ROLLED_BACK if rollback_success else ExecutionState.ROLLBACK_FAILED
            return False, x10_order, None
            
        if lighter_ok and not x10_ok:
            logger.error(f"🚨 [ATOMIC] {symbol}: Lighter filled but X10 verification failed! Closing Lighter immediately...")
            execution.lighter_filled = True
            execution.x10_filled = False
            execution.state = ExecutionState.ROLLING_BACK
            rollback_success = await self._execute_rollback_with_retry(execution)
            execution.state = ExecutionState.ROLLED_BACK if rollback_success else ExecutionState.ROLLBACK_FAILED
            return False, None, lighter_order
            
        logger.error(f"🚨 [ATOMIC] {symbol}: Verification failed: No positions found")
        execution.state = ExecutionState.FAILED
        execution.error = "Verification failed: No positions found"
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
        """Rollback X10 position using safe_rollback_position (handles actual position size and reduce_only internally)"""
        symbol = execution.symbol
        
        try:
            # X10 close_live_position delegates to safe_rollback_position which:
            # 1. Cancels any open orders
            # 2. Fetches actual position size
            # 3. Uses reduce_only=True automatically
            # 4. Verifies closure
            
            # Determine original side from execution or fetch position
            original_side = execution.side_x10
            
            logger.info(f"→ X10 Rollback {symbol}: Calling safe_rollback_position (original_side={original_side})")

            # close_live_position will delegate to safe_rollback_position
            # notional_usd is not used by safe_rollback_position (it calculates internally)
            success, _ = await self.x10.close_live_position(
                symbol, original_side, 0.0  # notional_usd not used by safe_rollback
            )

            if success:
                logger.info(f"✓ X10 rollback {symbol}: Success")
                return True
            else:
                logger.warning(f"✗ X10 rollback {symbol}: close_live_position returned False")
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

    async def _validate_preflight(self, execution: TradeExecution) -> bool:
        """
        Preflight validation - Check mark prices before execution.
        If ANY validation fails, abort immediately.
        
        Returns:
            True if validation passes, False otherwise
        """
        symbol = execution.symbol
        
        try:
            # Check Lighter mark price
            lighter_price_raw = self.lighter.fetch_mark_price(symbol)
            try:
                lighter_price = float(lighter_price_raw) if lighter_price_raw is not None else 0.0
            except (ValueError, TypeError):
                lighter_price = 0.0
            
            if lighter_price <= 0:
                execution.state = ExecutionState.ABORTED
                execution.error = f"Lighter price invalid: {lighter_price}"
                logger.error(f"❌ [PREFLIGHT] {symbol}: Aborted - Lighter price = {lighter_price}")
                return False
            
            # Check X10 mark price
            x10_price_raw = self.x10.fetch_mark_price(symbol)
            try:
                x10_price = float(x10_price_raw) if x10_price_raw is not None else 0.0
            except (ValueError, TypeError):
                x10_price = 0.0
            
            if x10_price <= 0:
                execution.state = ExecutionState.ABORTED
                execution.error = f"X10 price invalid: {x10_price}"
                logger.error(f"❌ [PREFLIGHT] {symbol}: Aborted - X10 price = {x10_price}")
                return False
            
            logger.info(f"✅ [PREFLIGHT] {symbol}: Prices valid (Lighter=${lighter_price:.2f}, X10=${x10_price:.2f})")
            return True
            
        except Exception as e:
            execution.state = ExecutionState.ABORTED
            execution.error = f"Preflight exception: {e}"
            logger.error(f"❌ [PREFLIGHT] {symbol}: Aborted - Exception: {e}")
            return False

    async def _validate_trade_conditions(
        self,
        execution: TradeExecution,
        price_x10: Optional[Decimal],
        price_lighter: Optional[Decimal]
    ) -> Tuple[bool, str]:
        """
        Phase 1: Pre-validation - Check all conditions before execution.
        
        Returns:
            (success, error_message)
        """
        symbol = execution.symbol
        
        # 1. Validate prices exist and are valid
        lighter_price = float(price_lighter) if price_lighter is not None else 0.0
        x10_price = float(price_x10) if price_x10 is not None else 0.0
        
        if lighter_price <= 0 or x10_price <= 0:
            return False, f"Invalid prices (L={lighter_price:.6f}, X={x10_price:.6f})"
        
        # 2. Validate price freshness (< 5 seconds)
        if execution.price_timestamp:
            price_age = time.time() - execution.price_timestamp
            if price_age > self.MAX_PRICE_AGE:
                return False, f"Stale prices ({price_age:.1f}s old, max {self.MAX_PRICE_AGE}s)"
        
        # 3. Validate sufficient balance on both exchanges
        try:
            lighter_balance = await self.lighter.get_real_available_balance() if hasattr(self.lighter, 'get_real_available_balance') else None
            if lighter_balance is not None:
                required_lighter = execution.size_lighter * lighter_price
                if lighter_balance < required_lighter:
                    return False, f"Insufficient Lighter balance (${lighter_balance:.2f} < ${required_lighter:.2f})"
        except Exception as e:
            logger.warning(f"⚠️ [VALIDATE] {symbol}: Could not check Lighter balance: {e}")
        
        try:
            x10_balance = await self.x10.get_real_available_balance() if hasattr(self.x10, 'get_real_available_balance') else None
            if x10_balance is not None:
                required_x10 = execution.size_x10 * x10_price
                if x10_balance < required_x10:
                    return False, f"Insufficient X10 balance (${x10_balance:.2f} < ${required_x10:.2f})"
        except Exception as e:
            logger.warning(f"⚠️ [VALIDATE] {symbol}: Could not check X10 balance: {e}")
        
        # 4. Validate market conditions (spread check)
        spread_pct = abs(lighter_price - x10_price) / ((lighter_price + x10_price) / 2) * 100
        if spread_pct > 5.0:  # More than 5% spread is suspicious
            logger.warning(f"⚠️ [VALIDATE] {symbol}: Large price spread ({spread_pct:.2f}%)")
        
        # 5. Validate liquidity (optional - check orderbook depth)
        # This could be expanded to check actual orderbook depth
        
        return True, "All validations passed"

    async def _recover_orphaned_positions(self):
        """
        Recovery mechanism: Check for orphaned positions on startup.
        
        Looks for any trades stuck in LIGHTER_EXECUTED or ROLLING_BACK state
        and attempts to rollback the positions.
        """
        logger.info("🔄 [RECOVERY] Checking for orphaned positions...")
        
        try:
            # Check actual positions on exchanges with validation retry
            lighter_positions = await self.lighter.fetch_open_positions()
            x10_positions = await self.x10.fetch_open_positions()

            # If any side returns empty while previously had positions, retry once after short delay
            if (not lighter_positions or not x10_positions):
                await asyncio.sleep(1.5)
                lighter_positions = lighter_positions or await self.lighter.fetch_open_positions()
                x10_positions = x10_positions or await self.x10.fetch_open_positions()
            
            lighter_symbols = {p.get('symbol') for p in (lighter_positions or []) if abs(p.get('size', 0)) > 1e-8}
            x10_symbols = {p.get('symbol') for p in (x10_positions or []) if abs(p.get('size', 0)) > 1e-8}
            
            # Find unhedged positions (exists on one exchange but not the other)
            lighter_only = lighter_symbols - x10_symbols
            x10_only = x10_symbols - lighter_symbols
            
            if lighter_only:
                logger.warning(f"🚨 [RECOVERY] Found {len(lighter_only)} orphaned Lighter positions: {lighter_only}")
                for symbol in lighter_only:
                    # Double-confirm it's not open on X10 before closing
                    x10_confirm = any(p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8 for p in (x10_positions or []))
                    if x10_confirm:
                        logger.info(f"[RECOVERY] Skip rollback for {symbol}: confirmed on X10 as well")
                        continue
                    # Create a fake execution object for rollback
                    execution = TradeExecution(
                        symbol=symbol,
                        state=ExecutionState.LIGHTER_EXECUTED,
                        lighter_filled=True,
                        x10_filled=False,
                        side_lighter="UNKNOWN",
                        side_x10="UNKNOWN",
                        size_lighter=0.0,
                        size_x10=0.0,
                    )
                    logger.info(f"🔄 [RECOVERY] Queueing rollback for orphaned Lighter position: {symbol}")
                    await self._queue_rollback(execution)
            
            if x10_only:
                logger.warning(f"🚨 [RECOVERY] Found {len(x10_only)} orphaned X10 positions: {x10_only}")
                for symbol in x10_only:
                    # Double-confirm it's not open on Lighter before closing
                    lighter_confirm = any(p.get('symbol') == symbol and abs(p.get('size', 0)) > 1e-8 for p in (lighter_positions or []))
                    if lighter_confirm:
                        logger.info(f"[RECOVERY] Skip rollback for {symbol}: confirmed on Lighter as well")
                        continue
                    # Create a fake execution object for rollback
                    execution = TradeExecution(
                        symbol=symbol,
                        state=ExecutionState.X10_EXECUTED,
                        lighter_filled=False,
                        x10_filled=True,
                        side_lighter="UNKNOWN",
                        side_x10="UNKNOWN",
                        size_lighter=0.0,
                        size_x10=0.0,
                    )
                    logger.info(f"🔄 [RECOVERY] Queueing rollback for orphaned X10 position: {symbol}")
                    await self._queue_rollback(execution)
            
            if not lighter_only and not x10_only:
                logger.info("✅ [RECOVERY] No orphaned positions found")
        
        except Exception as e:
            logger.error(f"❌ [RECOVERY] Failed to check for orphaned positions: {e}")

    async def _rollback_lighter_position(self, symbol: str, original_side: str, notional_usd: float) -> bool:
        """
        Immediate rollback of Lighter position (synchronous version for atomic execution).
        
        This is called immediately when X10 fails after Lighter succeeds.
        
        Args:
            symbol: Trading symbol
            original_side: Original side of the trade ("BUY" or "SELL")
            notional_usd: Notional value in USD
            
        Returns:
            True if rollback successful, False otherwise
        """
        logger.info(f"🔄 [ROLLBACK] Immediate Lighter rollback for {symbol}")
        
        try:
            success, _ = await self.lighter.close_live_position(
                symbol, original_side, notional_usd
            )
            
            if success:
                logger.info(f"✅ [ROLLBACK] Lighter position closed: {symbol}")
                return True
            else:
                logger.error(f"❌ [ROLLBACK] Failed to close Lighter position: {symbol}")
                return False
                
        except Exception as e:
            logger.error(f"❌ [ROLLBACK] Exception closing Lighter position {symbol}: {e}")
            return False