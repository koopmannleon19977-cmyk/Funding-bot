# src/application/execution/parallel_execution.py - PUNKT 1: PARALLEL EXECUTION MIT OPTIMISTIC ROLLBACK
# Note: This file has been moved to application/execution/ for better organization
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
from src.volatility_monitor import get_volatility_monitor  # FIX (2025-12-19): Import für dynamic spread protection
from src.validation.orderbook_validator import (
    OrderbookValidator,
    OrderbookValidationResult,
    OrderbookQuality,
    ExchangeProfile,
    get_orderbook_validator,
)
from src.data.orderbook_provider import get_orderbook_provider, init_orderbook_provider
import math

def _scalar_float(value: Any) -> Optional[float]:
    """Best-effort float conversion for real scalar values (avoid MagicMock -> 1.0 in tests)."""
    try:
        if value is None:
            return None
        if isinstance(value, (int, float, Decimal, str)):
            return float(value)
        return None
    except Exception:
        return None

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


@dataclass(slots=True)
class TradeExecution:
    """Tracks state of a single parallel trade execution
    
    OPTIMIZED: Uses slots=True for ~30% memory reduction per instance.
    This is the modern Python 3.10+ way to use __slots__ with dataclasses.
    """
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
    entry_price_x10: Optional[float] = None
    entry_price_lighter: Optional[float] = None

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_time) * 1000


