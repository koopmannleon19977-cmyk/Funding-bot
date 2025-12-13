# src/parallel_execution.py - PUNKT 1: PARALLEL EXECUTION MIT OPTIMISTIC ROLLBACK
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
from typing import Optional, Tuple, Dict, Any, List
from decimal import Decimal
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


import config
from src.utils import safe_float, safe_decimal
from src.fee_manager import get_fee_manager
from src.validation.orderbook_validator import (
    OrderbookValidator,
    OrderbookValidationResult,
    OrderbookQuality,
    ExchangeProfile,
    get_orderbook_validator,
)
from src.data.orderbook_provider import get_orderbook_provider, init_orderbook_provider
import math

def calculate_common_quantity(amount_usd, price, x10_step, lighter_step):
    """
    Berechnet die exakte Anzahl Coins, die auf BEIDEN Börsen handelbar ist.
    FIX: Nutzt Decimal für Präzision, um Float-Fehler (0.1+0.2!=0.3) zu vermeiden.
    """
    # 1. Inputs sicher zu Decimal konvertieren
    d_amount = safe_decimal(amount_usd)
    d_price = safe_decimal(price)
    d_x10_step = safe_decimal(x10_step)
    d_lighter_step = safe_decimal(lighter_step)
    
    if d_price <= 0: return 0.0
    
    # 2. Berechne theoretische Anzahl Coins
    raw_coins = d_amount / d_price
    
    # 3. Finde den größeren Schritt (Step Size)
    max_step = max(d_x10_step, d_lighter_step)
    
    if max_step <= 0: return float(raw_coins)
    
    # 4. Runde AB auf das nächste Vielfache
    # Decimal Quantize Verhalten bei 'ROUND_FLOOR' ist nicht exakt modulo-basiert für steps != 10^-N
    # Besser: (raw // step) * step
    
    steps = raw_coins // max_step # Ganzzahlige Anzahl Schritte
    coins = steps * max_step
    
    # Return as float for API compatibility
    return float(coins)


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
    state: ExecutionState = ExecutionState.PENDING
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
    quantity_coins: float = 0.0

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
    ROLLBACK_BASE_DELAY = config.ROLLBACK_DELAY_SECONDS
    EXECUTION_TIMEOUT = config.PARALLEL_EXECUTION_TIMEOUT

    def __init__(self, x10_adapter, lighter_adapter, db):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.db = db
        self.execution_locks: Dict[str, asyncio.Lock] = {}
        self.active_executions: Dict[str, TradeExecution] = {}
        self._rollback_queue: asyncio.Queue[Optional[TradeExecution]] = asyncio.Queue()
        self._rollback_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._shutdown_requested = False
        self._graceful_timeout = 30.0  # seconds to wait for graceful completion
        self.is_running = True
        self._stats = {
            "total_executions": 0,
            "successful": 0,
            "failed": 0,
            "rollbacks_triggered": 0,
            "rollbacks_successful": 0,
            "rollbacks_failed": 0,
            "rollbacks": 0
        }
        
        # ═══════════════════════════════════════════════════════════════
        # COMPLIANCE CHECK CACHE: Avoid repeated API calls for open orders
        # ═══════════════════════════════════════════════════════════════
        self._compliance_cache: Dict[str, Tuple[float, bool]] = {}  # symbol -> (timestamp, is_compliant)
        self._compliance_cache_ttl = 5.0  # Cache compliance result for 5 seconds
        
        # ═══════════════════════════════════════════════════════════════
        # FIX 5: EVENT-BASED FILL DETECTION
        # ═══════════════════════════════════════════════════════════════
        # Events for position updates (filled signals)
        self._position_fill_events: Dict[str, asyncio.Event] = {}  # symbol -> Event
        self._position_fill_sizes: Dict[str, float] = {}  # symbol -> target_size_coins
        self._last_position_check: Dict[str, float] = {}  # symbol -> timestamp
        
        logger.info("✅ ParallelExecutionManager started")

    async def start(self):
        """Start background rollback processor and register position update callbacks"""
        self._rollback_task = asyncio.create_task(self._rollback_processor())
        
        # ═══════════════════════════════════════════════════════════════
        # FIX 5: Register position update callbacks for event-based fill detection
        # ═══════════════════════════════════════════════════════════════
        if hasattr(self.x10, 'register_position_callback'):
            self.x10.register_position_callback(self._on_x10_position_update)
            logger.debug("✅ Registered X10 position update callback for fill detection")
        
        logger.info("✅ ParallelExecutionManager: Rollback processor started")

    async def stop(self, force: bool = False) -> None:
        """
        Stop the execution manager gracefully.
        
        IMPROVED SHUTDOWN SEQUENCE:
        1. Set shutdown flag (prevents new executions)
        2. Wait for active executions to complete OR timeout
        3. For timed-out executions: trigger rollback
        4. Cancel remaining tasks
        """
        if self._shutdown_requested:
            logger.info("✅ ParallelExecutionManager: Already stopping")
            return
            
        self._shutdown_requested = True
        self._shutdown_event.set()
        self.is_running = False
        config.IS_SHUTTING_DOWN = True
        
        active_count = len(self.active_executions)
        if active_count == 0:
            logger.info("✅ ParallelExecutionManager: No active executions")
            await self._stop_rollback_task()
            return
        
        logger.info(f"🛑 ParallelExecutionManager: Stopping {active_count} active executions...")
        
        if force:
            # Immediate abort (old behavior)
            logger.warning(f"⚡ ParallelExec: Force-aborting {active_count} executions!")
            await self._cancel_all_tasks()
            await self._stop_rollback_task()
            return
        
        # ═══════════════════════════════════════════════════════════════════
        # GRACEFUL SHUTDOWN: Wait for executions to finish or rollback
        # ═══════════════════════════════════════════════════════════════════
        
        logger.info(f"⏳ Waiting up to {self._graceful_timeout}s for {active_count} executions to complete...")
        
        # Give active executions time to notice shutdown and abort gracefully
        # They check config.IS_SHUTTING_DOWN and should exit quickly
        wait_start = time.monotonic()
        
        while len(self.active_executions) > 0:
            elapsed = time.monotonic() - wait_start
            if elapsed >= self._graceful_timeout:
                break
            await asyncio.sleep(0.5)
        
        # Check what's left
        remaining = len(self.active_executions)
        completed = active_count - remaining
        
        if remaining > 0:
            logger.warning(f"⚠️ {remaining} executions did not complete in time")
            
            # Get symbols of incomplete executions
            incomplete_symbols = list(self.active_executions.keys())
            
            # Rollback incomplete executions
            await self._rollback_incomplete_executions(incomplete_symbols)
        
        logger.info(f"✅ ParallelExecutionManager: {completed} completed, {remaining} rolled back")
        
        self.active_executions.clear()
        await self._stop_rollback_task()
        logger.info("✅ ParallelExecutionManager: Stopped")

    async def _stop_rollback_task(self) -> None:
        """Stop the background rollback processor"""
        if self._rollback_task:
            if self._rollback_task.done():
                # Task already finished - retrieve exception to prevent "never retrieved" error
                try:
                    self._rollback_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Rollback task had exception: {e}")
            else:
                # Task still running - cancel it
                self._rollback_task.cancel()
                try:
                    await self._rollback_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Rollback task exception during cancel: {e}")

    async def _rollback_incomplete_executions(self, symbols: List[str]) -> None:
        """
        Rollback partially completed executions.
        
        This handles the case where:
        - Lighter order filled but X10 hedge was not placed
        - X10 order filled but Lighter hedge was not placed
        """
        logger.info(f"🔄 Rolling back {len(symbols)} incomplete executions: {symbols}")
        
        for symbol in symbols:
            try:
                # Check current positions on both exchanges
                lighter_pos = None
                x10_pos = None
                
                try:
                    positions = await self.lighter.fetch_open_positions()
                    for p in positions or []:
                        if p.get("symbol") == symbol:
                            lighter_pos = p
                            break
                except Exception as e:
                    logger.debug(f"Could not fetch Lighter position for {symbol}: {e}")
                
                try:
                    positions = await self.x10.fetch_open_positions()
                    for p in positions or []:
                        if p.get("symbol") == symbol:
                            x10_pos = p
                            break
                except Exception as e:
                    logger.debug(f"Could not fetch X10 position for {symbol}: {e}")
                
                # Determine rollback action
                lighter_size = abs(safe_float(lighter_pos.get("size", 0))) if lighter_pos else 0
                x10_size = abs(safe_float(x10_pos.get("size", 0))) if x10_pos else 0
                
                if lighter_size > 0 and x10_size == 0:
                    # Lighter filled, X10 missing -> Close Lighter
                    logger.warning(f"🔄 ROLLBACK {symbol}: Lighter has position ({lighter_size}), X10 missing. Closing Lighter...")
                    await self._emergency_close_position(self.lighter, symbol, lighter_pos)
                    
                elif x10_size > 0 and lighter_size == 0:
                    # X10 filled, Lighter missing -> Close X10
                    logger.warning(f"🔄 ROLLBACK {symbol}: X10 has position ({x10_size}), Lighter missing. Closing X10...")
                    await self._emergency_close_position(self.x10, symbol, x10_pos)
                    
                elif lighter_size > 0 and x10_size > 0:
                    # Both have positions - this is actually OK (hedge complete)
                    logger.info(f"✅ {symbol}: Both exchanges have positions - hedge appears complete")
                    
                else:
                    # Neither has position - clean exit
                    logger.info(f"✅ {symbol}: No positions on either exchange - clean")
                    
            except Exception as e:
                logger.error(f"❌ Rollback error for {symbol}: {e}")
    
    # ═══════════════════════════════════════════════════════════════
    # FIX 5: EVENT-BASED FILL DETECTION
    # ═══════════════════════════════════════════════════════════════
    
    async def _on_x10_position_update(self, data: dict):
        """
        Handle X10 position update from WebSocket.
        Checks if this position update indicates a fill for an active execution.
        """
        try:
            symbol = data.get("market") or data.get("symbol") or ""
            if not symbol:
                return
            
            # Check if we're waiting for a fill for this symbol
            if symbol not in self._position_fill_events:
                return
            
            status = data.get("status", "").upper()
            size = safe_float(data.get("size") or data.get("quantity") or data.get("qty") or 0)
            target_size = self._position_fill_sizes.get(symbol, 0)
            
            # Position is OPENED and size matches target (95% threshold for partial fills)
            if status == "OPENED" and target_size > 0 and abs(size) >= target_size * 0.95:
                event = self._position_fill_events.get(symbol)
                if event and not event.is_set():
                    logger.debug(f"🔔 [WEBSOCKET FILL] {symbol}: Position update detected fill! size={size}, target={target_size}")
                    event.set()
        
        except Exception as e:
            logger.debug(f"Error processing X10 position update for fill detection: {e}")
    
    def _setup_fill_event(self, symbol: str, target_size_coins: float):
        """
        Setup event-based fill detection for a symbol.
        Returns the event that will be set when fill is detected.
        """
        # Create or reset event for this symbol
        if symbol in self._position_fill_events:
            self._position_fill_events[symbol].clear()
        else:
            self._position_fill_events[symbol] = asyncio.Event()
        
        self._position_fill_sizes[symbol] = target_size_coins
        self._last_position_check[symbol] = time.time()
        return self._position_fill_events[symbol]
    
    def _cleanup_fill_event(self, symbol: str):
        """Cleanup fill event for a symbol after execution completes"""
        self._position_fill_events.pop(symbol, None)
        self._position_fill_sizes.pop(symbol, None)
        self._last_position_check.pop(symbol, None)

    async def _emergency_close_position(self, adapter, symbol: str, position: dict) -> bool:
        """Emergency close a position during shutdown."""
        try:
            size = safe_float(position.get("size", 0))
            if size == 0:
                return True
            
            # Determine close side - if position is LONG (size > 0), we SELL to close
            # If position is SHORT (size < 0), we BUY to close
            # But original_side is the side of the POSITION, not the close order
            original_side = "BUY" if size > 0 else "SELL"
            close_side = "SELL" if size > 0 else "BUY"
            
            adapter_name = getattr(adapter, 'name', type(adapter).__name__)
            logger.info(f"🚨 EMERGENCY CLOSE {symbol}: {close_side} {abs(size)} on {adapter_name}")
            
            # Calculate notional_usd from size and price
            try:
                if hasattr(adapter, 'fetch_mark_price'):
                    price = safe_float(adapter.fetch_mark_price(symbol))
                else:
                    price = safe_float(position.get("mark_price", 0) or position.get("entry_price", 0))
                notional_usd = abs(size) * price if price > 0 else abs(size) * 100  # fallback to high estimate
            except Exception:
                notional_usd = abs(size) * 100  # Conservative fallback
            
            # Use close_live_position with POSITIONAL arguments (symbol, original_side, notional_usd)
            if hasattr(adapter, 'close_live_position'):
                success, order_id = await adapter.close_live_position(
                    symbol,
                    original_side,
                    notional_usd
                )
            else:
                success, order_id = await adapter.open_live_position(
                    symbol=symbol,
                    side=close_side,
                    notional_usd=0,
                    amount=abs(size),
                    reduce_only=True,
                    time_in_force="IOC"
                )
            
            if success:
                logger.info(f"✅ Emergency close {symbol} successful: {order_id}")
            else:
                logger.error(f"❌ Emergency close {symbol} failed!")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Emergency close error {symbol}: {e}")
            return False

    async def _cancel_all_tasks(self) -> None:
        """Force cancel all tasks (fallback)."""
        for symbol, task in list(self.active_executions.items()):
            logger.warning(f"⚡ Force-cancelling execution for {symbol}")
        self.active_executions.clear()

    async def _rollback_processor(self):
        """Background task that processes rollback queue with retry logic"""
        logger.info("🔄 Rollback processor running...")
        
        while not self._shutdown_event.is_set():
            try:
                execution = await self._rollback_queue.get()
                
                # Shutdown signal
                if execution is None:
                    self._rollback_queue.task_done()
                    break
                
                execution.state = ExecutionState.ROLLBACK_IN_PROGRESS
                self._stats["rollbacks"] += 1
                
                success = await self._execute_rollback_with_retry(execution)
                
                if success:
                    execution.state = ExecutionState.ROLLBACK_DONE
                    self._stats["rollbacks_successful"] += 1
                else:
                    execution.state = ExecutionState.ROLLBACK_FAILED
                    self._stats["rollbacks_failed"] += 1
                
                self._rollback_queue.task_done()
                
            except asyncio.CancelledError:
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
        timeout = timeout or self.EXECUTION_TIMEOUT
        
        # Get prices for logging
        lighter_price = float(price_lighter) if price_lighter else self.lighter.fetch_mark_price(symbol) or 0.0
        x10_price = float(price_x10) if price_x10 else self.x10.fetch_mark_price(symbol) or 0.0
        
        # ═══════════════════════════════════════════════════════════════════════════
        # HEDGED TRADE START - Comprehensive Logging
        # ═══════════════════════════════════════════════════════════════════════════
        logger.info(f"═══════════════════════════════════════════════════════════")
        logger.info(f"🚀 HEDGED TRADE START: {symbol}")
        logger.info(f"   Lighter: {side_lighter} ${float(size_lighter):.2f} @ ${lighter_price:.6f}")
        logger.info(f"   X10:     {side_x10} ${float(size_x10):.2f} @ ${x10_price:.6f}")
        logger.info(f"   Strategy: MAKER (Lighter first, then X10 hedge)")
        logger.info(f"═══════════════════════════════════════════════════════════")
        
        # Ensure symbol-level lock exists
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()

        async with self.execution_locks[symbol]:
            # ═══════════════════════════════════════════════════════════════
            # PHASE 3: COMPLIANCE CHECK (Wash Trading Prevention)
            # ═══════════════════════════════════════════════════════════════
            is_compliant = await self._run_compliance_check(symbol, side_x10, side_lighter)
            if not is_compliant:
                logger.warning(f"🛡️ Trade blocked by Compliance Check for {symbol}")
                return False, None, None

            # ═══════════════════════════════════════════════════════════════
            # SIZE ALIGNMENT (Common Denominator)
            # ═══════════════════════════════════════════════════════════════
            safe_coins = 0.0
            try:
                # 1. Get Reference Price (Lighter Maker)
                maker_price_val = float(price_lighter) if price_lighter else None
                if not maker_price_val or maker_price_val <= 0:
                    maker_price_val = self.lighter.fetch_mark_price(symbol)
                
                if maker_price_val and maker_price_val > 0:
                    # 2. Get Step Sizes
                    # X10
                    x10_step = 0.001
                    x10_m = self.x10.market_info.get(symbol)
                    if x10_m:
                        if hasattr(x10_m, 'trading_config'): # Object from SDK
                            x10_step = float(getattr(x10_m.trading_config, "min_order_size_change", 0.001))
                        elif isinstance(x10_m, dict):
                            x10_step = float(x10_m.get('lot_size', x10_m.get('min_order_qty', 0.001)))
                            
                    # Lighter
                    lighter_step = 0.01
                    lig_m = self.lighter.get_market_info(symbol) if hasattr(self.lighter, 'get_market_info') else None
                    if lig_m:
                        # Prioritize 'lot_size', then 'min_quantity', then 'size_increment'
                        lighter_step = float(lig_m.get('lot_size', lig_m.get('size_increment', lig_m.get('min_quantity', 0.01))))
                    
                    # 3. Calculate aligned size
                    target_size_usd = float(size_lighter)
                    safe_coins = calculate_common_quantity(
                        target_size_usd,
                        maker_price_val,
                        x10_step,
                        lighter_step
                    )
                    
                    if safe_coins > 0:
                        final_usd_size = safe_coins * maker_price_val
                        logger.info(f"⚖️ Size Alignment {symbol}: {target_size_usd:.2f}$ -> {final_usd_size:.2f}$ ({safe_coins:.6f} Coins) to match both exchanges (X10 Step: {x10_step}, Lighter Step: {lighter_step})")
                        
                        # Update sizes
                        size_lighter = Decimal(str(final_usd_size))
                        # Match X10 to new aligned size
                        size_x10 = size_lighter 
                    else:
                        logger.warning(f"⚠️ Size Alignment {symbol}: Calculated 0 coins? Keeping original.")

            except Exception as e:
                logger.error(f"⚠️ Size Alignment Error: {e}", exc_info=True)

            # Create execution tracker
            execution = TradeExecution(
                symbol=symbol,
                side_x10=side_x10,
                side_lighter=side_lighter,
                size_x10=float(size_x10),
                size_lighter=float(size_lighter),
                quantity_coins=float(safe_coins)
            )
            self.active_executions[symbol] = execution
            self._stats["total_executions"] += 1

            try:
                result = await self._execute_parallel_internal(
                    execution, timeout
                )
                
                success = result[0]
                if success:
                    self._stats["successful"] += 1
                else:
                    self._stats["failed"] += 1
                
                return result

            except Exception as e:
                execution.state = ExecutionState.FAILED
                execution.error = str(e)
                self._stats["failed"] += 1
                logger.error(f"❌ [PARALLEL] {symbol}: Exception: {e}", exc_info=True)
                
                return False, None, None
            
            finally:
                # Immediately cleanup execution from active list
                # This ensures failed/completed trades don't block the slot
                self.active_executions.pop(symbol, None)

    async def _calculate_dynamic_timeout(
        self, 
        symbol: str, 
        side: str, 
        trade_size_usd: float
    ) -> float:
        """
        Calculate dynamic timeout based on orderbook liquidity.
        
        Fix #13: Adjust timeout based on orderbook depth and spread.
        Higher liquidity = shorter timeout, lower liquidity = longer timeout.
        """
        try:
            # Get orderbook validation result to assess liquidity
            validation_result = await self._validate_orderbook_for_maker(
                symbol=symbol,
                side=side,
                trade_size_usd=trade_size_usd,
            )
            
            base_timeout = float(getattr(config, "LIGHTER_ORDER_TIMEOUT_SECONDS", 60.0))
            min_timeout = float(getattr(config, "MAKER_ORDER_MIN_TIMEOUT_SECONDS", 30.0))
            max_timeout = float(getattr(config, "MAKER_ORDER_MAX_TIMEOUT_SECONDS", 90.0))
            multiplier = float(getattr(config, "MAKER_ORDER_LIQUIDITY_TIMEOUT_MULTIPLIER", 0.5))
            
            if validation_result.is_valid:
                # Calculate depth ratio (how much of our order size is covered by orderbook)
                if side == "SELL":
                    depth_usd = float(validation_result.ask_depth_usd)
                else:
                    depth_usd = float(validation_result.bid_depth_usd)
                
                if depth_usd > 0:
                    # If orderbook depth is 2x our order size, use shorter timeout
                    # If orderbook depth is < 1x our order size, use longer timeout
                    depth_ratio = depth_usd / trade_size_usd
                    
                    if depth_ratio >= 2.0:
                        # High liquidity - use shorter timeout
                        timeout = base_timeout * multiplier
                    elif depth_ratio >= 1.0:
                        # Medium liquidity - use base timeout
                        timeout = base_timeout
                    else:
                        # Low liquidity - use longer timeout
                        timeout = base_timeout * (1.0 + (1.0 - depth_ratio))
                    
                    timeout = max(min_timeout, min(max_timeout, timeout))
                    logger.debug(
                        f"⏱️ {symbol}: Dynamic timeout calculated: {timeout:.1f}s "
                        f"(depth_ratio={depth_ratio:.2f}, base={base_timeout:.1f}s)"
                    )
                    return timeout
            
            # Fallback to base timeout if validation failed
            return base_timeout
            
        except Exception as e:
            logger.debug(f"⚠️ {symbol}: Error calculating dynamic timeout: {e}")
            return float(getattr(config, "LIGHTER_ORDER_TIMEOUT_SECONDS", 60.0))
    
    async def _retry_maker_order_with_adjusted_price(
        self,
        execution: TradeExecution,
        original_order_id: str,
        phase_times: Dict[str, float]
    ) -> Tuple[bool, Optional[str]]:
        """
        Retry Maker order with adjusted price after timeout.
        
        Fix #13: Retry logic for Maker Orders with price adjustment.
        
        Args:
            execution: Trade execution state
            original_order_id: Original order ID that timed out
            phase_times: Phase timing dictionary
            
        Returns:
            Tuple of (success: bool, new_order_id: Optional[str])
        """
        symbol = execution.symbol
        max_retries = int(getattr(config, "MAKER_ORDER_MAX_RETRIES", 2))
        retry_delay = float(getattr(config, "MAKER_ORDER_RETRY_DELAY_SECONDS", 2.0))
        price_adjustment_pct = float(getattr(config, "MAKER_ORDER_PRICE_ADJUSTMENT_PCT", 0.001))
        
        # Check if retries are enabled
        if max_retries <= 0:
            logger.debug(f"⏭️ {symbol}: Retries disabled (max_retries=0)")
            return False, None
        
        # Cancel original order first
        try:
            logger.info(f"🔄 [RETRY] {symbol}: Cancelling original order {original_order_id[:40]}...")
            if hasattr(self.lighter, 'cancel_limit_order'):
                await self.lighter.cancel_limit_order(original_order_id, symbol)
            else:
                await self.lighter.cancel_all_orders(symbol)
        except Exception as e:
            logger.warning(f"⚠️ [RETRY] {symbol}: Error cancelling original order: {e}")
            # Continue anyway - order might already be cancelled
        
        # Wait before retry
        await asyncio.sleep(retry_delay)
        
        # Get current market price for price adjustment
        try:
            # Fix #13: fetch_mark_price is synchronous, not async
            current_price = self.lighter.fetch_mark_price(symbol)
            if current_price is None or current_price <= 0:
                logger.warning(f"⚠️ [RETRY] {symbol}: Invalid mark price ({current_price}), skipping retry")
                return False, None
        except Exception as e:
            logger.warning(f"⚠️ [RETRY] {symbol}: Error fetching mark price: {e}, skipping retry")
            return False, None
        
        # Retry loop
        for retry_attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    f"🔄 [RETRY] {symbol}: Attempt {retry_attempt}/{max_retries} "
                    f"(price adjustment: {price_adjustment_pct * 100 * retry_attempt:.3f}%)"
                )
                
                # Calculate adjusted price (more aggressive for each retry)
                if execution.side_lighter == "SELL":
                    # For SELL orders, lower the price to increase fill probability
                    adjusted_price = current_price * (1.0 - (price_adjustment_pct * retry_attempt))
                else:
                    # For BUY orders, raise the price to increase fill probability
                    adjusted_price = current_price * (1.0 + (price_adjustment_pct * retry_attempt))
                
                logger.debug(
                    f"💰 [RETRY] {symbol}: Original price={current_price:.6f}, "
                    f"Adjusted price={adjusted_price:.6f} ({execution.side_lighter})"
                )
                
                # Re-validate orderbook before retry
                validation_result = await self._validate_orderbook_for_maker(
                    symbol=symbol,
                    side=execution.side_lighter,
                    trade_size_usd=execution.size_lighter,
                )
                
                if not validation_result.is_valid:
                    logger.warning(
                        f"⚠️ [RETRY] {symbol}: Orderbook validation failed on retry {retry_attempt}: "
                        f"{validation_result.reason}"
                    )
                    # Wait a bit longer before next retry
                    await asyncio.sleep(retry_delay * retry_attempt)
                    continue
                
                # Calculate dynamic timeout for retry
                dynamic_timeout = await self._calculate_dynamic_timeout(
                    symbol=symbol,
                    side=execution.side_lighter,
                    trade_size_usd=execution.size_lighter,
                )
                
                # Place new order with adjusted price
                # Note: We need to use the adjusted price in the order placement
                # This requires modifying the _execute_lighter_leg to accept a price parameter
                # For now, we'll use the existing method and let it calculate the price
                # The price adjustment will be handled by the maker price calculation
                
                # Place retry order
                retry_success, retry_order_id = await self._execute_lighter_leg(
                    symbol,
                    execution.side_lighter,
                    execution.size_lighter,
                    post_only=True,
                    amount_coins=execution.quantity_coins,
                    # TODO: Add price parameter to _execute_lighter_leg for explicit price control
                )
                
                if not retry_success or not retry_order_id:
                    logger.warning(f"⚠️ [RETRY] {symbol}: Retry order placement failed (attempt {retry_attempt})")
                    await asyncio.sleep(retry_delay * retry_attempt)
                    continue
                
                logger.info(f"✅ [RETRY] {symbol}: Retry order placed: {retry_order_id[:40]}...")
                
                # Wait for fill with dynamic timeout
                wait_start = time.time()
                filled = False
                check_count = 0
                
                while time.time() - wait_start < dynamic_timeout:
                    if getattr(config, "IS_SHUTTING_DOWN", False):
                        logger.warning(f"⚡ [RETRY] {symbol}: SHUTDOWN detected - aborting retry wait!")
                        break
                    
                    check_count += 1
                    try:
                        pos = await self.lighter.fetch_open_positions()
                        p = next((x for x in (pos or []) if x.get("symbol") == symbol), None)
                        current_size = safe_float(p.get("size", 0)) if p else 0.0
                        
                        if execution.quantity_coins > 0 and abs(current_size) >= execution.quantity_coins * 0.95:
                            filled = True
                            logger.info(
                                f"✅ [RETRY] {symbol}: Fill detected after {check_count} checks "
                                f"(attempt {retry_attempt}/{max_retries})!"
                            )
                            break
                        
                        await asyncio.sleep(0.5)
                        
                    except Exception as e:
                        logger.debug(f"[RETRY] {symbol}: Check #{check_count} error: {e}")
                        await asyncio.sleep(1)
                
                if filled:
                    # Update phase times
                    phase_times["lighter_fill_wait"] = time.time() - wait_start
                    phase_times["maker_retry_attempt"] = retry_attempt
                    return True, retry_order_id
                else:
                    logger.warning(
                        f"⏰ [RETRY] {symbol}: Retry order timeout after {dynamic_timeout:.1f}s "
                        f"(attempt {retry_attempt}/{max_retries})"
                    )
                    # Cancel retry order before next attempt
                    try:
                        if hasattr(self.lighter, 'cancel_limit_order'):
                            await self.lighter.cancel_limit_order(retry_order_id, symbol)
                        else:
                            await self.lighter.cancel_all_orders(symbol)
                    except Exception as e:
                        logger.debug(f"⚠️ [RETRY] {symbol}: Error cancelling retry order: {e}")
                    
                    # Wait before next retry
                    if retry_attempt < max_retries:
                        await asyncio.sleep(retry_delay * (retry_attempt + 1))
                
            except Exception as e:
                logger.error(f"❌ [RETRY] {symbol}: Error during retry attempt {retry_attempt}: {e}")
                if retry_attempt < max_retries:
                    await asyncio.sleep(retry_delay * (retry_attempt + 1))
                continue
        
        # All retries failed
        logger.warning(f"❌ [RETRY] {symbol}: All {max_retries} retry attempts failed")
        return False, None

    async def _handle_maker_timeout(self, execution, lighter_order_id) -> bool:
        symbol = execution.symbol
        logger.warning(f"⏰ [MAKER STRATEGY] {symbol}: Wait timeout! Cancelling Lighter order {lighter_order_id}...")

        # 1. Cancel Order
        cancel_returned_404 = False
        try:
            if hasattr(self.lighter, 'cancel_limit_order'):
                await self.lighter.cancel_limit_order(lighter_order_id, symbol)
            else:
                await self.lighter.cancel_all_orders(symbol)
        except Exception as e:
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str:
                cancel_returned_404 = True
                logger.warning(f"⚠️ Cancel {symbol}: Order Not Found (404). Flagging for Trade History check.")
            else:
                logger.error(f"Cancel failed for {symbol}: {e}")

        # ═══════════════════════════════════════════════════════════════
        # RACE CONDITION FIX: Atomic Status Check with RETRY LOOP ("Paranoid Mode")
        # ═══════════════════════════════════════════════════════════════
        # -----------------------------------------------------------------------
        # FIX: 404 Handling & Trade History Check
        # -----------------------------------------------------------------------
        filled = False
        check_history = False

        try:
            # 1. Versuche Order-Status zu holen
            order_info = await self.lighter.get_order(lighter_order_id, symbol)
            
            if order_info:
                status = order_info.get('status', '').upper()
                filled_amount = float(order_info.get('filledAmount', 0) or order_info.get('executedQty', 0))
                
                if status in ['FILLED', 'PARTIALLY_FILLED'] or filled_amount > 0:
                    logger.warning(f"⚠️ [MAKER STRATEGY] Order {lighter_order_id} found as {status} (Size: {filled_amount})")
                    filled = True
                elif status == 'CANCELED':
                    logger.info(f"✓ Order confirmed CANCELED via API.")
                    filled = False
            else:
                # API liefert None zurück -> Behandle wie 404
                check_history = True

        except Exception as e:
            # 2. Fehlerbehandlung (z.B. 404 Not Found)
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str:
                logger.warning(f"❓ Order {lighter_order_id} returned 404. Status unklar. Checking Fills...")
                check_history = True
            else:
                logger.error(f"Error checking status: {e}")
                # Im Zweifel gehen wir vom schlimmsten aus und prüfen Positionen
                check_history = True

        # 3. Wenn Status unklar (404) ODER Cancel 404 gab, prüfe Trade History (Fills)
        if (check_history or cancel_returned_404) and not filled:
            try:
                # Hole die letzten 10 Trades für dieses Symbol
                # WICHTIG: Deine Adapter-Methode muss 'fetch_my_trades' unterstützen!
                recent_trades = await self.lighter.fetch_my_trades(symbol, limit=10)
                
                # Suche nach Trades, die zu unserer Order ID gehören
                # (Achte darauf, dass IDs Strings oder Ints sein können, daher str())
                my_fills = [
                    t for t in recent_trades 
                    if str(t.get('orderId', '')) == str(lighter_order_id) or 
                       str(t.get('order_id', '')) == str(lighter_order_id)
                ]

                if my_fills:
                    fill_sum = sum(float(t.get('amount', 0) or t.get('qty', 0)) for t in my_fills)
                    logger.warning(f"🚨 404 TRAP: Order returned 404 but found {len(my_fills)} FILLS in history! Sum: {fill_sum}")
                    filled = True
                else:
                    logger.info(f"✓ 404 Double-Check: No fills found in history. Confirmed Cancelled.")
                    filled = False
                    
            except Exception as hist_e:
                logger.error(f"Could not verify trade history: {hist_e}. Fallback to Position Check.")
                # Fallback: Wenn wir History nicht lesen können, verlassen wir uns auf den 
                # "Paranoid Check" (Position Check), der gleich danach kommt.
        
        # -----------------------------------------------------------------------
        # Ende des Fixes
        # -----------------------------------------------------------------------

        if filled:
             logger.warning(f"⚠️ [MAKER STRATEGY] {symbol}: Order FILLED during cancel race! Proceeding to Hedge.")
        else:
                # FAST SHUTDOWN: Skip extended checks during shutdown
                is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
                extended_checks_done = 0
                extended_checks_skipped = False
                
                if is_shutting_down:
                    logger.warning(f"⚡ [MAKER STRATEGY] {symbol}: SHUTDOWN - skipping extended ghost fill checks!")
                    extended_checks_skipped = True
                else:
                    # 2. FIX: Der "Paranoid Check" muss aggressiver sein (User Request)
                    logger.info(f"🔍 [MAKER STRATEGY] {symbol}: Checking for Ghost Fills (EXTENDED CHECK)...")
                    
                    # Erhöht von 10 auf 30 Versuche mit ansteigendem Delay (insg. ~60 Sekunden Abdeckung)
                    max_checks = 30
                    for i in range(max_checks): 
                        # FAST SHUTDOWN: Abort immediately if shutdown detected mid-loop
                        if getattr(config, 'IS_SHUTTING_DOWN', False):
                            logger.warning(f"⚡ {symbol}: SHUTDOWN detected during ghost check - aborting!")
                            extended_checks_skipped = True
                            break
                            
                        # Backoff: Wartet 1s, 1.2s, 1.4s ... bis max 3s
                        wait_time = min(1.0 + (i * 0.2), 3.0)
                        await asyncio.sleep(wait_time)
                        extended_checks_done += 1
                        
                        try:
                            positions = await self.lighter.fetch_open_positions()
                            
                            # Suche nach Position in diesem Symbol
                            pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                            size = safe_float(pos.get('size', 0)) if pos else 0.0
                            
                            if abs(size) > 1e-8:
                                logger.warning(f"⚠️ [MAKER STRATEGY] {symbol}: GHOST FILL DETECTED on attempt {i+1}! Size={size}. HEDGING NOW!")
                                filled = True
                                break
                            
                            # Logge nur alle 5 Versuche, um Spam zu vermeiden
                            if i % 5 == 0:
                                logger.debug(f"🔍 {symbol} check {i+1}/{max_checks} clean...")
                                
                        except Exception as e:
                            logger.debug(f"Check error: {e}")
        


        if filled:
            logger.info(f"✅ [MAKER STRATEGY] {symbol}: Lighter Filled! Executing X10 Hedge...")
            return True
        else:
            # FIX: Log-Nachricht akkurat basierend auf tatsächlich durchgeführten Checks
            if extended_checks_skipped:
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit - extended checks skipped during shutdown).")
            elif extended_checks_done > 0:
                # Berechne ungefähre Zeit basierend auf Checks (Backoff: 1.0s, 1.2s, 1.4s... bis 3.0s max)
                estimated_seconds = sum(min(1.0 + (i * 0.2), 3.0) for i in range(extended_checks_done))
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit verified after ~{estimated_seconds:.1f}s, {extended_checks_done} checks).")
            else:
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit - no extended verification needed).")
            return False

    async def _execute_parallel_internal(
        self, 
        execution: TradeExecution,
        timeout: float
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Internal parallel execution logic with comprehensive logging and timeout handling"""
        symbol = execution.symbol
        trade_start_time = time.monotonic()

        # Track all phases for final summary
        phase_times: Dict[str, float] = {}

        try:
            # ═══════════════════════════════════════════════════════════════
            # PHASE 0: ORDERBOOK VALIDATION
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()
            logger.info(f"📋 [PHASE 0] {symbol}:  Validating orderbook...")

            if getattr(config, "OB_VALIDATION_ENABLED", True):
                validation_result = await self._validate_orderbook_for_maker(
                    symbol=symbol,
                    side=execution.side_lighter,
                    trade_size_usd=execution.size_lighter,
                )

                phase_times["orderbook_validation"] = time.monotonic() - phase_start

                if not validation_result.is_valid:
                    logger.warning(
                        f"❌ [PHASE 0] {symbol}:  Orderbook validation FAILED - {validation_result.reason}"
                    )
                    self._log_trade_summary(
                        symbol,
                        "ABORTED",
                        "Orderbook validation failed",
                        trade_start_time,
                        phase_times,
                        execution,
                    )
                    execution.state = ExecutionState.FAILED
                    execution.error = f"Orderbook invalid: {validation_result.reason}"
                    return False, None, None
                else:
                    logger.info(
                        f"✅ [PHASE 0] {symbol}: Orderbook validation PASSED ({phase_times['orderbook_validation']:.2f}s)"
                    )

            # ═══════════════════════════════════════════════════════════════
            # PHASE 1: LIGHTER MAKER ORDER
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()
            logger.info(f"📤 [PHASE 1] {symbol}: Placing Lighter {execution.side_lighter} order...")
            logger.info(f"   Size: ${execution.size_lighter:.2f} ({execution.quantity_coins:.6f} coins)")
            execution.state = ExecutionState.LEG1_SENT

            lighter_success, lighter_order_id = await self._execute_lighter_leg(
                symbol,
                execution.side_lighter,
                execution.size_lighter,
                post_only=True,
                amount_coins=execution.quantity_coins,
            )

            phase_times["lighter_order_placement"] = time.monotonic() - phase_start
            execution.lighter_order_id = lighter_order_id

            if not lighter_success or not lighter_order_id:
                logger.error(
                    f"❌ [PHASE 1] {symbol}: Lighter order FAILED ({phase_times['lighter_order_placement']:.2f}s)"
                )
                self._log_trade_summary(
                    symbol,
                    "ABORTED",
                    "Lighter placement failed",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                execution.state = ExecutionState.FAILED
                execution.error = "Lighter Placement Failed"
                return False, None, None

            logger.info(
                f"✅ [PHASE 1] {symbol}: Lighter order placed ({phase_times['lighter_order_placement']:.2f}s)"
            )
            logger.info(f"   Order ID: {lighter_order_id[:40]}...")

            # ═══════════════════════════════════════════════════════════════
            # PHASE 1.5: WAIT FOR LIGHTER FILL
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()
            is_shutting_down = getattr(config, "IS_SHUTTING_DOWN", False)
            
            # Fix #13: Use dynamic timeout based on orderbook liquidity
            if is_shutting_down:
                max_wait_seconds = 2.0
            else:
                max_wait_seconds = await self._calculate_dynamic_timeout(
                    symbol=symbol,
                    side=execution.side_lighter,
                    trade_size_usd=execution.size_lighter,
                )

            logger.info(f"⏳ [PHASE 1.5] {symbol}: Waiting for Lighter fill (max {max_wait_seconds:.1f}s, dynamic timeout)...")

            filled = False
            wait_start = time.time()
            check_count = 0

            while time.time() - wait_start < max_wait_seconds:
                if getattr(config, "IS_SHUTTING_DOWN", False):
                    logger.warning(f"⚡ [PHASE 1.5] {symbol}:  SHUTDOWN detected - aborting wait!")
                    break

                check_count += 1
                try:
                    pos = await self.lighter.fetch_open_positions()
                    p = next((x for x in (pos or []) if x.get("symbol") == symbol), None)
                    current_size = safe_float(p.get("size", 0)) if p else 0.0

                    if execution.quantity_coins > 0 and abs(current_size) >= execution.quantity_coins * 0.95:
                        filled = True
                        logger.info(f"✅ [PHASE 1.5] {symbol}: Fill detected after {check_count} checks!")
                        break

                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.debug(f"[PHASE 1.5] {symbol}: Check #{check_count} error: {e}")
                    await asyncio.sleep(1)

            phase_times["lighter_fill_wait"] = time.monotonic() - phase_start

            if not filled:
                # Fix #13: Enhanced timeout logging with orderbook analysis
                try:
                    # Get orderbook state for better diagnostics
                    validation_result = await self._validate_orderbook_for_maker(
                        symbol=symbol,
                        side=execution.side_lighter,
                        trade_size_usd=execution.size_lighter,
                    )
                    
                    if validation_result.is_valid:
                        depth_info = (
                            f"bid_depth=${float(validation_result.bid_depth_usd):.2f}, "
                            f"ask_depth=${float(validation_result.ask_depth_usd):.2f}, "
                            f"spread={validation_result.spread_percent*100 if validation_result.spread_percent else 0:.3f}%"
                        )
                    else:
                        depth_info = f"validation_failed: {validation_result.reason}"
                    
                    logger.warning(
                        f"⏰ [PHASE 1.5] {symbol}: Fill timeout after {phase_times['lighter_fill_wait']:.2f}s "
                        f"(orderbook: {depth_info})"
                    )
                except Exception as e:
                    logger.warning(
                        f"⏰ [PHASE 1.5] {symbol}: Fill timeout after {phase_times['lighter_fill_wait']:.2f}s "
                        f"(diagnostics error: {e})"
                    )
                
                # Fix #13: Retry logic for Maker Orders
                retry_success, retry_order_id = await self._retry_maker_order_with_adjusted_price(
                    execution, lighter_order_id, phase_times
                )
                
                if retry_success and retry_order_id:
                    # Retry succeeded - update order ID and continue
                    lighter_order_id = retry_order_id
                    filled = True
                    logger.info(f"✅ [PHASE 1.5] {symbol}: Retry order FILLED after timeout")
                else:
                    # Check if original order was filled during cancel (race condition)
                    filled = await self._handle_maker_timeout(execution, lighter_order_id)

                    if not filled:
                        self._log_trade_summary(
                            symbol,
                            "TIMEOUT",
                            "Lighter order not filled (no retries available)",
                            trade_start_time,
                            phase_times,
                            execution,
                        )
                        execution.state = ExecutionState.FAILED
                        return False, None, lighter_order_id

            execution.lighter_filled = True
            logger.info(f"✅ [PHASE 1.5] {symbol}: Lighter FILLED ({phase_times['lighter_fill_wait']:.2f}s)")

            # ═══════════════════════════════════════════════════════════════
            # PHASE 2: X10 HEDGE ORDER
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()
            filled_size_coins = execution.quantity_coins

            logger.info(f"📤 [PHASE 2] {symbol}: Placing X10 {execution.side_x10} hedge...")
            logger.info(f"   Size: {filled_size_coins:.6f} coins (matching Lighter fill)")

            execution.state = ExecutionState.LEG2_SENT

            x10_success, x10_order_id = await self._execute_x10_leg(
                symbol,
                execution.side_x10,
                size_type="COINS",
                size_value=filled_size_coins,
                post_only=False,
            )

            phase_times["x10_hedge"] = time.monotonic() - phase_start
            execution.x10_order_id = x10_order_id
            execution.x10_filled = x10_success

            if x10_success:
                logger.info(f"✅ [PHASE 2] {symbol}:  X10 hedge FILLED ({phase_times['x10_hedge']:.2f}s)")
                logger.info(f"   Order ID: {x10_order_id}")

                execution.state = ExecutionState.COMPLETE
                self._log_trade_summary(
                    symbol,
                    "SUCCESS",
                    "Hedged trade complete",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                return True, x10_order_id, lighter_order_id
            else:
                logger.error(f"❌ [PHASE 2] {symbol}: X10 hedge FAILED ({phase_times['x10_hedge']:.2f}s)")
                logger.warning(f"🔄 {symbol}: Initiating rollback - Lighter position exposed!")

                await self._queue_rollback(execution)
                self._log_trade_summary(
                    symbol,
                    "ROLLBACK",
                    "X10 hedge failed",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                return False, None, lighter_order_id

        except asyncio.TimeoutError:
            logger.error(f"❌ {symbol}: Trade execution timeout!")
            self._log_trade_summary(
                symbol,
                "TIMEOUT",
                "Execution timeout",
                trade_start_time,
                phase_times,
                execution,
            )
            execution.state = ExecutionState.FAILED
            execution.error = "Execution timeout"
            return False, None, None

        except Exception as e:
            logger.error(f"❌ {symbol}: Unexpected error: {e}", exc_info=True)
            self._log_trade_summary(
                symbol,
                "ERROR",
                str(e),
                trade_start_time,
                phase_times,
                execution,
            )
            execution.state = ExecutionState.FAILED
            execution.error = str(e)
            return False, None, None


    def _log_trade_summary(
        self,
        symbol: str,
        result: str,
        reason: str,
        start_time: float,
        phase_times: Dict[str, float],
        execution: TradeExecution,
    ):
        """Log comprehensive trade execution summary"""
        total_time = time.monotonic() - start_time

        logger.info("═" * 60)
        logger.info(f"📊 TRADE SUMMARY:  {symbol}")
        logger.info(f"   Result: {result}")
        logger.info(f"   Reason:  {reason}")
        logger.info(f"   Total Time: {total_time:.2f}s")
        logger.info(f"   State: {execution.state.value}")

        if phase_times:
            logger.info("   Phase Times:")
            for phase, duration in phase_times.items():
                logger.info(f"      - {phase}: {duration:.2f}s")

        if execution.lighter_order_id:
            logger.info(f"   Lighter Order:  {execution.lighter_order_id[:40]}...")
        if execution.x10_order_id:
            logger.info(f"   X10 Order: {execution.x10_order_id}")

        logger.info("═" * 60)




    async def _wait_for_lighter_fill(
        self, symbol: str, execution: TradeExecution, lighter_order_id: str, max_wait_seconds: float
    ) -> bool:
        """Wait for Lighter order to fill (polling-based)
        
        Market orders (IOC) should fill immediately, so we use a shorter interval
        for faster detection. POST_ONLY orders may take longer, so we use a longer interval.
        Also checks if IOC order was cancelled (not in open orders).
        """
        wait_start = time.time()
        check_count = 0
        # Use longer polling interval for POST_ONLY orders (they may take time to fill)
        # Check if order is POST_ONLY by checking if it's still in open orders after initial delay
        polling_interval = 1.0  # Longer interval for POST_ONLY orders (can be adjusted based on order type)
        # For IOC orders, check order status early to detect cancellations
        order_status_checked = False

        while time.time() - wait_start < max_wait_seconds:
            if getattr(config, "IS_SHUTTING_DOWN", False):
                logger.warning(f"⚡ [LIGHTER FILL] {symbol}: SHUTDOWN detected - aborting wait!")
                return False

            check_count += 1
            try:
                # Check position first (fastest path)
                pos = await self.lighter.fetch_open_positions()
                p = next((x for x in (pos or []) if x.get("symbol") == symbol), None)
                current_size = safe_float(p.get("size", 0)) if p else 0.0
                is_ghost = p.get("is_ghost", False) if p else False

                # Check if position exists:
                # 1. Ghost Guardian position (is_ghost=True) means order is pending and will fill
                # 2. Real position with at least 50% of expected size (more lenient for faster detection)
                if execution.quantity_coins > 0 and (is_ghost or abs(current_size) >= execution.quantity_coins * 0.50):
                    fill_time = time.time() - wait_start
                    if is_ghost:
                        logger.info(f"✅ [LIGHTER FILL] {symbol}: Fill detected via Ghost Guardian after {check_count} checks ({fill_time:.2f}s)! (Order pending, position will appear in API soon)")
                    else:
                        logger.info(f"✅ [LIGHTER FILL] {symbol}: Fill detected after {check_count} checks ({fill_time:.2f}s)! (size={abs(current_size):.6f}, expected={execution.quantity_coins:.6f})")
                    return True

                # For IOC orders, check if order still exists after initial delay
                # IOC orders that don't fill immediately are cancelled
                # BUT: If order is gone, it might be filled (position exists) OR cancelled (no position)
                # We need to do a fresh fetch to bypass rate limiter deduplication
                # However, positions may take time to appear in API, so we check multiple times
                if not order_status_checked and time.time() - wait_start > 1.0:
                    try:
                        open_orders = await self.lighter.get_open_orders(symbol)
                        # Extract order IDs (could be full hash or truncated)
                        order_id_short = lighter_order_id[:20] if lighter_order_id else ""
                        order_found = any(
                            str(o.get("id", "")).startswith(order_id_short) or 
                            str(o.get("id", "")).endswith(order_id_short[-20:])
                            for o in open_orders
                        )
                        
                        if not order_found:
                            # Order is not in open orders - could be filled OR cancelled
                            # Do multiple fresh position fetches with delays to account for API lag
                            # Positions may take 1-3 seconds to appear after IOC order fills
                            # For POST_ONLY orders, positions may take longer (5-10s), so we need more attempts
                            # Check if this is a POST_ONLY order: if order was placed >5s ago and disappeared, likely POST_ONLY filled
                            elapsed_so_far = time.time() - wait_start
                            is_post_only_likely = elapsed_so_far > 5.0
                            max_fresh_fetch_attempts = 10 if is_post_only_likely else 3  # More attempts for POST_ONLY
                            fresh_fetch_attempts = 0
                            fresh_fetch_delay = 0.5
                            
                            while fresh_fetch_attempts < max_fresh_fetch_attempts:
                                cached_positions = None
                                if hasattr(self.lighter, '_positions_cache'):
                                    # Set cache to None temporarily to force fresh API call
                                    cached_positions = self.lighter._positions_cache
                                    self.lighter._positions_cache = None
                                
                                try:
                                    await asyncio.sleep(fresh_fetch_delay * fresh_fetch_attempts)  # Delay increases with each attempt
                                    fresh_pos = await self.lighter.fetch_open_positions()
                                    fresh_p = next((x for x in (fresh_pos or []) if x.get("symbol") == symbol), None)
                                    fresh_size = safe_float(fresh_p.get("size", 0)) if fresh_p else 0.0
                                    is_ghost = fresh_p.get("is_ghost", False) if fresh_p else False
                                    
                                    # Check if position exists:
                                    # 1. Ghost Guardian position (is_ghost=True) means order is pending and will fill
                                    # 2. Real position with at least 50% of expected size
                                    if is_ghost:
                                        # Ghost Guardian position = order is pending, consider it filled
                                        fill_time = time.time() - wait_start
                                        logger.info(f"✅ [LIGHTER FILL] {symbol}: Fill confirmed via Ghost Guardian (attempt {fresh_fetch_attempts + 1}) after {fill_time:.2f}s (IOC order disappeared, Ghost Guardian detected pending position - will appear in API soon)!")
                                        # Restore cache with fresh data
                                        if hasattr(self.lighter, '_positions_cache'):
                                            self.lighter._positions_cache = fresh_pos if fresh_pos else cached_positions
                                        return True  # Fill confirmed, exit immediately
                                    elif abs(fresh_size) >= execution.quantity_coins * 0.50:
                                        # Position exists with sufficient size! Order was filled
                                        fill_time = time.time() - wait_start
                                        logger.info(f"✅ [LIGHTER FILL] {symbol}: Fill confirmed via fresh fetch (attempt {fresh_fetch_attempts + 1}) after {fill_time:.2f}s (IOC order disappeared but position exists)! (size={abs(fresh_size):.6f}, expected={execution.quantity_coins:.6f})")
                                        # Restore cache with fresh data
                                        if hasattr(self.lighter, '_positions_cache'):
                                            self.lighter._positions_cache = fresh_pos if fresh_pos else cached_positions
                                        return True  # Fill confirmed, exit immediately
                                    else:
                                        fresh_fetch_attempts += 1
                                        # Restore cache before next attempt
                                        if hasattr(self.lighter, '_positions_cache'):
                                            self.lighter._positions_cache = cached_positions
                                        if fresh_fetch_attempts >= max_fresh_fetch_attempts:
                                            # No position found after all fresh fetch attempts
                                            # For POST_ONLY orders, continue polling instead of giving up immediately
                                            # (POST_ONLY orders can take time to fill and appear in API)
                                            elapsed = time.time() - wait_start
                                            if is_post_only_likely:
                                                # POST_ONLY order: continue polling instead of giving up
                                                logger.debug(f"⏳ [LIGHTER FILL] {symbol}: POST_ONLY order not found in open orders and no position after {max_fresh_fetch_attempts} fresh fetch attempts ({elapsed:.2f}s) - continuing to poll (POST_ONLY may fill later)")
                                                # Restore cache and continue polling
                                                if hasattr(self.lighter, '_positions_cache'):
                                                    self.lighter._positions_cache = cached_positions
                                                order_status_checked = True  # Don't check again, just continue polling
                                                break  # Exit fresh fetch loop, continue normal polling
                                            else:
                                                # IOC order: likely cancelled/rejected
                                                logger.warning(f"❌ [LIGHTER FILL] {symbol}: IOC order not found in open orders and no position after {max_fresh_fetch_attempts} fresh fetch attempts ({elapsed:.2f}s) - cancelled/rejected")
                                                return False
                                except Exception as fresh_fetch_error:
                                    logger.debug(f"[LIGHTER FILL] {symbol}: Fresh fetch attempt {fresh_fetch_attempts + 1} error: {fresh_fetch_error}")
                                    # Restore cache on error
                                    if hasattr(self.lighter, '_positions_cache') and cached_positions is not None:
                                        self.lighter._positions_cache = cached_positions
                                    fresh_fetch_attempts += 1
                                    if fresh_fetch_attempts >= max_fresh_fetch_attempts:
                                        # Continue polling if all fresh fetch attempts fail
                                        break
                        else:
                            # Order still exists, continue normal polling
                            order_status_checked = True
                    except Exception as e:
                        logger.debug(f"[LIGHTER FILL] {symbol}: Order status check error: {e}")
                        # Continue polling if check fails
                        order_status_checked = True  # Mark as checked to avoid repeated errors

                await asyncio.sleep(polling_interval)
            except Exception as e:
                logger.debug(f"[LIGHTER FILL] {symbol}: Check #{check_count} error: {e}")
                await asyncio.sleep(polling_interval)

        logger.warning(f"⏰ [LIGHTER FILL] {symbol}: Fill timeout after {max_wait_seconds:.2f}s")
        return False

    async def _wait_for_x10_fill(
        self, symbol: str, execution: TradeExecution, x10_order_id: str, max_wait_seconds: float = 60.0
    ) -> bool:
        """Wait for X10 limit order to fill (polling-based via WebSocket position updates)"""
        wait_start = time.time()
        check_count = 0
        polling_interval = 0.5  # Check every 0.5s
        
        while time.time() - wait_start < max_wait_seconds:
            if getattr(config, "IS_SHUTTING_DOWN", False):
                logger.warning(f"⚡ [X10 FILL] {symbol}: SHUTDOWN detected - aborting wait!")
                return False
            
            check_count += 1
            try:
                # Check position cache (updated via WebSocket on_position_update)
                # NOTE: _positions_cache is a LIST of position dicts, not a dict!
                if hasattr(self.x10, '_positions_cache') and self.x10._positions_cache:
                    # Search for position in the list
                    pos = next(
                        (p for p in self.x10._positions_cache if p.get('symbol') == symbol),
                        None
                    )
                    if pos:
                        size = safe_float(pos.get('size', 0))
                        # Check if position matches expected size (at least 95%)
                        if abs(size) >= execution.quantity_coins * 0.95:
                            fill_time = time.time() - wait_start
                            logger.info(f"✅ [X10 FILL] {symbol}: Fill detected after {check_count} checks ({fill_time:.2f}s)!")
                            return True
                
                await asyncio.sleep(polling_interval)
            except Exception as e:
                logger.debug(f"[X10 FILL] {symbol}: Check #{check_count} error: {e}")
                await asyncio.sleep(polling_interval)
        
        logger.warning(f"⏰ [X10 FILL] {symbol}: Fill timeout after {max_wait_seconds:.2f}s")
        return False

    async def _execute_lighter_leg(
        self, symbol: str, side: str, notional_usd: float, post_only: bool, amount_coins: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """Execute Lighter leg with error handling"""
        try:
            return await self.lighter.open_live_position(
                symbol, side, notional_usd, post_only=post_only, amount=amount_coins
            )
        except Exception as e:
            logger.error(f"Lighter leg error {symbol}: {e}")
            return False, None

    async def _execute_x10_leg(
        self, 
        symbol: str, 
        side: str, 
        size_type: str,   # "USD" or "COINS"
        size_value: float, 
        post_only: bool
    ) -> Tuple[bool, Optional[str]]:
        """Execute X10 leg with error handling"""
        try:
            qty_coins = 0.0
            notional_usd = 0.0
            amount_arg = None

            # Preis holen für Logging / Fallback
            price = self.x10.fetch_mark_price(symbol)
            
            if size_type == "COINS":
                qty_coins = size_value
                amount_arg = qty_coins
                # Logging
                logger.info(f"🚀 X10 EXECUTE {symbol} {side}: {qty_coins:.6f} Coins (Basis: {size_type}={size_value})")
            else:
                # USD Mode
                notional_usd = size_value
                if price and price > 0:
                    est_coins = size_value / price
                    logger.info(f"🚀 X10 EXECUTE {symbol} {side}: ~{est_coins:.6f} Coins (Basis: {size_type}={size_value})")
                else:
                    logger.info(f"🚀 X10 EXECUTE {symbol} {side}: ${size_value:.2f} (Basis: USD)")

            # IMPORTANT: We pass 'amount' if we have coins, otherwise 'notional_usd'
            # open_live_position logic: if amount provided, uses it. else divides notional_usd by price.
            return await self.x10.open_live_position(
                symbol, side, notional_usd=notional_usd, post_only=post_only, amount=amount_arg
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
        execution.state = ExecutionState.ROLLBACK_QUEUED
        self._stats["rollbacks_triggered"] += 1
        await self._rollback_queue.put(execution)
        logger.info(f"📤 [ROLLBACK] {execution.symbol}: Queued for background processing")

    async def _execute_rollback_with_retry(self, execution: TradeExecution) -> bool:
        """Execute rollback with exponential backoff retry"""
        symbol = execution.symbol
        
        logger.warning(f"═══════════════════════════════════════════════════════════")
        logger.warning(f"🔄 ROLLBACK STARTED: {symbol}")
        logger.warning(f"   Lighter Filled: {execution.lighter_filled}")
        logger.warning(f"   X10 Filled: {execution.x10_filled}")
        logger.warning(f"═══════════════════════════════════════════════════════════")

        # Initial settlement delay
        logger.info(f"⏳ {symbol}: Waiting 3s for order settlement before rollback...")
        await asyncio.sleep(3.0)

        for attempt in range(self.MAX_ROLLBACK_ATTEMPTS):
            try:
                execution.rollback_attempts = attempt + 1
                delay = self.ROLLBACK_BASE_DELAY * (2 ** attempt)
                
                if attempt > 0:
                    logger.warning(f"🔄 ROLLBACK RETRY {symbol}: Attempt {attempt + 1}/{self.MAX_ROLLBACK_ATTEMPTS} after {delay}s delay")
                    await asyncio.sleep(delay)

                success = False
                
                if execution.lighter_filled and not execution.x10_filled:
                    logger.info(f"🔄 {symbol}: Lighter filled but X10 failed -> Rolling back Lighter")
                    success = await self._rollback_lighter(execution)
                elif execution.x10_filled and not execution.lighter_filled:
                    logger.info(f"🔄 {symbol}: X10 filled but Lighter failed -> Rolling back X10")
                    success = await self._rollback_x10(execution)
                else:
                    # Edge case: both or neither - shouldn't happen
                    logger.warning(f"⚠️ ROLLBACK {symbol}: Unexpected state (lighter={execution.lighter_filled}, x10={execution.x10_filled})")
                    return True

                if success:
                    logger.info(f"═══════════════════════════════════════════════════════════")
                    logger.info(f"✅ ROLLBACK COMPLETE: {symbol} (attempt {attempt + 1})")
                    logger.info(f"═══════════════════════════════════════════════════════════")
                    return True

            except Exception as e:
                logger.error(f"❌ ROLLBACK ERROR {symbol}: Attempt {attempt + 1} failed: {e}")

        # All attempts failed - CRITICAL
        logger.critical(f"═══════════════════════════════════════════════════════════")
        logger.critical(f"🚨 ROLLBACK FAILED: {symbol} after {self.MAX_ROLLBACK_ATTEMPTS} attempts!")
        logger.critical(f"🚨 NAKED LEG RISK! MANUAL INTERVENTION REQUIRED!")
        logger.critical(f"═══════════════════════════════════════════════════════════")
        
        # Critical failure - log emergency
        logger.critical(f"🚨 EMERGENCY: Manual intervention required for {symbol}!")
        return False

    async def _rollback_x10(self, execution: TradeExecution) -> bool:
        """
        Rollback X10 position with actual position verification.
        
        This is called when Lighter leg fails after X10 order filled.
        We need to close the X10 position to prevent naked exposure.
        """
        symbol = execution.symbol
        
        logger.warning(f"🔄 ROLLBACK: Closing X10 {symbol} position...")
        
        try:
            positions = await self.x10.fetch_open_positions()
            pos = next(
                (p for p in (positions or []) 
                 if p.get('symbol') == symbol and abs(safe_float(p.get('size', 0))) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✅ ROLLBACK {symbol}: No X10 position found (already closed?)")
                return True

            actual_size = safe_float(pos.get('size', 0))
            # Positive = LONG, Negative = SHORT
            position_side = "BUY" if actual_size > 0 else "SELL"
            close_side = "SELL" if actual_size > 0 else "BUY"
            close_size = abs(actual_size)

            logger.warning(
                f"🔄 ROLLBACK {symbol}: Closing X10 {position_side} position "
                f"({close_size:.6f} coins) with {close_side} market order"
            )

            success, order_id = await self.x10.close_live_position(
                symbol, position_side, close_size
            )

            if success:
                logger.info(f"✅ ROLLBACK SUCCESS: X10 position {symbol} closed (order: {order_id})")
                return True
            else:
                logger.critical(f"🚨 ROLLBACK FAILED: X10 {symbol} close_live_position returned False!")
                logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED for {symbol}!")
                return False

        except Exception as e:
            logger.critical(f"🚨 ROLLBACK EXCEPTION {symbol}: {e}")
            logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED for {symbol}!")
            return False

    async def _rollback_lighter(self, execution: TradeExecution) -> bool:
        """
        Rollback Lighter position with actual position verification.
        
        This is called when X10 hedge fails after Lighter order filled.
        We need to close the Lighter position to prevent naked exposure.
        """
        symbol = execution.symbol
        original_side = execution.side_lighter
        
        logger.warning(f"🔄 ROLLBACK: Closing Lighter {symbol} position...")
        
        try:
            positions = await self.lighter.fetch_open_positions()
            pos = next(
                (p for p in (positions or [])
                 if p.get('symbol') == symbol and abs(safe_float(p.get('size', 0))) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✅ ROLLBACK {symbol}: No Lighter position found (already closed?)")
                return True

            actual_size = safe_float(pos.get('size', 0))
            position_side = "BUY" if actual_size > 0 else "SELL"
            close_side = "SELL" if actual_size > 0 else "BUY"
            close_size_coins = abs(actual_size)

            # CRITICAL FIX: Sichere Typ-Konvertierung
            raw_price = self.lighter.fetch_mark_price(symbol)
            mark_price = safe_float(raw_price)
            
            if mark_price <= 0:
                logger.critical(f"🚨 ROLLBACK FAILED {symbol}: No valid price available!")
                return False

            notional_usd = close_size_coins * mark_price

            logger.warning(
                f"🔄 ROLLBACK {symbol}: Closing Lighter {position_side} position "
                f"({close_size_coins:.6f} coins @ ${mark_price:.2f} = ${notional_usd:.2f}) "
                f"with {close_side} market order"
            )

            success, order_id = await self.lighter.close_live_position(
                symbol, position_side, notional_usd
            )

            if success:
                logger.info(f"✅ ROLLBACK SUCCESS: Lighter position {symbol} closed (order: {order_id})")
                return True
            else:
                logger.critical(f"🚨 ROLLBACK FAILED: Lighter {symbol} close_live_position returned False!")
                logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED for {symbol}!")
                return False

        except Exception as e:
            logger.critical(f"🚨 ROLLBACK EXCEPTION {symbol}: {e}")
            logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED for {symbol}!")
            return False

    def get_execution_stats(self) -> Dict[str, Any]:
        """Return current execution statistics for monitoring"""
        active_states = {}
        for ex in self.active_executions.values():
            state_name = ex.state.value
            active_states[state_name] = active_states.get(state_name, 0) + 1
        
        return {
            "active_executions": len(self.active_executions),
            "pending_rollbacks": self._rollback_queue.qsize(),
            "active_states": active_states,
            **self._stats
        }

    def get_execution(self, symbol: str) -> Optional[TradeExecution]:
        """Get execution state for a symbol"""
        return self.active_executions.get(symbol)

    async def _validate_orderbook_for_maker(
        self, 
        symbol: str, 
        side: str, 
        trade_size_usd: float
    ) -> OrderbookValidationResult:
        """
        Validate orderbook before placing a Maker order.
        
        Enhanced with:
        - Post-reconnect cooldown checking
        - Crossed book detection with REST fallback
        - Staleness validation
        - Depth and spread checks
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            side: Order side - "BUY" or "SELL"
            trade_size_usd: Intended trade size in USD
            
        Returns:
            OrderbookValidationResult with validation status
        """
        try:
            from decimal import Decimal
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 1: Check ONLY post-reconnect cooldown
            # (We check data validity AFTER trying to fetch it)
            # ═══════════════════════════════════════════════════════════════
            provider = get_orderbook_provider()
            
            if provider:
                # Check if in post-reconnect cooldown
                if provider.is_in_cooldown():
                    remaining = provider.get_cooldown_remaining()
                    # ═══════════════════════════════════════════════════════════════
                    # IMPROVEMENT: During cooldown, try to fetch fresh REST snapshot
                    # instead of immediately failing. This allows trading to resume
                    # faster after WebSocket reconnects.
                    # ═══════════════════════════════════════════════════════════════
                    logger.info(f"⏱️ {symbol}: Post-reconnect cooldown ({remaining:.1f}s) - fetching fresh REST snapshot...")
                    
                    try:
                        fresh_snapshot = await provider.fetch_orderbook_rest_fallback(
                            symbol, "lighter", retry_on_crossed=True
                        )
                        if fresh_snapshot and fresh_snapshot.best_ask and fresh_snapshot.best_bid:
                            if fresh_snapshot.best_ask > fresh_snapshot.best_bid:
                                logger.info(f"✅ {symbol}: Fresh REST snapshot OK during cooldown - proceeding with validation")
                                # Don't return early - continue to full validation with fresh data
                            else:
                                logger.warning(f"⚠️ {symbol}: REST snapshot during cooldown still crossed")
                                return OrderbookValidationResult(
                                    is_valid=False,
                                    quality=OrderbookQuality.CROSSED,
                                    reason=f"Post-reconnect cooldown + crossed book",
                                    bid_depth_usd=Decimal("0"),
                                    ask_depth_usd=Decimal("0"),
                                    spread_percent=None,
                                    best_bid=fresh_snapshot.best_bid,
                                    best_ask=fresh_snapshot.best_ask,
                                    bid_levels=len(fresh_snapshot.bids),
                                    ask_levels=len(fresh_snapshot.asks),
                                    staleness_seconds=0.0,
                                    recommended_action="wait"
                                )
                        else:
                            # Couldn't fetch fresh snapshot - fail with cooldown message
                            reason = f"Post-reconnect cooldown ({remaining:.1f}s remaining) - REST fetch failed"
                            logger.warning(f"⏱️ {symbol}: {reason}")
                            return OrderbookValidationResult(
                                is_valid=False,
                                quality=OrderbookQuality.EMPTY,
                                reason=reason,
                                bid_depth_usd=Decimal("0"),
                                ask_depth_usd=Decimal("0"),
                                spread_percent=None,
                                best_bid=None,
                                best_ask=None,
                                bid_levels=0,
                                ask_levels=0,
                                staleness_seconds=0.0,
                                recommended_action="wait"
                            )
                    except Exception as e:
                        reason = f"Post-reconnect cooldown ({remaining:.1f}s remaining) - REST error: {e}"
                        logger.warning(f"⏱️ {symbol}: {reason}")
                        return OrderbookValidationResult(
                            is_valid=False,
                            quality=OrderbookQuality.EMPTY,
                            reason=reason,
                            bid_depth_usd=Decimal("0"),
                            ask_depth_usd=Decimal("0"),
                            spread_percent=None,
                            best_bid=None,
                            best_ask=None,
                            bid_levels=0,
                            ask_levels=0,
                            staleness_seconds=0.0,
                            recommended_action="wait"
                        )
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 2: Get orderbook data (from provider or adapter)
            # ═══════════════════════════════════════════════════════════════
            orderbook = None
            orderbook_timestamp = None
            
            # Try OrderbookProvider first
            if provider:
                snapshot = await provider.get_lighter_orderbook(symbol)
                if snapshot:
                    # Convert snapshot to bids/asks tuples
                    bids = [(float(b[0]), float(b[1])) for b in snapshot.bids]
                    asks = [(float(a[0]), float(a[1])) for a in snapshot.asks]
                    orderbook_timestamp = snapshot.timestamp
                    orderbook = {'bids': bids, 'asks': asks}
            
            # Fallback to adapter cache
            if not orderbook and hasattr(self.lighter, '_orderbook_cache'):
                orderbook = self.lighter._orderbook_cache.get(symbol)
                orderbook_timestamp = self.lighter._orderbook_cache_time.get(symbol)
            
            # Last resort: fetch fresh
            if not orderbook:
                try:
                    orderbook = await self.lighter.fetch_orderbook(symbol, limit=20)
                    orderbook_timestamp = time.time()
                except Exception as e:
                    logger.warning(f"⚠️ {symbol}: Failed to fetch orderbook: {e}")
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 3: Parse and validate orderbook data
            # ═══════════════════════════════════════════════════════════════
            if orderbook:
                bids_raw = orderbook.get('bids', [])
                asks_raw = orderbook.get('asks', [])
                
                # Convert to list of tuples [(price, size), ...]
                bids = []
                for b in bids_raw:
                    if isinstance(b, (list, tuple)) and len(b) >= 2:
                        bids.append((safe_float(b[0], 0), safe_float(b[1], 0)))
                    elif isinstance(b, dict):
                        price = safe_float(b.get('price', b.get('p', 0)), 0)
                        size = safe_float(b.get('size', b.get('s', b.get('quantity', 0))), 0)
                        bids.append((price, size))
                
                asks = []
                for a in asks_raw:
                    if isinstance(a, (list, tuple)) and len(a) >= 2:
                        asks.append((safe_float(a[0], 0), safe_float(a[1], 0)))
                    elif isinstance(a, dict):
                        price = safe_float(a.get('price', a.get('p', 0)), 0)
                        size = safe_float(a.get('size', a.get('s', a.get('quantity', 0))), 0)
                        asks.append((price, size))
                
                # ═══════════════════════════════════════════════════════════════
                # STEP 3.5: Pre-check for crossed book before validator
                # ═══════════════════════════════════════════════════════════════
                if bids and asks:
                    best_bid = bids[0][0]
                    best_ask = asks[0][0]
                    if best_ask <= best_bid:
                        logger.warning(
                            f"⚠️ CROSSED BOOK detected in validation for {symbol}: "
                            f"ask={best_ask} <= bid={best_bid} - attempting REST fallback..."
                        )
                        # Try REST fallback with retry logic
                        if provider:
                            fresh_ob = await provider.fetch_orderbook_rest_fallback(symbol, "lighter", retry_on_crossed=True)
                            if fresh_ob and fresh_ob.best_ask and fresh_ob.best_bid:
                                if fresh_ob.best_ask > fresh_ob.best_bid:
                                    # Fresh data is not crossed, use it
                                    bids = [(float(b[0]), float(b[1])) for b in fresh_ob.bids]
                                    asks = [(float(a[0]), float(a[1])) for a in fresh_ob.asks]
                                    orderbook_timestamp = fresh_ob.timestamp
                                    logger.info(f"✅ {symbol}: REST fallback provided valid orderbook")
                                else:
                                    # REST also returned crossed - reject
                                    return OrderbookValidationResult(
                                        is_valid=False,
                                        quality=OrderbookQuality.CROSSED,
                                        reason=f"Crossed book persists after REST fallback: ask={best_ask} <= bid={best_bid}",
                                        bid_depth_usd=Decimal("0"),
                                        ask_depth_usd=Decimal("0"),
                                        spread_percent=None,
                                        best_bid=Decimal(str(best_bid)),
                                        best_ask=Decimal(str(best_ask)),
                                        bid_levels=len(bids),
                                        ask_levels=len(asks),
                                        staleness_seconds=0.0,
                                        recommended_action="wait"
                                    )
            else:
                bids = []
                asks = []
            
            # Log orderbook state before validation
            logger.debug(
                f"📚 {symbol} Orderbook state: {len(bids)} bids, {len(asks)} asks, "
                f"timestamp={orderbook_timestamp}"
            )
            
            # ═══════════════════════════════════════════════════════════════
            # STEP 4: Run full validation with Lighter profile
            # ═══════════════════════════════════════════════════════════════
            validator = get_orderbook_validator(profile=ExchangeProfile.LIGHTER)
            
            result = validator.validate_for_maker_order(
                symbol=symbol,
                side=side,
                trade_size_usd=Decimal(str(trade_size_usd)),
                bids=bids,
                asks=asks,
                orderbook_timestamp=orderbook_timestamp,
            )
            
            return result
            
        except Exception as e:
            logger.error(f"❌ {symbol}: Orderbook validation error: {e}")
            # Return a failed validation result
            from decimal import Decimal
            return OrderbookValidationResult(
                is_valid=False,
                quality=OrderbookQuality.EMPTY,
                reason=f"Validation error: {str(e)}",
                bid_depth_usd=Decimal("0"),
                ask_depth_usd=Decimal("0"),
                spread_percent=None,
                best_bid=None,
                best_ask=None,
                bid_levels=0,
                ask_levels=0,
                staleness_seconds=0.0,
                recommended_action="skip"
            )

    async def _run_compliance_check(self, symbol: str, side_x10: str, side_lighter: str) -> bool:
        """
        Checks for self-match / wash trading risks.
        Returns TRUE if safe to trade, FALSE if risk detected.
        
        OPTIMIZED: Uses cache to avoid repeated API calls during order bursts.
        """
        if not getattr(config, 'COMPLIANCE_CHECK_ENABLED', False):
            return True
        
        # ═══════════════════════════════════════════════════════════════
        # CHECK CACHE: Skip API calls if we recently checked this symbol
        # ═══════════════════════════════════════════════════════════════
        now = time.time()
        if symbol in self._compliance_cache:
            cache_time, cached_result = self._compliance_cache[symbol]
            if now - cache_time < self._compliance_cache_ttl:
                logger.debug(f"⚡ Compliance check cache hit for {symbol} (age: {now - cache_time:.1f}s)")
                return cached_result
        
        try:
            # 1. Fetch Open Orders (Parallel)
            # Use return_exceptions=True so one failure doesn't crash the other
            results = await asyncio.gather(
                self.x10.get_open_orders(symbol),
                self.lighter.get_open_orders(symbol),
                return_exceptions=True
            )
            
            orders_x10 = results[0]
            orders_lit = results[1]
            
            # Handle potential exceptions during fetch
            if isinstance(orders_x10, Exception):
                logger.warning(f"Compliance Check X10 Error: {orders_x10}")
                orders_x10 = []
            if isinstance(orders_lit, Exception):
                logger.warning(f"Compliance Check Lighter Error: {orders_lit}")
                orders_lit = []
            
            risk_detected = False
            
            # 2. Check X10 Conflicts
            # If I want to BUY, I must not have any SELL orders open
            for o in orders_x10:
                o_side = str(o.get('side', '')).upper()
                if side_x10 == "BUY" and o_side == "SELL":
                    logger.warning(f"⛔ COMPLIANCE ALERT: Self-Match risk on X10 {symbol}! (Buying into own Sell Order {o.get('id')})")
                    risk_detected = True
                if side_x10 == "SELL" and o_side == "BUY":
                    logger.warning(f"⛔ COMPLIANCE ALERT: Self-Match risk on X10 {symbol}! (Selling into own Buy Order {o.get('id')})")
                    risk_detected = True
            
            # 3. Check Lighter Conflicts
            for o in orders_lit:
                o_side = str(o.get('side', '')).upper()
                if side_lighter == "BUY" and o_side == "SELL":
                    logger.warning(f"⛔ COMPLIANCE ALERT: Self-Match risk on Lighter {symbol}! (Buying into own Sell Order {o.get('id')})")
                    risk_detected = True
                if side_lighter == "SELL" and o_side == "BUY":
                    logger.warning(f"⛔ COMPLIANCE ALERT: Self-Match risk on Lighter {symbol}! (Selling into own Buy Order {o.get('id')})")
                    risk_detected = True
            
            if risk_detected:
                if getattr(config, 'COMPLIANCE_BLOCK_SELF_MATCH', True):
                    # Cache the failed result
                    self._compliance_cache[symbol] = (now, False)
                    return False
            
            # Cache the successful result
            self._compliance_cache[symbol] = (now, True)
            return True
            
        except Exception as e:
            logger.error(f"Compliance Check Failed: {e}")
            # Fail safe: Allow trading if check errors locally
            # Don't cache errors - let next check retry
            return True

    def is_busy(self) -> bool:
        """Check if any executions or rollbacks are in progress"""
        return len(self.active_executions) > 0 or not self._rollback_queue.empty()