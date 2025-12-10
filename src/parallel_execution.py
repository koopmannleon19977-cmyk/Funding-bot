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
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


import config
from src.utils import safe_float, safe_decimal
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

    def __init__(self, x10_adapter, lighter_adapter, db, circuit_breaker=None):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.db = db
        self.circuit_breaker = circuit_breaker
        self.execution_locks: Dict[str, asyncio.Lock] = {}
        self.active_executions: Dict[str, TradeExecution] = {}
        self._rollback_queue: asyncio.Queue[Optional[TradeExecution]] = asyncio.Queue()
        self._rollback_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._stats = {
            "total_executions": 0,
            "successful": 0,
            "failed": 0,
            "rollbacks_triggered": 0,
            "rollbacks_successful": 0,
            "rollbacks_failed": 0,
            "rollbacks": 0
        }
        logger.info("✅ ParallelExecutionManager started")

    async def start(self):
        """Start background rollback processor"""
        self._rollback_task = asyncio.create_task(self._rollback_processor())
        logger.info("✅ ParallelExecutionManager: Rollback processor started")

    async def stop(self):
        """Stop background tasks gracefully and abort all active executions"""
        self._shutdown_event.set()
        self.is_running = False  # Block new executions
        
        # FAST SHUTDOWN: Clear active executions immediately to unblock shutdown
        if self.active_executions:
            logger.warning(f"⚡ ParallelExec: Aborting {len(self.active_executions)} active executions for fast shutdown!")
            self.active_executions.clear()
        
        if self._rollback_task:
            self._rollback_task.cancel()
            try:
                await self._rollback_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ ParallelExecutionManager: Stopped")

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
                
                if self.circuit_breaker:
                    self.circuit_breaker.record_trade_result(success, symbol)
                
                return result

            except Exception as e:
                execution.state = ExecutionState.FAILED
                execution.error = str(e)
                self._stats["failed"] += 1
                logger.error(f"❌ [PARALLEL] {symbol}: Exception: {e}", exc_info=True)
                
                if self.circuit_breaker:
                    self.circuit_breaker.record_trade_result(False, symbol)
                    
                return False, None, None
            
            finally:
                # Immediately cleanup execution from active list
                # This ensures failed/completed trades don't block the slot
                self.active_executions.pop(symbol, None)

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
                if is_shutting_down:
                    logger.warning(f"⚡ [MAKER STRATEGY] {symbol}: SHUTDOWN - skipping extended ghost fill checks!")
                else:
                    # 2. FIX: Der "Paranoid Check" muss aggressiver sein (User Request)
                    logger.info(f"🔍 [MAKER STRATEGY] {symbol}: Checking for Ghost Fills (EXTENDED CHECK)...")
                    
                    # Erhöht von 10 auf 30 Versuche mit ansteigendem Delay (insg. ~60 Sekunden Abdeckung)
                    # OPTIMIZED: During shutdown, only do 3 quick checks
                    max_checks = 3 if getattr(config, 'IS_SHUTTING_DOWN', False) else 30
                    for i in range(max_checks): 
                        # FAST SHUTDOWN: Abort immediately if shutdown detected mid-loop
                        if getattr(config, 'IS_SHUTTING_DOWN', False):
                            logger.warning(f"⚡ {symbol}: SHUTDOWN detected during ghost check - aborting!")
                            break
                            
                        # Backoff: Wartet 1s, 1.2s, 1.4s ... bis max 3s
                        # OPTIMIZED: Only 0.3s during shutdown
                        wait_time = 0.3 if getattr(config, 'IS_SHUTTING_DOWN', False) else min(1.0 + (i * 0.2), 3.0)
                        await asyncio.sleep(wait_time)
                        
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
            logger.info(f"✓ [MAKER STRATEGY] {symbol}: Cancel confirmed (Clean Exit verified after 60s).")
            return False

    async def _execute_parallel_internal(
        self, 
        execution: TradeExecution,
        timeout: float
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Internal parallel execution logic"""
        symbol = execution.symbol
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 1: LIGHTER POST-ONLY (Maker)
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"🔄 [MAKER STRATEGY] {symbol}: Placing Lighter Maker Order...")
        execution.state = ExecutionState.LEG1_SENT

        # 1. Place Lighter Order
        lighter_success, lighter_order_id = await self._execute_lighter_leg(
            symbol, 
            execution.side_lighter, 
            execution.size_lighter,
            post_only=True,
            amount_coins=execution.quantity_coins
        )

        execution.lighter_order_id = lighter_order_id
        
        if not lighter_success or not lighter_order_id:
            logger.warning(f"❌ [MAKER STRATEGY] {symbol}: Lighter placement failed/rejected. Aborting.")
            execution.state = ExecutionState.FAILED
            execution.error = "Lighter Placement Failed"
            return False, None, None

        logger.info(f"⏳ [MAKER STRATEGY] {symbol}: Lighter placed ({lighter_order_id}), waiting for fill...")
        
        # 2. Wait for Fill (Polled Check)
        # We need to check if it fills. We give it e.g. 10-20 seconds.
        # If not filled, we cancel and abort.
        filled = False
        wait_start = time.time()
        # FIX: User requested 30-60s timeout for Maker strategies
        # OPTIMIZED: During shutdown, use only 2 seconds to abort quickly
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        MAX_WAIT_SECONDS = 2.0 if is_shutting_down else float(getattr(config, 'LIGHTER_ORDER_TIMEOUT_SECONDS', 60.0))
        
        while time.time() - wait_start < MAX_WAIT_SECONDS:
            # FAST SHUTDOWN: Abort immediately if shutdown detected
            if getattr(config, 'IS_SHUTTING_DOWN', False):
                logger.warning(f"⚡ [MAKER STRATEGY] {symbol}: SHUTDOWN detected - aborting wait!")
                break
                
            try:
                # Check position
                pos = await self.lighter.fetch_open_positions()
                # Find position
                p = next((x for x in (pos or []) if x.get('symbol') == symbol), None)
                current_size = safe_float(p.get('size', 0)) if p else 0.0
                
                # Check if position size is significant (indicates fill)
                # Note: This checks for *any* position, but given we protect symbols with locks,
                # this is a reasonable proxy for "our order filled".
                if execution.quantity_coins > 0 and abs(current_size) >= execution.quantity_coins * 0.95:
                    filled = True
                    break
                    
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.debug(f"Rank check error: {e}")
                await asyncio.sleep(1)

        if not filled:
             # Delegate to separate handler
             filled = await self._handle_maker_timeout(execution, lighter_order_id)
             
             if not filled:
                 execution.state = ExecutionState.FAILED
                 return False, None, lighter_order_id
        
        execution.lighter_filled = True
        
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 2: QUANTITY-BASED HEDGING (Match Coins, not USD)
        # ═══════════════════════════════════════════════════════════════════
        # PROBLEM: Different prices on Lighter/X10 mean $100 on Lighter != $100 on X10 in Coins.
        # FIX: We fetch the EXACT filled coin amount from Lighter and replicate it on X10.
        
        filled_size_coins = execution.quantity_coins
        logger.info(f"⚖️ HEDGING {symbol}: Matching calculated amount on X10: {filled_size_coins:.6f} coins")

        # 3. Execute X10 (Taker)
        execution.state = ExecutionState.LEG2_SENT
        
        # We use the NEW signature of _execute_x10_leg that accepts specific coin quantity
        x10_success, x10_order_id = await self._execute_x10_leg(
             symbol, 
             execution.side_x10, 
             size_type="COINS",
             size_value=filled_size_coins,
             post_only=False # Taker
        )
        
        execution.x10_order_id = x10_order_id
        execution.x10_filled = x10_success
        
        if x10_success:
             # Success!
             return True, x10_order_id, lighter_order_id
        else:
             # X10 Failed -> ROLLBACK Lighter!
             logger.error(f"❌ [MAKER STRATEGY] {symbol}: X10 Hedge failed! Rolling back Lighter...")
             await self._queue_rollback(execution)
             return False, None, lighter_order_id




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
                    logger.info(f"✅ [ROLLBACK] {symbol}: Complete (attempt {attempt + 1})")
                    return True

            except Exception as e:
                logger.error(f"❌ [ROLLBACK] {symbol}: Attempt {attempt + 1} error: {e}")

        if self.circuit_breaker:
            self.circuit_breaker.trigger_emergency_stop(
                reason=f"CRITICAL: Rollback failed for {symbol} after {self.MAX_ROLLBACK_ATTEMPTS} attempts! Risk of NAKED LEG!"
            )
        logger.error(f"❌ [ROLLBACK] {symbol}: All {self.MAX_ROLLBACK_ATTEMPTS} attempts failed! TRIGGERING KILL SWITCH!")
        return False

    async def _rollback_x10(self, execution: TradeExecution) -> bool:
        """Rollback X10 position with actual position verification"""
        symbol = execution.symbol
        
        try:
            positions = await self.x10.fetch_open_positions()
            pos = next(
                (p for p in (positions or []) 
                 if p.get('symbol') == symbol and abs(safe_float(p.get('size', 0))) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✓ X10 Rollback {symbol}: No position found (already closed?)")
                return True

            actual_size = safe_float(pos.get('size', 0))
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
                 if p.get('symbol') == symbol and abs(safe_float(p.get('size', 0))) > 1e-8),
                None
            )

            if not pos:
                logger.info(f"✓ Lighter Rollback {symbol}: No position found (already closed?)")
                return True

            actual_size = safe_float(pos.get('size', 0))
            original_side = "BUY" if actual_size > 0 else "SELL"
            close_size_coins = abs(actual_size)

            # CRITICAL FIX: Sichere Typ-Konvertierung
            raw_price = self.lighter.fetch_mark_price(symbol)
            mark_price = safe_float(raw_price)
            
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

    async def _run_compliance_check(self, symbol: str, side_x10: str, side_lighter: str) -> bool:
        """
        Checks for self-match / wash trading risks.
        Returns TRUE if safe to trade, FALSE if risk detected.
        """
        if not getattr(config, 'COMPLIANCE_CHECK_ENABLED', False):
            return True
        
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
                    return False
                
            return True
            
        except Exception as e:
            logger.error(f"Compliance Check Failed: {e}")
            # Fail safe: Allow trading if check errors locally
            return True

    def is_busy(self) -> bool:
        """Check if any executions or rollbacks are in progress"""
        return len(self.active_executions) > 0 or not self._rollback_queue.empty()