class ParallelExecutionManager:
    """
    Manages parallel trade execution with optimistic rollback.

    ═══════════════════════════════════════════════════════════════════════════
    EXECUTION STRATEGY (REVERSED - NEW 2025-12-19):
    ═══════════════════════════════════════════════════════════════════════════
    1. Send X10 LIMIT/POST_ONLY (maker) first - wait for fill (max 10s)
    2. If X10 fills -> immediately hedge with Lighter MARKET (taker/IOC)
    3. If X10 cancelled/timeout -> ABORT (no zombie risk!)
    4. If Lighter hedge fails -> rollback X10 position
    5. State machine tracks execution for recovery & monitoring

    BENEFITS vs old strategy:
    - X10 Maker Fee = 0% (vs 0.0225% Taker)
    - No zombie risk (X10 is gate - if no fill, no trade)
    - Lighter has better liquidity for Market orders
    - Follows @Quantzilla34 & @DegeniusQ recommendations
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
        
        # FIXED: Also register Lighter position callback for Ghost-Fill detection
        if hasattr(self.lighter, 'register_position_callback'):
            self.lighter.register_position_callback(self._on_lighter_position_update)
            logger.debug("✅ Registered Lighter position update callback for fill detection")

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
    
    async def _on_lighter_position_update(self, data: dict):
        """
        Handle Lighter position update (from REST polling or WebSocket if available).
        FIXED: Enables faster Ghost-Fill detection by triggering event when position appears.
        """
        try:
            symbol = data.get("symbol") or data.get("market") or ""
            if not symbol:
                return
            
            # Check if we're waiting for a fill for this symbol
            if symbol not in self._position_fill_events:
                return
            
            size = safe_float(data.get("size") or data.get("quantity") or data.get("qty") or 0)
            target_size = self._position_fill_sizes.get(symbol, 0)
            is_ghost = data.get("is_ghost", False)
            
            # Position exists with at least 50% of target OR is Ghost Guardian position
            if target_size > 0 and (is_ghost or abs(size) >= target_size * 0.50):
                event = self._position_fill_events.get(symbol)
                if event and not event.is_set():
                    if is_ghost:
                        logger.debug(f"🔔 [LIGHTER FILL EVENT] {symbol}: Ghost Guardian detected! Marking as filled.")
                    else:
                        logger.debug(f"🔔 [LIGHTER FILL EVENT] {symbol}: Position detected! size={size}, target={target_size}")
                    event.set()
        
        except Exception as e:
            logger.debug(f"Error processing Lighter position update for fill detection: {e}")
    
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
        logger.info(f"   X10:     {side_x10} ${float(size_x10):.2f} @ ${x10_price:.6f}")
        logger.info(f"   Lighter: {side_lighter} ${float(size_lighter):.2f} @ ${lighter_price:.6f}")
        logger.info(f"   Strategy: REVERSED MAKER (X10 LIMIT first, then Lighter MARKET hedge)")
        logger.info(f"═══════════════════════════════════════════════════════════")
        
        # Ensure symbol-level lock exists
        if symbol not in self.execution_locks:
            self.execution_locks[symbol] = asyncio.Lock()

        async with self.execution_locks[symbol]:
            # ═══════════════════════════════════════════════════════════════
            # PHASE 0.5: CANCEL EXISTING ORDERS (Prevent Duplicate Orders)
            # ═══════════════════════════════════════════════════════════════
            # FIX (2025-12-19): REVERSED STRATEGY - Cancel X10 orders before placing
            # (Previously cancelled Lighter, now we cancel X10 since X10 goes first)
            try:
                # Step 1: Cancel X10 orders (X10 goes first now)
                existing_x10_orders = []
                if hasattr(self.x10, 'get_open_orders'):
                    try:
                        existing_x10_orders = await self.x10.get_open_orders(symbol)
                    except Exception as e:
                        logger.debug(f"🧹 [PRE-TRADE] {symbol}: Error checking X10 open orders: {e}")

                if hasattr(self.x10, 'cancel_all_orders'):
                    if existing_x10_orders and len(existing_x10_orders) > 0:
                        logger.warning(f"🧹 [PRE-TRADE] {symbol}: Found {len(existing_x10_orders)} existing X10 order(s) - cancelling before new trade")
                    else:
                        logger.debug(f"🧹 [PRE-TRADE] {symbol}: No X10 orders found, but attempting cancel anyway (defensive)")

                    cancelled_x10 = await self.x10.cancel_all_orders(symbol)
                    if cancelled_x10:
                        if existing_x10_orders:
                            logger.info(f"🧹 [PRE-TRADE] {symbol}: Successfully cancelled {len(existing_x10_orders)} X10 order(s)")
                        else:
                            logger.debug(f"🧹 [PRE-TRADE] {symbol}: X10 cancel_all_orders() executed")
                    else:
                        logger.warning(f"🧹 [PRE-TRADE] {symbol}: X10 cancel_all_orders returned False")
                else:
                    logger.warning(f"🧹 [PRE-TRADE] {symbol}: X10 cancel_all_orders not available!")

                # Step 2: Also cancel Lighter orders (defensive, in case of previous failures)
                existing_lighter_orders = []
                if hasattr(self.lighter, 'get_open_orders'):
                    try:
                        existing_lighter_orders = await self.lighter.get_open_orders(symbol)
                    except Exception as e:
                        logger.debug(f"🧹 [PRE-TRADE] {symbol}: Error checking Lighter open orders: {e}")

                if hasattr(self.lighter, 'cancel_all_orders'):
                    if existing_lighter_orders and len(existing_lighter_orders) > 0:
                        logger.warning(f"🧹 [PRE-TRADE] {symbol}: Found {len(existing_lighter_orders)} existing Lighter order(s) - cancelling")
                        await self.lighter.cancel_all_orders(symbol)
            except Exception as e:
                # Non-fatal: Log but continue
                logger.warning(f"🧹 [PRE-TRADE] {symbol}: Error in order cleanup: {e} - proceeding anyway")

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
                        x10_m = None
                        x10_market_info = getattr(self.x10, "market_info", None)
                        if isinstance(x10_market_info, dict):
                            x10_m = x10_market_info.get(symbol)
                        elif hasattr(self.x10, "get_market_info"):
                            try:
                                x10_m = self.x10.get_market_info(symbol)
                            except Exception:
                                x10_m = None

                        if x10_m:
                            if hasattr(x10_m, "trading_config"):  # Object from SDK
                                candidate = getattr(getattr(x10_m, "trading_config", None), "min_order_size_change", None)
                                candidate_f = _scalar_float(candidate)
                                if candidate_f:
                                    x10_step = candidate_f
                            elif isinstance(x10_m, dict):
                                candidate = x10_m.get("lot_size", x10_m.get("min_order_qty"))
                                candidate_f = _scalar_float(candidate)
                                if candidate_f:
                                    x10_step = candidate_f
                             
                    # Lighter
                    lighter_step = 0.01
                    lig_m = self.lighter.get_market_info(symbol) if hasattr(self.lighter, 'get_market_info') else None
                    if lig_m:
                        # Prioritize 'lot_size', then 'min_quantity', then 'size_increment'
                        candidate = lig_m.get('lot_size', lig_m.get('size_increment', lig_m.get('min_quantity', 0.01)))
                        lighter_step = _scalar_float(candidate) or lighter_step
                    
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
                quantity_coins=float(safe_coins),
                entry_price_x10=x10_price,
                entry_price_lighter=lighter_price
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
            
            # FIX (2025-12-19): Reduced timeouts for faster Ghost Fill detection
            # OLD: 60s base, 30-90s range
            # NEW: 20s base, 15-25s range (2.5-3x faster detection!)
            base_timeout = float(getattr(config, "LIGHTER_ORDER_TIMEOUT_SECONDS", 20.0))  # REDUCED from 60s
            min_timeout = float(getattr(config, "MAKER_ORDER_MIN_TIMEOUT_SECONDS", 15.0))  # REDUCED from 30s
            max_timeout = float(getattr(config, "MAKER_ORDER_MAX_TIMEOUT_SECONDS", 25.0))  # REDUCED from 90s
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
                    
                    # FIX 9: Apply volatility adjustment if available
                    try:
                        if hasattr(self.lighter, 'calculate_volatility'):
                            vol_data = await self.lighter.calculate_volatility(symbol, "1h", 14)
                            if vol_data:
                                volatility_level = vol_data.get("volatility_level", "MEDIUM")
                                atr_percent = vol_data.get("atr_percent", 0)
                                
                                if volatility_level == "LOW":
                                    # Low volatility - can reduce timeout slightly
                                    timeout *= 0.9
                                elif volatility_level == "HIGH":
                                    # High volatility - increase timeout
                                    timeout *= 1.2
                                
                                logger.debug(
                                    f"📊 {symbol}: Volatility adjustment applied: "
                                    f"ATR={atr_percent:.2f}% ({volatility_level})"
                                )
                    except Exception as vol_err:
                        logger.debug(f"⚠️ {symbol}: Volatility check skipped: {vol_err}")
                    
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
        
        # ═══════════════════════════════════════════════════════════════
        # FIX: Check MAX_OPEN_TRADES before placing retry order
        # This prevents retries from exceeding the position limit
        # ═══════════════════════════════════════════════════════════════
        max_positions = int(getattr(config, 'MAX_OPEN_POSITIONS', getattr(config, 'MAX_OPEN_TRADES', 40)))
        if max_positions > 0:
            try:
                # Get open positions from both exchanges
                x10_positions = await self.x10.fetch_open_positions()
                lighter_positions = await self.lighter.fetch_open_positions()
                
                open_symbols_real = {
                    p.get('symbol') for p in (x10_positions or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                } | {
                    p.get('symbol') for p in (lighter_positions or [])
                    if abs(safe_float(p.get('size', 0))) > 1e-8
                }
                
                # Also include DB open trades and in-flight executions/tasks
                from src.core.state import get_open_trades
                existing = await get_open_trades()
                open_symbols_db = {t.get('symbol') for t in (existing or []) if t.get('symbol')}
                
                # Include active executions (pending orders) - but only those that are NOT failed
                active_symbols = set()
                try:
                    if hasattr(self, 'active_executions') and isinstance(self.active_executions, dict):
                        # Only count executions that are not in FAILED state
                        for sym, exec_obj in self.active_executions.items():
                            if hasattr(exec_obj, 'state') and exec_obj.state != ExecutionState.FAILED:
                                active_symbols.add(sym)
                            elif not hasattr(exec_obj, 'state'):
                                # If no state attribute, assume it's active (backward compatibility)
                                active_symbols.add(sym)
                except Exception:
                    pass
                
                # Count total active symbols (positions + pending orders)
                total_active_symbols = {s for s in (open_symbols_real | open_symbols_db | active_symbols) if s}
                
                # Check if position already exists for this symbol (order might have filled)
                if symbol in open_symbols_real:
                    logger.info(
                        f"✅ [RETRY] {symbol}: Position already exists - order likely filled, skipping retry"
                    )
                    return False, None
                
                # If we're already at max, don't place retry (unless this symbol is already counted)
                if len(total_active_symbols) >= max_positions and symbol not in total_active_symbols:
                    logger.info(
                        f"⛔ [RETRY] {symbol}: Max open positions reached "
                        f"({len(total_active_symbols)}/{max_positions}) - skipping retry"
                    )
                    return False, None
            except Exception as e:
                logger.warning(f"⚠️ [RETRY] {symbol}: Error checking MAX_OPEN_TRADES: {e}, proceeding with retry")
        
        # ═══════════════════════════════════════════════════════════════
        # FIX: Check if position already exists BEFORE trying to cancel
        # If position exists, order was already filled - skip retry entirely
        # ═══════════════════════════════════════════════════════════════
        try:
            positions = await self.lighter.fetch_open_positions()
            pos = next((p for p in (positions or []) if p.get("symbol") == symbol), None)
            if pos:
                pos_size = safe_float(pos.get("size", 0))
                is_ghost = pos.get("is_ghost", False)
                if is_ghost or abs(pos_size) >= execution.quantity_coins * 0.50:
                    logger.info(f"✅ [RETRY] {symbol}: Position already exists - order likely filled, skipping retry")
                    return True, None  # Order was filled, no retry needed
        except Exception as e:
            logger.debug(f"[RETRY] {symbol}: Position pre-check error: {e}")

        # Cancel original order first.
        # IMPORTANT: If we cannot confirm cancellation (e.g. hash can't be resolved),
        # do NOT place a new retry order. Otherwise we can stack multiple live maker
        # orders and effectively "nachkaufen" when both fill.
        cancelled_ok = False
        try:
            logger.info(f"🔄 [RETRY] {symbol}: Cancelling original order {original_order_id[:40]}...")
            if hasattr(self.lighter, 'cancel_limit_order'):
                cancelled_ok = bool(await self.lighter.cancel_limit_order(original_order_id, symbol))
            else:
                cancelled_ok = bool(await self.lighter.cancel_all_orders(symbol))
        except Exception as e:
            logger.warning(f"⚠️ [RETRY] {symbol}: Error cancelling original order: {e}")
            cancelled_ok = False

        if not cancelled_ok:
            logger.warning(
                f"🛑 [RETRY] {symbol}: Original order cancel NOT confirmed; skipping retry to prevent duplicate fills"
            )
            return False, None

        # Extra safety: if we still see any open Lighter orders for this symbol, skip retry.
        # (Prevents stacking orders in eventual-consistency windows.)
        try:
            if hasattr(self.lighter, 'get_open_orders'):
                open_orders = await self.lighter.get_open_orders(symbol)
                if open_orders:
                    logger.warning(
                        f"🛑 [RETRY] {symbol}: {len(open_orders)} open Lighter orders still present; skipping retry"
                    )
                    return False, None
        except Exception as e:
            logger.debug(f"[RETRY] {symbol}: open-order safety check failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════
        # HARD-GUARD FIX (2025-12-18): After cancel confirmed, re-check position!
        # If we already have a position (Size>0), the order FILLED during cancel.
        # Do NOT retry - just return success. Otherwise we get hedge-mismatch
        # (e.g., Lighter 1176 coins vs X10 392 coins = massive exposure)
        # ═══════════════════════════════════════════════════════════════
        try:
            positions = await self.lighter.fetch_open_positions()
            p = next((x for x in (positions or []) if x.get("symbol") == symbol), None)
            current_size = safe_float(p.get("size", 0)) if p else 0.0
            
            if abs(current_size) > 0:
                # Position exists = order was FILLED during cancel!
                logger.warning(
                    f"🛑 [RETRY] {symbol}: Position size={current_size:.6f} detected after cancel! "
                    f"Order was filled - SKIP RETRY to prevent hedge-mismatch"
                )
                # Return True = "success" to trigger hedge with actual size
                return True, None
        except Exception as e:
            logger.debug(f"[RETRY] {symbol}: Post-cancel position check error: {e}")
        
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
            # ═══════════════════════════════════════════════════════════════
            # FIX 1 (2025-12-13): SHUTDOWN CHECK BEFORE ANY ORDER PLACEMENT
            # This MUST happen BEFORE placing any new orders to prevent:
            # 1. Orders placed AFTER ImmediateCancelAll (won't be cancelled!)
            # 2. Duplicate orders accumulating in the orderbook
            # ═══════════════════════════════════════════════════════════════
            if getattr(config, "IS_SHUTTING_DOWN", False):
                logger.warning(f"⚡ [RETRY] {symbol}: SHUTDOWN detected - aborting retry loop before placing new order!")
                return False, None
            
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
                            fill_time = time.time() - wait_start
                            logger.info(
                                f"✅ [RETRY] {symbol}: Fill detected after {check_count} checks "
                                f"(attempt {retry_attempt}/{max_retries}, {fill_time:.2f}s)!"
                            )
                            break
                        
                        # FIX 7: Reduced from 0.5s to 0.3s for faster fill detection
                        await asyncio.sleep(0.3)
                        
                    except Exception as e:
                        logger.debug(f"[RETRY] {symbol}: Check #{check_count} error: {e}")
                        # FIX 7: Reduced from 1.0s to 0.5s for faster recovery
                        await asyncio.sleep(0.5)
                
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

    async def _handle_maker_timeout(self, execution, lighter_order_id) -> Tuple[bool, Optional[float], bool]:
        """
        Handle maker order timeout and check for fills.
        
        Returns:
            Tuple[bool, Optional[float], bool]: (filled, actual_filled_size_coins, wait_more)
            - filled: True if order was filled (fully or partially) and ready for hedge
            - actual_filled_size_coins: Actual filled size in coins if known, None otherwise
            - wait_more: True if partial fill exists but is below X10 min_trade_size (keep waiting)
        """
        symbol = execution.symbol
        logger.warning(f"⏰ [MAKER STRATEGY] {symbol}: Wait timeout! Checking for fills before cancel...")

        # ═══════════════════════════════════════════════════════════════
        # FIX (2025-12-13): Get X10 min_trade_size for this symbol
        # ═══════════════════════════════════════════════════════════════
        x10_min_trade_size = 0.001  # Default fallback
        try:
            x10_m = self.x10.market_info.get(symbol) if hasattr(self, 'x10') else None
            if x10_m:
                if hasattr(x10_m, 'trading_config'):  # Object from SDK
                    raw = getattr(x10_m.trading_config, "min_order_size_change", None)
                    parsed = _scalar_float(raw)
                    if parsed is not None:
                        x10_min_trade_size = parsed
                elif isinstance(x10_m, dict):
                    raw = x10_m.get('lot_size', x10_m.get('min_order_qty', 0.001))
                    parsed = _scalar_float(raw)
                    if parsed is not None:
                        x10_min_trade_size = parsed
            logger.debug(f"📏 [MIN_SIZE] {symbol}: X10 min_trade_size = {x10_min_trade_size}")
        except Exception as e:
            logger.debug(f"[MIN_SIZE] {symbol}: Error getting X10 min_trade_size: {e}")

        # ═══════════════════════════════════════════════════════════════
        # FIX: Check if position already exists BEFORE trying to cancel
        # If position exists, order was already filled - skip cancel attempt
        # NEW: If partial fill < X10 min_trade_size, DON'T CANCEL - keep waiting!
        # ═══════════════════════════════════════════════════════════════
        try:
            positions = await self.lighter.fetch_open_positions()
            pos = next((p for p in (positions or []) if p.get("symbol") == symbol), None)
            if pos:
                pos_size = safe_float(pos.get("size", 0))
                abs_pos_size = abs(pos_size)
                is_ghost = pos.get("is_ghost", False)
                
                if is_ghost or abs_pos_size >= execution.quantity_coins * 0.50:
                    # FULL or LARGE PARTIAL FILL: sufficient for hedge.
                    # IMPORTANT: Do NOT skip cancel purely based on position presence.
                    # A position can pre-exist (or be stale/orphaned) and leaving the maker order open
                    # is dangerous (it can keep filling and change exposure). We therefore best-effort
                    # cancel the outstanding order(s) before proceeding to hedge the observed position.
                    logger.info(
                        f"✅ [MAKER STRATEGY] {symbol}: Position detected (size={pos_size:.6f}) - "
                        "cancelling outstanding maker order(s) before hedging"
                    )
                    try:
                        if hasattr(self.lighter, 'cancel_limit_order'):
                            await self.lighter.cancel_limit_order(lighter_order_id, symbol)
                        else:
                            await self.lighter.cancel_all_orders(symbol)
                    except Exception as cancel_e:
                        logger.warning(f"⚠️ [MAKER STRATEGY] {symbol}: Cancel attempt failed before hedge: {cancel_e}")

                    return True, abs_pos_size, False  # filled=True, wait_more=False
                    
                elif abs_pos_size > 1e-8:
                    # PARTIAL FILL: Position exists but is smaller than expected
                    # ═══════════════════════════════════════════════════════════════
                    # OPTION A FIX: Check if partial fill >= X10 min_trade_size
                    # If UNDER min_trade_size: DON'T CANCEL, keep waiting for more fills
                    # ═══════════════════════════════════════════════════════════════
                    if abs_pos_size < x10_min_trade_size:
                        logger.warning(
                            f"⏳ [MAKER STRATEGY] {symbol}: SMALL PARTIAL FILL detected "
                            f"({abs_pos_size:.6f} < X10 min {x10_min_trade_size:.6f}) - "
                            f"NOT CANCELLING ORDER, will keep waiting for more fills!"
                        )
                        # Return special signal: wait_more=True
                        return False, None, True  # filled=False, wait_more=True
                    else:
                        # Partial fill IS large enough for X10 hedge
                        logger.warning(
                            f"⚠️ [MAKER STRATEGY] {symbol}: PARTIAL FILL detected "
                            f"({abs_pos_size:.6f} >= X10 min {x10_min_trade_size:.6f}) - "
                            f"will cancel remaining order and hedge this amount"
                        )
                        # Don't return yet - continue to cancel remaining order parts
                        
        except Exception as e:
            logger.debug(f"[MAKER STRATEGY] {symbol}: Position pre-check error: {e}")
            # Continue with cancel attempt if check fails

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
        actual_filled_size: Optional[float] = None  # Track actual filled size
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
                    actual_filled_size = abs(filled_amount)  # Store actual filled size
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
                    actual_filled_size = abs(fill_sum)  # Store actual filled size from trade history
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
                    # 2. FIX: Aggressiver Ghost-Fill Check (2025-12-13 Audit Fix)
                    # PROBLEM: Alter Code startete bei 1.0s und wuchs auf 3.0s = 55s bis Attempt 22!
                    # LÖSUNG: Schnellere Checks mit kürzerem Backoff + tatsächliche Position Size tracken
                    logger.info(f"🔍 [MAKER STRATEGY] {symbol}: Checking for Ghost Fills (FAST CHECK)...")
                    
                    # AUDIT FIX: 20 Versuche mit schnellerem Intervall (~10s total statt ~60s)
                    # Alt: 30 Versuche, 1.0-3.0s delay = ~60s
                    # Neu: 20 Versuche, 0.3-1.0s delay = ~10s
                    max_checks = 20
                    for i in range(max_checks): 
                        # FAST SHUTDOWN: Abort immediately if shutdown detected mid-loop
                        if getattr(config, 'IS_SHUTTING_DOWN', False):
                            logger.warning(f"⚡ {symbol}: SHUTDOWN detected during ghost check - aborting!")
                            extended_checks_skipped = True
                            break
                            
                        # AUDIT FIX: Schnellerer Backoff - Start 0.3s, +0.05s pro Versuch, max 1.0s
                        # Attempt 0: 0.3s, Attempt 5: 0.55s, Attempt 14+: 1.0s
                        # Total Zeit für 20 checks: ~10-12s (statt ~55s vorher!)
                        wait_time = min(0.3 + (i * 0.05), 1.0)
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
                                actual_filled_size = abs(size)  # Store actual position size for accurate hedge
                                
                                # CRITICAL FIX (2025-12-13): If partial fill detected, attempt to cancel remaining order
                                # The original order might still be open in the orderbook (e.g., 0.2 filled, 51.8 still open)
                                try:
                                    if hasattr(self.lighter, 'cancel_all_orders'):
                                        logger.info(f"🧹 [GHOST FILL] {symbol}: Attempting to cancel remaining order parts after partial fill (size={size})")
                                        await self.lighter.cancel_all_orders(symbol)
                                except Exception as cancel_e:
                                    logger.debug(f"🧹 [GHOST FILL] {symbol}: Error cancelling remaining order: {cancel_e}")
                                
                                break
                            
                            # Logge alle 5 Versuche für besseres Monitoring
                            if i % 5 == 0:
                                logger.debug(f"🔍 {symbol} check {i+1}/{max_checks} clean...")
                                
                        except Exception as e:
                            logger.debug(f"Check error: {e}")
        


        if filled:
            logger.info(f"✅ [MAKER STRATEGY] {symbol}: Lighter Filled! Executing X10 Hedge...")
            # Return filled=True with actual size (or None if size unknown)
            return True, actual_filled_size, False  # filled=True, wait_more=False
        else:
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL FIX (2025-12-19): ORDERBOOK VERIFICATION
            # Problem: Bot only checks positions, not if order still in orderbook!
            # Orders can remain in orderbook even after "cancel NOT confirmed"
            # Solution: Check orderbook AND retry cancel if order still exists
            # ═══════════════════════════════════════════════════════════════
            logger.info(f"🔍 [ORDERBOOK CHECK] {symbol}: Verifying order cancellation in orderbook...")
            orders_still_open = False
            try:
                if hasattr(self.lighter, 'get_open_orders'):
                    open_orders = await self.lighter.get_open_orders(symbol)
                    logger.info(f"📋 [ORDERBOOK CHECK] {symbol}: Found {len(open_orders) if open_orders else 0} open orders")
                    if open_orders:
                        orders_still_open = True
                        logger.warning(
                            f"🚨 [MAKER STRATEGY] {symbol}: {len(open_orders)} orders STILL in orderbook after cancel! "
                            f"Attempting force-cancel..."
                        )
                        
                        # Force-cancel all orders for this symbol
                        try:
                            cancel_success = await self.lighter.cancel_all_orders(symbol)
                            if cancel_success:
                                logger.info(f"✅ [MAKER STRATEGY] {symbol}: Force-cancel successful")
                                # Wait and verify
                                await asyncio.sleep(0.5)
                                # Double-check
                                remaining_orders = await self.lighter.get_open_orders(symbol)
                                if remaining_orders:
                                    logger.error(
                                        f"❌ [MAKER STRATEGY] {symbol}: CRITICAL BUG - {len(remaining_orders)} orders STILL present after force-cancel!"
                                    )
                                else:
                                    orders_still_open = False
                            else:
                                logger.error(f"❌ [MAKER STRATEGY] {symbol}: Force-cancel FAILED")
                        except Exception as force_e:
                            logger.error(f"❌ [MAKER STRATEGY] {symbol}: Force-cancel exception: {force_e}")
            except Exception as e:
                logger.debug(f"[MAKER STRATEGY] {symbol}: Orderbook check error: {e}")
            
            # FIX: Log-Nachricht akkurat basierend auf tatsächlich durchgeführten Checks
            if orders_still_open:
                logger.error(
                    f"❌ [MAKER STRATEGY] {symbol}: CRITICAL - Orders remain in orderbook! "
                    f"Clean Exit FAILED after {extended_checks_done} position checks."
                )
            elif extended_checks_skipped:
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit - extended checks skipped during shutdown).")
            elif extended_checks_done > 0:
                # AUDIT FIX: Neue Formel für Backoff: 0.3s + (i * 0.05s), max 1.0s
                estimated_seconds = sum(min(0.3 + (i * 0.05), 1.0) for i in range(extended_checks_done))
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit verified after ~{estimated_seconds:.1f}s, {extended_checks_done} checks).")
            else:
                logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit - no extended verification needed).")
            return False, None, False  # Not filled, no size, wait_more=False

    def _get_x10_min_trade_size_coins(self, symbol: str) -> float:
        """Best-effort retrieval of X10 minimum order size for a market in COINS."""
        x10_min_trade_size = 0.001
        try:
            x10_m = self.x10.market_info.get(symbol) if hasattr(self, "x10") else None
            if x10_m:
                if hasattr(x10_m, "trading_config"):
                    raw = getattr(x10_m.trading_config, "min_order_size_change", None)
                    parsed = _scalar_float(raw)
                    if parsed is not None:
                        x10_min_trade_size = parsed
                elif isinstance(x10_m, dict):
                    raw = x10_m.get("lot_size", x10_m.get("min_order_qty", 0.001))
                    parsed = _scalar_float(raw)
                    if parsed is not None:
                        x10_min_trade_size = parsed
        except Exception:
            pass
        return float(x10_min_trade_size)

    def _estimate_unhedged_usd_from_partial_fill(self, execution: TradeExecution, partial_fill_coins: float) -> float:
        """Estimate unhedged USD exposure from partial fill fraction vs planned execution size."""
        try:
            planned_coins = float(execution.quantity_coins or 0.0)
            planned_usd = float(abs(execution.size_lighter or 0.0))
            if planned_usd <= 0 or planned_coins <= 0:
                return 0.0
            fraction = min(1.0, max(0.0, float(abs(partial_fill_coins)) / planned_coins))
            return planned_usd * fraction
        except Exception:
            return 0.0

    async def _abort_maker_microfill_and_cleanup(
        self,
        execution: TradeExecution,
        lighter_order_id: str,
        partial_fill_coins: float,
        x10_min_trade_coins: float,
        reason: str,
    ) -> None:
        """Cancel remaining maker order parts and immediately close any partial Lighter position."""
        symbol = execution.symbol

        logger.warning(
            f"🧹 [MICROFILL] {symbol}: Aborting maker entry ({reason}) | "
            f"partial={abs(partial_fill_coins):.6f} < x10_min={x10_min_trade_coins:.6f}"
        )

        # 1) Cancel remaining order parts (best-effort)
        try:
            if hasattr(self.lighter, "cancel_limit_order") and lighter_order_id:
                await self.lighter.cancel_limit_order(lighter_order_id, symbol)
            else:
                await self.lighter.cancel_all_orders(symbol)
        except Exception as cancel_err:
            logger.warning(f"⚠️ [MICROFILL] {symbol}: Cancel failed: {cancel_err}")

        # 2) Close any partial position on Lighter (best-effort)
        try:
            positions = await self.lighter.fetch_open_positions()
            lighter_pos = next((p for p in (positions or []) if p.get("symbol") == symbol), None)
            if not lighter_pos:
                return

            pos_size = float(lighter_pos.get("size", 0) or 0)
            if abs(pos_size) <= 0:
                return

            original_side = "BUY" if pos_size > 0 else "SELL"  # long=BUY, short=SELL
            est_unhedged_usd = self._estimate_unhedged_usd_from_partial_fill(execution, abs(pos_size))
            notional_usd = max(est_unhedged_usd, 0.01)  # allow dust closes

            logger.warning(
                f"🧹 [MICROFILL] {symbol}: Closing partial Lighter position "
                f"(pos_size={abs(pos_size):.6f} coins, notional≈${notional_usd:.2f})"
            )

            ok, _ = await self.lighter.close_live_position(symbol, original_side, notional_usd)
            if ok:
                logger.info(f"✅ [MICROFILL] {symbol}: Partial position closed")
            else:
                logger.warning(f"⚠️ [MICROFILL] {symbol}: Failed to close partial position")
        except Exception as close_err:
            logger.warning(f"⚠️ [MICROFILL] {symbol}: Close error: {close_err}")

    # ============================================================
    # SPREAD PROTECTION (PRE-HEDGE)
    # ============================================================
    @staticmethod
    def _mid_from_bid_ask(bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
        try:
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                return (bid + ask) / Decimal("2")
            if bid is not None and bid > 0:
                return bid
            if ask is not None and ask > 0:
                return ask
            return None
        except Exception:
            return None

    @staticmethod
    def _spread_pct(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
        try:
            if a is None or b is None:
                return None
            denom = max(a, b)
            if denom <= 0:
                return None
            return abs(a - b) / denom
        except Exception:
            return None

    def _mid_from_book_dict(self, book: Any) -> Optional[Decimal]:
        """Compute a mid price from an adapter-style orderbook dict (bids/asks)."""
        try:
            if not isinstance(book, dict):
                return None
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = None
            best_ask = None
            if bids and isinstance(bids[0], (list, tuple)) and len(bids[0]) >= 1:
                best_bid = safe_decimal(bids[0][0])
            if asks and isinstance(asks[0], (list, tuple)) and len(asks[0]) >= 1:
                best_ask = safe_decimal(asks[0][0])
            return self._mid_from_bid_ask(best_bid, best_ask)
        except Exception:
            return None

    async def _get_cross_exchange_mid_prices(self, symbol: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Best-effort mid prices for X10 and Lighter.
        Prefers OrderbookProvider snapshots; falls back to adapter caches and mark prices.
        """
        x10_mid: Optional[Decimal] = None
        lit_mid: Optional[Decimal] = None

        try:
            provider = get_orderbook_provider()
        except Exception:
            provider = None

        # 1) Provider snapshots (best)
        if provider:
            try:
                x10_task = asyncio.create_task(provider.get_x10_orderbook(symbol))
                lit_task = asyncio.create_task(provider.get_lighter_orderbook(symbol))
                x10_snap, lit_snap = await asyncio.gather(x10_task, lit_task, return_exceptions=True)

                if not isinstance(x10_snap, Exception) and x10_snap is not None:
                    x10_mid = self._mid_from_bid_ask(getattr(x10_snap, "best_bid", None), getattr(x10_snap, "best_ask", None))
                if not isinstance(lit_snap, Exception) and lit_snap is not None:
                    lit_mid = self._mid_from_bid_ask(getattr(lit_snap, "best_bid", None), getattr(lit_snap, "best_ask", None))
            except Exception:
                pass

        # 2) Adapter orderbook caches (fallback)
        if x10_mid is None:
            try:
                if hasattr(self.x10, "_orderbook_cache"):
                    x10_mid = self._mid_from_book_dict(self.x10._orderbook_cache.get(symbol))
            except Exception:
                x10_mid = None

        if lit_mid is None:
            try:
                if hasattr(self.lighter, "_orderbook_cache"):
                    lit_mid = self._mid_from_book_dict(self.lighter._orderbook_cache.get(symbol))
            except Exception:
                lit_mid = None

        # 3) Mark price caches (last resort)
        if x10_mid is None:
            try:
                x10_px = self.x10.fetch_mark_price(symbol) if hasattr(self.x10, "fetch_mark_price") else None
                x10_mid = safe_decimal(x10_px) if x10_px else None
            except Exception:
                x10_mid = None

        if lit_mid is None:
            try:
                lit_px = self.lighter.fetch_mark_price(symbol) if hasattr(self.lighter, "fetch_mark_price") else None
                lit_mid = safe_decimal(lit_px) if lit_px else None
            except Exception:
                lit_mid = None

        return x10_mid, lit_mid

    async def _validate_spread_before_hedge(
        self,
        symbol: str,
        expected_x10_price: Optional[Decimal],
        expected_lit_price: Optional[Decimal],
    ) -> tuple[bool, str]:
        """
        Spread-Protection: after Lighter fill, re-check cross-exchange spread before placing X10 hedge.
        If the spread has narrowed too much vs entry snapshot, abort hedge and rollback Lighter.
        
        FIX (2025-12-19): Dynamic narrow_factor based on volatility
        - High volatility: Use 0.50 factor (more relaxed, allow wider divergences)
        - Normal: Use 0.70 factor (standard protection)
        - Low volatility: Use 0.60 factor (moderate protection)
        """
        if not getattr(config, "SPREAD_PROTECTION_ENABLED", True):
            return True, "disabled"

        expected_spread_pct = self._spread_pct(expected_x10_price, expected_lit_price)
        if expected_spread_pct is None:
            return True, "no_expected_prices"

        min_expected = safe_decimal(getattr(config, "SPREAD_PROTECTION_MIN_EXPECTED_SPREAD_PCT", 0.0))
        if expected_spread_pct <= min_expected:
            return True, "expected_spread_too_small"

        x10_mid, lit_mid = await self._get_cross_exchange_mid_prices(symbol)
        current_spread_pct = self._spread_pct(x10_mid, lit_mid)
        if current_spread_pct is None:
            return True, "no_current_prices"

        # FIX (2025-12-19): Dynamic narrow_factor based on volatility
        narrow_factor = safe_decimal(getattr(config, "SPREAD_PROTECTION_NARROW_FACTOR", 0.70))
        
        # Determine volatility regime for symbol (if available)
        try:
            vol_monitor = get_volatility_monitor()
            vol_regime = vol_monitor.current_regimes.get(symbol, "NORMAL")
            
            if vol_regime == "HIGH":
                # High volatility: be more lenient (0.50 instead of 0.70)
                narrow_factor = Decimal("0.50")
                logger.info(f"📊 {symbol}: HIGH volatility regime - using relaxed spread factor {narrow_factor}")
            elif vol_regime == "HARD_CAP":
                # Extreme volatility: very lenient (0.40)
                narrow_factor = Decimal("0.40")
                logger.info(f"📊 {symbol}: HARD_CAP volatility - using very relaxed factor {narrow_factor}")
            elif vol_regime == "LOW":
                # Low volatility: moderate protection (0.60)
                narrow_factor = Decimal("0.60")
                logger.info(f"📊 {symbol}: LOW volatility regime - using moderate factor {narrow_factor}")
            elif vol_regime == "NORMAL":
                # NORMAL regime: use default/config value
                logger.info(f"📊 {symbol}: NORMAL volatility regime - using config factor {narrow_factor}")
        except Exception as vol_err:
            logger.warning(f"⚠️ Could not apply volatility-based factor adjustment: {vol_err}")
        
        # Validate factor bounds
        if narrow_factor <= 0 or narrow_factor >= 1:
            narrow_factor = Decimal("0.70")

        if current_spread_pct < (expected_spread_pct * narrow_factor):
            msg = (
                f"Spread narrowed {float(expected_spread_pct*100):.3f}% → {float(current_spread_pct*100):.3f}% "
                f"(factor={float(narrow_factor):.2f}, vol_regime=auto)"
            )
            return False, msg

        return True, "ok"

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
            # PHASE 1: X10 MAKER ORDER (REVERSED STRATEGY)
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()

            # FIX (2025-12-19): Fresh price fetch immediately before order placement
            # This ensures we use the most current market data, not cached prices
            try:
                fresh_lighter_price = None
                fresh_x10_price = None

                # Get fresh prices from both exchanges
                if hasattr(self.lighter, 'get_orderbook_mid_price'):
                    fresh_lighter_price = await self.lighter.get_orderbook_mid_price(symbol)
                if hasattr(self.x10, 'get_orderbook_mid_price'):
                    fresh_x10_price = await self.x10.get_orderbook_mid_price(symbol)

                # Use fresh prices if available, otherwise fall back to cached
                if fresh_lighter_price and fresh_lighter_price > 0:
                    logger.info(f"✅ [FRESH PRICE] {symbol} Lighter: ${fresh_lighter_price:.6f}")
                    execution.entry_price_lighter = fresh_lighter_price
                if fresh_x10_price and fresh_x10_price > 0:
                    logger.info(f"✅ [FRESH PRICE] {symbol} X10: ${fresh_x10_price:.6f}")
                    execution.entry_price_x10 = fresh_x10_price

            except Exception as e:
                logger.warning(f"⚠️ [FRESH PRICE] Could not fetch fresh prices: {e}")

            logger.info(f"📤 [PHASE 1] {symbol}: Placing X10 {execution.side_x10} LIMIT order (MAKER)...")
            logger.info(f"   Size: ${execution.size_x10:.2f} ({execution.quantity_coins:.6f} coins)")
            logger.info(f"   Strategy: POST_ONLY (Maker Fee = 0%)")
            execution.state = ExecutionState.LEG1_SENT

            # Execute X10 LIMIT order with post_only=True
            x10_success, x10_order_id = await self._execute_x10_leg(
                symbol,
                execution.side_x10,
                "COINS",  # Size type
                execution.quantity_coins,  # Size value in coins
                post_only=True,  # MAKER ORDER
            )

            phase_times["x10_order_placement"] = time.monotonic() - phase_start
            execution.x10_order_id = x10_order_id

            if not x10_success or not x10_order_id:
                logger.error(
                    f"❌ [PHASE 1] {symbol}: X10 order FAILED ({phase_times['x10_order_placement']:.2f}s)"
                )
                self._log_trade_summary(
                    symbol,
                    "ABORTED",
                    "X10 placement failed",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                execution.state = ExecutionState.FAILED
                execution.error = "X10 Placement Failed"
                return False, None, None

            logger.info(
                f"✅ [PHASE 1] {symbol}: X10 order placed ({phase_times['x10_order_placement']:.2f}s)"
            )
            logger.info(f"   Order ID: {x10_order_id[:16]}...")

            # ═══════════════════════════════════════════════════════════════
            # PHASE 1.5: WAIT FOR X10 FILL (NEW - Critical Verification!)
            # ═══════════════════════════════════════════════════════════════
            phase_start = time.monotonic()
            is_shutting_down = getattr(config, "IS_SHUTTING_DOWN", False)

            # Timeout configuration for X10 MAKER fill wait
            if is_shutting_down:
                max_wait_seconds = 2.0
            else:
                # FIX (2025-12-19): Use WebSocket events + LONGER timeout for slow-filling MAKER orders
                # CRITICAL: VIRTUAL-USD filled after 19s, FARTCOIN EXPIRED after 29s
                # WebSocket events can arrive AFTER asyncio.wait_for() timeout!
                # Use MAX timeout to allow slow fills (liquid pairs fill instantly, illiquid take 30-60s)
                max_wait_seconds = getattr(config, 'MAKER_ORDER_MAX_TIMEOUT_SECONDS', 60.0)
                logger.debug(f"   Using MAKER timeout from config: {max_wait_seconds}s (WebSocket events)")

            logger.info(f"⏳ [PHASE 1.5] {symbol}: Waiting for X10 fill (max {max_wait_seconds:.1f}s)...")

            # Use new check_order_filled function from x10_adapter
            x10_filled, fill_info = await self.x10.check_order_filled(
                symbol=symbol,
                order_id=x10_order_id,
                timeout=max_wait_seconds,
                check_interval=0.5
            )

            phase_times["x10_fill_wait"] = time.monotonic() - phase_start

            if not x10_filled:
                # X10 order not filled (CANCELLED, TIMEOUT, or REJECTED)
                fill_status = fill_info.get('status', 'UNKNOWN') if fill_info else 'UNKNOWN'
                logger.error(
                    f"❌ [PHASE 1.5] {symbol}: X10 order NOT FILLED after {max_wait_seconds:.1f}s (status={fill_status})"
                )
                logger.warning(f"🛑 {symbol}: ABORTING trade - no zombie risk! (X10 was gate)")

                # Cancel X10 order if still open (defensive cleanup)
                try:
                    if hasattr(self.x10, 'cancel_all_orders'):
                        await self.x10.cancel_all_orders(symbol)
                except Exception as e:
                    logger.debug(f"⚠️ {symbol}: Error cancelling X10 order after timeout: {e}")

                self._log_trade_summary(
                    symbol,
                    "ABORTED",
                    f"X10 not filled ({fill_status})",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                execution.state = ExecutionState.FAILED
                execution.error = f"X10_NOT_FILLED_{fill_status}"
                return False, None, None

            # X10 FILLED successfully!
            execution.x10_filled = True
            logger.info(f"✅ [PHASE 1.5] {symbol}: X10 FILLED ({phase_times['x10_fill_wait']:.2f}s)")

            # Extract fill details
            if fill_info:
                filled_qty = fill_info.get('filled_qty', 0)
                avg_fill_price = fill_info.get('avg_fill_price', 0)
                logger.info(f"   Filled Quantity: {filled_qty:.6f} coins")
                logger.info(f"   Avg Fill Price: ${avg_fill_price:.6f}")

                # Update execution with actual fill data
                execution.quantity_coins = filled_qty
                if avg_fill_price > 0:
                    execution.entry_price_x10 = avg_fill_price

            # ═══════════════════════════════════════════════════════════════
            # PHASE 2: LIGHTER MARKET HEDGE (REVERSED STRATEGY)
            # ═══════════════════════════════════════════════════════════════
            # X10 is filled! Now immediately hedge with Lighter MARKET order
            phase_start = time.monotonic()

            logger.info(f"📤 [PHASE 2] {symbol}: Placing Lighter {execution.side_lighter} MARKET hedge...")
            logger.info(f"   Size: {execution.quantity_coins:.6f} coins (matching X10 fill)")
            logger.info(f"   Strategy: IOC/MARKET (instant fill)")

            # Execute Lighter MARKET order (post_only=False -> IOC)
            lighter_success, lighter_order_id = await self._execute_lighter_leg(
                symbol,
                execution.side_lighter,
                execution.size_lighter,
                post_only=False,  # MARKET/IOC ORDER
                amount_coins=execution.quantity_coins,
            )

            phase_times["lighter_hedge"] = time.monotonic() - phase_start
            execution.lighter_order_id = lighter_order_id

            if lighter_success and lighter_order_id:
                logger.info(f"✅ [PHASE 2] {symbol}: Lighter hedge FILLED ({phase_times['lighter_hedge']:.2f}s)")
                logger.info(f"   Order ID: {lighter_order_id[:40]}...")
                execution.lighter_filled = True
                execution.state = ExecutionState.COMPLETE

                # Fetch actual Lighter fill price from position
                try:
                    await asyncio.sleep(0.5)  # Let position update
                    positions = await self.lighter.fetch_open_positions()
                    lighter_pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                    if lighter_pos:
                        avg_price = safe_float(lighter_pos.get('avg_entry_price') or lighter_pos.get('entry_price', 0))
                        if avg_price > 0:
                            execution.entry_price_lighter = avg_price
                            logger.info(f"   Lighter Fill Price: ${avg_price:.6f}")
                except Exception as e:
                    logger.warning(f"⚠️ [PHASE 2] {symbol}: Could not fetch Lighter fill price: {e}")

                # ═══════════════════════════════════════════════════════════════
                # ENTRY PRICE VALIDATION (NEW - Catch bad fills early!)
                # ═══════════════════════════════════════════════════════════════
                try:
                    x10_price = execution.entry_price_x10
                    lighter_price = execution.entry_price_lighter

                    if x10_price > 0 and lighter_price > 0:
                        # Calculate entry spread
                        entry_spread_abs = abs(x10_price - lighter_price)
                        entry_spread_pct = (entry_spread_abs / ((x10_price + lighter_price) / 2)) * 100

                        logger.info(f"📊 [ENTRY VALIDATION] {symbol}: Entry Spread = {entry_spread_pct:.3f}%")
                        logger.info(f"   X10: ${x10_price:.6f}, Lighter: ${lighter_price:.6f}")

                        # Check if entry spread is too large (indicates bad fills)
                        max_entry_spread_pct = getattr(config, 'MAX_ENTRY_SPREAD_PCT', 0.5)  # Default 0.5%

                        if entry_spread_pct > max_entry_spread_pct:
                            logger.error(
                                f"❌ [ENTRY VALIDATION] {symbol}: Entry spread TOO LARGE "
                                f"({entry_spread_pct:.3f}% > {max_entry_spread_pct:.2f}%) - BAD FILLS!"
                            )
                            logger.warning(f"⚠️ {symbol}: This trade will likely be unprofitable. Consider immediate close.")

                            # Optional: Auto-close if entry is too bad
                            auto_close_bad_entries = getattr(config, 'AUTO_CLOSE_BAD_ENTRIES', False)
                            if auto_close_bad_entries:
                                logger.warning(f"🔄 {symbol}: AUTO_CLOSE_BAD_ENTRIES enabled - closing positions immediately!")
                                await self._execute_immediate_rollback(execution)

                                self._log_trade_summary(
                                    symbol,
                                    "CLOSED_BAD_ENTRY",
                                    f"Entry spread too large ({entry_spread_pct:.3f}%)",
                                    trade_start_time,
                                    phase_times,
                                    execution,
                                )
                                execution.state = ExecutionState.FAILED
                                execution.error = f"BAD_ENTRY_SPREAD_{entry_spread_pct:.2f}PCT"
                                return False, x10_order_id, lighter_order_id
                        else:
                            logger.info(f"✅ [ENTRY VALIDATION] {symbol}: Entry spread OK ({entry_spread_pct:.3f}% < {max_entry_spread_pct:.2f}%)")
                except Exception as e:
                    logger.warning(f"⚠️ [ENTRY VALIDATION] {symbol}: Validation error: {e}")

                self._log_trade_summary(
                    symbol,
                    "SUCCESS",
                    "Hedged trade complete (X10 MAKER → Lighter MARKET)",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                return True, x10_order_id, lighter_order_id
            else:
                # Lighter hedge failed - CRITICAL! X10 position is exposed
                logger.error(f"❌ [PHASE 2] {symbol}: Lighter hedge FAILED ({phase_times['lighter_hedge']:.2f}s)")
                logger.warning(f"🔄 {symbol}: Initiating IMMEDIATE rollback - X10 position exposed!")

                # Execute rollback IMMEDIATELY
                await self._execute_immediate_rollback(execution)

                self._log_trade_summary(
                    symbol,
                    "FAILED",
                    "Lighter hedge failed (X10 exposed)",
                    trade_start_time,
                    phase_times,
                    execution,
                )
                execution.state = ExecutionState.FAILED
                execution.error = "LIGHTER_HEDGE_FAILED"
                return False, x10_order_id, None

            # ═══════════════════════════════════════════════════════════════
            # SECTION END: Trade Execution Complete
            # ═══════════════════════════════════════════════════════════════

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
        """Wait for Lighter order to fill (hybrid: event-based + polling fallback)
        
        FIX 7 (2025-12-14): Reduced polling interval from 0.5s to 0.3s for ~40% faster Ghost-Fill detection.
        Pattern from TS SDK: Faster polling reduces unhedged exposure window significantly.
        Also uses event-based detection via _position_fill_events when available.
        """
        wait_start = time.time()
        check_count = 0
        # FIX 7: Reduced from 0.5s to 0.3s for faster fill detection (~40% improvement)
        # TS SDK Pattern: Minimize detection latency to reduce unhedged exposure window
        polling_interval = 0.3  # Faster polling to reduce Ghost-Fill window
        # For IOC orders, check order status early to detect cancellations
        order_status_checked = False
        
        # Setup event-based fill detection (if not already setup)
        fill_event = None
        if execution.quantity_coins > 0:
            fill_event = self._setup_fill_event(symbol, execution.quantity_coins)

        try:
            while time.time() - wait_start < max_wait_seconds:
                if getattr(config, "IS_SHUTTING_DOWN", False):
                    logger.warning(f"⚡ [LIGHTER FILL] {symbol}: SHUTDOWN detected - aborting wait!")
                    return False

                # FIXED: Check event first (fastest path - set by position callbacks)
                if fill_event and fill_event.is_set():
                    fill_time = time.time() - wait_start
                    logger.info(f"🔔 [LIGHTER FILL] {symbol}: Fill detected via EVENT after {fill_time:.2f}s!")
                    return True

                check_count += 1
                try:
                    # Check position via REST (fallback path)
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
                                # FIX 7: More aggressive fresh-fetch with shorter delays
                                max_fresh_fetch_attempts = 12 if is_post_only_likely else 5  # Increased attempts
                                fresh_fetch_attempts = 0
                                fresh_fetch_delay = 0.2  # FIX 7: Reduced from 0.5s to 0.2s
                                
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
        finally:
            # FIXED: Cleanup fill event to prevent memory leaks
            self._cleanup_fill_event(symbol)

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
            logger.error(f"❌ X10 leg exception {symbol} ({side}, size={size_value} {size_type}): {e}", exc_info=True)
            return False, None

    async def _execute_x10_maker_with_escalation(
        self,
        symbol: str,
        side: str,
        size_coins: float,
        timeout_per_cycle: float = None,
        max_requotes: int = None,
        price_chase_pct: float = None,
    ) -> Tuple[bool, Optional[str], bool]:
        """
        X10 MAKER ENGINE - Speed-First Strategy with Taker Escalation.
        
        IMPORTANT: X10 is LESS liquid than Lighter!
        → Short Maker attempts, fast escalation to Taker
        → Hedge completion is MORE important than fee savings
        
        Flow:
        1. Place POST_ONLY order (0% fees)
        2. Wait for fill (3s default)
        3. If not filled: Cancel + Requote with more aggressive price (1 attempt)
        4. If still not filled: Escalate to TAKER (IOC)
        5. Ghost-Fill protection: Position-delta check before each Cancel/Replace
        
        Args:
            symbol: Trading symbol
            side: BUY or SELL
            size_coins: Size in coins (already validated against X10 min)
            timeout_per_cycle: Seconds to wait per attempt (default from config)
            max_requotes: Max Cancel/Replace attempts (default from config)
            price_chase_pct: How much more aggressive per requote (default from config)
            
        Returns:
            Tuple[success, order_id, used_taker]:
                - success: True if order filled (maker or taker)
                - order_id: The filled order ID
                - used_taker: True if had to escalate to Taker
        """
        # Load config with defaults
        timeout = timeout_per_cycle or float(getattr(config, 'X10_MAKER_TIMEOUT_SECONDS', 3.0))
        requotes = max_requotes or int(getattr(config, 'X10_MAKER_MAX_REQUOTES', 1))
        chase_pct = price_chase_pct or float(getattr(config, 'X10_MAKER_PRICE_CHASE_PCT', 0.001))
        check_interval = float(getattr(config, 'X10_MAKER_FILL_CHECK_INTERVAL', 0.3))
        
        # Shutdown mode: faster timeout
        is_shutdown = getattr(config, 'IS_SHUTTING_DOWN', False)
        if is_shutdown:
            timeout = float(getattr(config, 'X10_MAKER_SHUTDOWN_TIMEOUT_SECONDS', 2.0))
            requotes = 0  # Skip requotes during shutdown
            logger.warning(f"⚡ [X10 MAKER] {symbol}: SHUTDOWN MODE - timeout={timeout}s, requotes=0")
        
        total_start = time.monotonic()
        current_order_id: Optional[str] = None
        filled = False
        used_taker = False
        requote_count = 0
        
        # Get initial position snapshot for ghost-fill detection
        initial_position_size = 0.0
        try:
            positions = await self.x10.fetch_open_positions()
            pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
            initial_position_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
        except Exception as e:
            logger.debug(f"[X10 MAKER] {symbol}: Initial position check error: {e}")
        
        logger.info(
            f"🎯 [X10 MAKER] {symbol} {side}: Starting Maker-First strategy | "
            f"size={size_coins:.6f} coins, timeout={timeout}s, max_requotes={requotes}"
        )
        
        # ═══════════════════════════════════════════════════════════════
        # MAKER ATTEMPT LOOP (1 initial + N requotes)
        # ═══════════════════════════════════════════════════════════════
        for attempt in range(requotes + 1):
            if getattr(config, 'IS_SHUTTING_DOWN', False) and attempt > 0:
                logger.warning(f"⚡ [X10 MAKER] {symbol}: SHUTDOWN detected - skipping requote {attempt}")
                break
                
            cycle_start = time.monotonic()
            
            # Calculate price (more aggressive with each requote)
            price_adjustment = 1.0 + (chase_pct * attempt) if side == "BUY" else 1.0 - (chase_pct * attempt)
            
            # Place POST_ONLY order
            try:
                if attempt == 0:
                    logger.info(f"📤 [X10 MAKER] {symbol}: Placing initial POST_ONLY {side} order...")
                else:
                    logger.info(
                        f"🔄 [X10 MAKER] {symbol}: Requote {attempt}/{requotes} - "
                        f"ATOMIC REPLACE (previous_order_id={current_order_id}) - "
                        f"chasing price by {chase_pct * attempt * 100:.2f}%..."
                    )

                success, order_id = await self.x10.open_live_position(
                    symbol=symbol,
                    side=side,
                    notional_usd=0,  # Use amount directly
                    amount=size_coins,
                    post_only=True,
                    reduce_only=False,
                    previous_order_id=current_order_id if attempt > 0 else None,  # FIX: Atomic replace!
                )
                
                if not success or not order_id:
                    logger.warning(f"⚠️ [X10 MAKER] {symbol}: POST_ONLY order placement failed (attempt {attempt})")
                    if attempt < requotes:
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        break
                
                current_order_id = order_id
                logger.debug(f"✓ [X10 MAKER] {symbol}: Order placed: {order_id}")
                
            except Exception as e:
                logger.error(f"❌ [X10 MAKER] {symbol}: Order placement exception: {e}")
                if attempt < requotes:
                    await asyncio.sleep(0.5)
                    continue
                else:
                    break
            
            # ═══════════════════════════════════════════════════════════════
            # WAIT FOR FILL (with position monitoring)
            # ═══════════════════════════════════════════════════════════════
            wait_start = time.time()
            while time.time() - wait_start < timeout:
                if getattr(config, 'IS_SHUTTING_DOWN', False):
                    logger.warning(f"⚡ [X10 MAKER] {symbol}: SHUTDOWN during wait - breaking!")
                    break
                
                try:
                    # Check position (faster than checking order status)
                    positions = await self.x10.fetch_open_positions()
                    pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                    current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                    
                    # Calculate position delta (detect fill)
                    size_delta = abs(current_size - initial_position_size)
                    
                    # Check if filled (>= 90% of requested size appeared)
                    if size_delta >= size_coins * 0.90:
                        filled = True
                        fill_time = time.time() - wait_start
                        total_time = time.monotonic() - total_start
                        logger.info(
                            f"✅ [X10 MAKER] {symbol}: MAKER FILLED in {fill_time:.2f}s! "
                            f"(attempt {attempt}, total={total_time:.2f}s, size_delta={size_delta:.6f})"
                        )
                        return True, current_order_id, False  # Success, Maker fill
                    
                    await asyncio.sleep(check_interval)
                    
                except Exception as e:
                    logger.debug(f"[X10 MAKER] {symbol}: Fill check error: {e}")
                    await asyncio.sleep(check_interval)
            
            # Timeout - check for ghost fill before cancel
            if not filled and attempt < requotes:
                # Ghost-Fill protection: Check position BEFORE cancel
                try:
                    positions = await self.x10.fetch_open_positions()
                    pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                    current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                    size_delta = abs(current_size - initial_position_size)
                    
                    if size_delta >= size_coins * 0.50:
                        # Partial fill large enough - count as success
                        logger.warning(
                            f"⚠️ [X10 MAKER] {symbol}: GHOST FILL detected before cancel! "
                            f"size_delta={size_delta:.6f} >= 50% of {size_coins:.6f}"
                        )
                        return True, current_order_id, False  # Partial maker fill
                        
                except Exception as e:
                    logger.debug(f"[X10 MAKER] {symbol}: Ghost check error: {e}")
                
                # Cancel and continue to next requote
                try:
                    logger.debug(f"🗑️ [X10 MAKER] {symbol}: Cancelling order for requote...")
                    await self.x10.cancel_order(current_order_id, symbol)
                    await asyncio.sleep(0.2)  # Brief settle time
                except Exception as e:
                    logger.debug(f"[X10 MAKER] {symbol}: Cancel error (may already be filled): {e}")
                    
                    # After cancel error, re-check for ghost fill
                    try:
                        positions = await self.x10.fetch_open_positions()
                        pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                        current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                        size_delta = abs(current_size - initial_position_size)
                        
                        if size_delta >= size_coins * 0.50:
                            logger.warning(f"⚠️ [X10 MAKER] {symbol}: Fill detected after cancel error!")
                            return True, current_order_id, False
                    except Exception:
                        pass
                
                requote_count = attempt + 1
        
        # ═══════════════════════════════════════════════════════════════
        # TAKER ESCALATION (if maker attempts exhausted)
        # ═══════════════════════════════════════════════════════════════
        if not filled and getattr(config, 'X10_MAKER_ESCALATION_ENABLED', True):
            logger.warning(
                f"🚀 [X10 MAKER→TAKER] {symbol}: Maker attempts exhausted ({requote_count} requotes) - "
                f"Escalating to TAKER IOC!"
            )
            
            # Cancel any remaining maker order
            if current_order_id:
                try:
                    await self.x10.cancel_order(current_order_id, symbol)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
            
            # Final ghost-fill check before taker
            try:
                positions = await self.x10.fetch_open_positions()
                pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                size_delta = abs(current_size - initial_position_size)
                
                if size_delta >= size_coins * 0.50:
                    logger.warning(f"⚠️ [X10 TAKER] {symbol}: Fill detected before taker! (ghost fill)")
                    return True, current_order_id, False
            except Exception:
                pass
            
            # Place TAKER (IOC) order
            try:
                taker_start = time.monotonic()
                success, taker_order_id = await self.x10.open_live_position(
                    symbol=symbol,
                    side=side,
                    notional_usd=0,
                    amount=size_coins,
                    post_only=False,  # TAKER
                    reduce_only=False,
                )
                
                if success and taker_order_id:
                    # Wait briefly for fill
                    await asyncio.sleep(0.5)
                    
                    # Verify fill
                    positions = await self.x10.fetch_open_positions()
                    pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
                    current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                    size_delta = abs(current_size - initial_position_size)
                    
                    if size_delta >= size_coins * 0.90:
                        taker_time = time.monotonic() - taker_start
                        total_time = time.monotonic() - total_start
                        logger.info(
                            f"✅ [X10 TAKER] {symbol}: TAKER FILLED in {taker_time:.2f}s! "
                            f"(total={total_time:.2f}s, used_taker=True)"
                        )
                        return True, taker_order_id, True  # Success, Taker fill
                    else:
                        logger.warning(
                            f"⚠️ [X10 TAKER] {symbol}: Taker order placed but fill not confirmed "
                            f"(expected={size_coins:.6f}, delta={size_delta:.6f})"
                        )
                else:
                    logger.error(f"❌ [X10 TAKER] {symbol}: Taker order placement failed!")
                    
            except Exception as e:
                logger.error(f"❌ [X10 TAKER] {symbol}: Taker exception: {e}", exc_info=True)
        
        # All attempts failed
        total_time = time.monotonic() - total_start
        logger.error(
            f"❌ [X10 MAKER] {symbol}: All attempts FAILED! "
            f"(total_time={total_time:.2f}s, requotes={requote_count})"
        )
        return False, current_order_id, used_taker


    async def _get_fresh_maker_price(self, symbol: str, side: str) -> Optional[float]:
        """
        Get the current best maker price for an order.

        Returns the price that would be used for a new maker order right now.
        Uses get_maker_price from lighter_adapter if available.

        Args:
            symbol: Trading symbol
            side: BUY or SELL

        Returns:
            float: Best maker price, or None if unavailable
        """
        try:
            if hasattr(self.lighter, 'get_maker_price'):
                price = await self.lighter.get_maker_price(symbol, side)
                if price and price > 0:
                    return float(price)

            # Fallback: Use mark price
            mark = self.lighter.fetch_mark_price(symbol)
            if mark and mark > 0:
                return float(mark)

            return None
        except Exception as e:
            logger.debug(f"⚠️ _get_fresh_maker_price error for {symbol}: {e}")
            return None

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

    async def _execute_immediate_rollback(self, execution: TradeExecution) -> bool:
        """
        Execute rollback IMMEDIATELY (synchronous) with POSITION VERIFICATION.
        
        FIX (2025-12-19): "Ghost Position" Prevention
        - Previously: Assumed 'Order Accepted' == 'Position Closed'. (Wrong for IOC!)
        - Now: Loops until position size is CONFIRMED zero via API fetch.
        """
        symbol = execution.symbol
        execution.state = ExecutionState.ROLLBACK_IN_PROGRESS
        self._stats["rollbacks_triggered"] += 1
        
        logger.warning(f"═══════════════════════════════════════════════════════════")
        logger.warning(f"🔄 IMMEDIATE ROLLBACK: {symbol}")
        logger.warning(f"   Lighter Filled: {execution.lighter_filled}, X10 Filled: {execution.x10_filled}")
        logger.warning(f"═══════════════════════════════════════════════════════════")
        
        try:
            # Step 1: Cancel any remaining Lighter orders for this symbol
            try:
                if hasattr(self.lighter, 'cancel_all_orders'):
                    await self.lighter.cancel_all_orders(symbol)
                    logger.info(f"🧹 [ROLLBACK] {symbol}: Cancelled orders")
            except Exception as cancel_e:
                logger.warning(f"⚠️ [ROLLBACK] {symbol}: Cancel error: {cancel_e}")
            
            # Step 2: Retry Loop for verified closure
            max_retries = 5
            
            for attempt in range(max_retries):
                # A. Fetch current position
                # Force fresh fetch to avoid stale cache
                if hasattr(self.lighter, '_positions_cache'):
                    self.lighter._positions_cache = None
                    
                positions = await self.lighter.fetch_open_positions()
                pos = next(
                    (p for p in (positions or []) 
                     if p.get('symbol') == symbol),
                    None
                )
                
                # Check size
                current_size = abs(safe_float(pos.get('size', 0))) if pos else 0.0
                
                # SUCCESS CHECK: Is position gone?
                if current_size < 1e-8:
                    logger.info(f"✅ [ROLLBACK] SUCCESS: Position {symbol} is closed (size={current_size}).")
                    execution.state = ExecutionState.ROLLBACK_DONE
                    self._stats["rollbacks_successful"] += 1
                    return True
                
                logger.warning(f"🔄 [ROLLBACK] {symbol}: Position Open (Size={current_size:.6f}). Attempting Close {attempt+1}/{max_retries}...")
                
                # B. Determine Close Params
                actual_size_signed = safe_float(pos.get('size', 0))
                # If Long defined by positive size -> Close by Selling
                close_side = "SELL" if actual_size_signed > 0 else "BUY"
                
                # Fetch price for notional calc
                price = self.lighter.fetch_mark_price(symbol) or 0.0
                if price <= 0:
                    logger.warning(f"⚠️ [ROLLBACK] {symbol}: Zero/Missing price. Using defensive default.")
                    price = 1.0 # Should not happen usually
                
                # Calculate Close Notional
                close_notional = current_size * price
                
                # C. Execute Close (IOC)
                try:
                    # Use close_live_position logic from adapter
                    ok, order_id = await self.lighter.close_live_position(
                        symbol=symbol,
                        original_side="BUY" if actual_size_signed > 0 else "SELL", # This arg name in adapter is 'original_side'
                        notional_usd=close_notional
                    )
                    
                    if ok:
                        logger.info(f"📤 [ROLLBACK] {symbol}: Close Order Sent (IOC, ID: {order_id})")
                    else:
                        logger.warning(f"❌ [ROLLBACK] {symbol}: Close Order Rejected/Failed")
                        
                except Exception as close_e:
                    logger.warning(f"⚠️ [ROLLBACK] {symbol}: Close Exception: {close_e}")
                
                # D. Wait for fill/settlement before re-check
                # IOC orders are instant, but API update might lag slightly.
                # 1.0s wait gives robust buffer.
                await asyncio.sleep(1.0)
            
            # If loop finishes and position still exists:
            logger.critical(f"🚨 [ROLLBACK] FAILED: {symbol} position STILL OPEN (Size={current_size:.6f}) after {max_retries} attempts!")
            logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED!")
            execution.state = ExecutionState.ROLLBACK_FAILED
            self._stats["rollbacks_failed"] += 1
            return False
                 
        except Exception as e:
            logger.critical(f"🚨 [ROLLBACK] EXCEPTION {symbol}: {e}")
            logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED for {symbol}!")
            execution.state = ExecutionState.ROLLBACK_FAILED
            self._stats["rollbacks_failed"] += 1
            return False


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

            # ═══════════════════════════════════════════════════════════════
            # STEP 4.5: Entry spread gate (align Maker validation with
            # MAX_SPREAD_FILTER_PERCENT used in opportunity filtering).
            #
            # Notes on units:
            # - config.MAX_SPREAD_FILTER_PERCENT is a fraction (e.g. 0.003 = 0.3%)
            # - OrderbookValidationResult.spread_percent is in percent units (e.g. 0.3)
            # ═══════════════════════════════════════════════════════════════
            try:
                max_spread_fraction = getattr(config, "MAX_SPREAD_FILTER_PERCENT", None)
                if max_spread_fraction is not None and result.spread_percent is not None:
                    # Compare using MID-based spread fraction to align with other parts of the bot
                    # (e.g. opportunities use fractional spreads; bid-based % can slightly exceed mid-based at the boundary).
                    best_bid = result.best_bid
                    best_ask = result.best_ask
                    max_spread_frac = Decimal(str(max_spread_fraction))
                    epsilon_frac = Decimal("0.000001")  # 1e-6 = 0.0001%

                    spread_frac_mid = None
                    try:
                        if best_bid is not None and best_ask is not None:
                            mid = (best_bid + best_ask) / Decimal("2")
                            if mid > 0:
                                spread_frac_mid = (best_ask - best_bid) / mid
                    except Exception:
                        spread_frac_mid = None

                    # Fall back to bid-based percent if mid-based cannot be computed
                    if spread_frac_mid is None:
                        max_spread_pct = max_spread_frac * Decimal("100")
                        if max_spread_pct > 0 and result.spread_percent > (max_spread_pct + Decimal("0.0001")):
                            return OrderbookValidationResult(
                                is_valid=False,
                                quality=OrderbookQuality.INSUFFICIENT,
                                reason=(
                                    f"Entry spread gate tripped ({result.spread_percent:.2f}% > {max_spread_pct:.2f}%)"
                                ),
                                bid_depth_usd=result.bid_depth_usd,
                                ask_depth_usd=result.ask_depth_usd,
                                spread_percent=result.spread_percent,
                                best_bid=result.best_bid,
                                best_ask=result.best_ask,
                                bid_levels=result.bid_levels,
                                ask_levels=result.ask_levels,
                                staleness_seconds=result.staleness_seconds,
                                recommended_action="wait",
                                profile_used=result.profile_used,
                            )
                        return result

                    if max_spread_frac > 0 and spread_frac_mid > (max_spread_frac + epsilon_frac):
                        max_spread_pct_mid = max_spread_frac * Decimal("100")
                        spread_pct_mid = spread_frac_mid * Decimal("100")
                        return OrderbookValidationResult(
                            is_valid=False,
                            quality=OrderbookQuality.INSUFFICIENT,
                            reason=(
                                f"Entry spread gate tripped ({spread_pct_mid:.2f}% > {max_spread_pct_mid:.2f}%)"
                            ),
                            bid_depth_usd=result.bid_depth_usd,
                            ask_depth_usd=result.ask_depth_usd,
                            spread_percent=result.spread_percent,
                            best_bid=result.best_bid,
                            best_ask=result.best_ask,
                            bid_levels=result.bid_levels,
                            ask_levels=result.ask_levels,
                            staleness_seconds=result.staleness_seconds,
                            recommended_action="wait",
                            profile_used=result.profile_used,
                        )
            except Exception:
                # Never fail validation due to gating logic errors
                pass
            
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
