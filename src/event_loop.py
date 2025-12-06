# src/event_loop. py - PUNKT 10: EVENT-LOOP UMBAU

import asyncio
import signal
import logging
from typing import Optional, List, Callable, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import time

logger = logging.getLogger(__name__)


class TaskPriority(Enum):
    CRITICAL = 0  # WebSocket, Heartbeat
    HIGH = 1      # Trade Execution, Rollback
    NORMAL = 2    # Opportunity Scan, Trade Management
    LOW = 3       # Cleanup, Stats, Logging


@dataclass
class ManagedTask:
    name: str
    coro_factory: Callable
    priority: TaskPriority = TaskPriority. NORMAL
    restart_on_failure: bool = True
    restart_delay: float = 5.0
    max_restarts: int = 10
    restart_count: int = 0
    task: Optional[asyncio.Task] = None
    last_restart: float = 0.0
    enabled: bool = True


class BotEventLoop:
    """
    Central event loop manager for the trading bot.
    
    Features:
    - Task supervision with auto-restart
    - Priority-based task management
    - Graceful shutdown handling
    - Health monitoring
    - Signal handling (SIGINT, SIGTERM)
    """
    
    def __init__(self):
        self._tasks: Dict[str, ManagedTask] = {}
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._shutdown_timeout = 30.0
        
        # Components
        self. x10_adapter = None
        self.lighter_adapter = None
        self.parallel_exec = None
        self.ws_manager = None
        self. oi_tracker = None
        self.state_manager = None
        self.telegram_bot = None
        
        # Callbacks
        self._on_shutdown_callbacks: List[Callable] = []
        self._on_startup_callbacks: List[Callable] = []
    
    def set_components(self, **components):
        """Set bot components"""
        for name, component in components.items():
            if hasattr(self, name):
                setattr(self, name, component)
    
    def register_task(
        self,
        name: str,
        coro_factory: Callable,
        priority: TaskPriority = TaskPriority.NORMAL,
        restart_on_failure: bool = True,
        restart_delay: float = 5.0,
        max_restarts: int = 10
    ):
        """Register a managed task"""
        self._tasks[name] = ManagedTask(
            name=name,
            coro_factory=coro_factory,
            priority=priority,
            restart_on_failure=restart_on_failure,
            restart_delay=restart_delay,
            max_restarts=max_restarts
        )
        logger.debug(f"Registered task: {name} (priority={priority.name})")
    
    def on_shutdown(self, callback: Callable):
        """Register shutdown callback"""
        self._on_shutdown_callbacks.append(callback)
    
    def on_startup(self, callback: Callable):
        """Register startup callback"""
        self._on_startup_callbacks.append(callback)
    
    async def start(self):
        """Start the event loop and all tasks"""
        if self._running:
            logger.warning("Event loop already running")
            return
        
        self._running = True
        self._shutdown_event. clear()
        
        logger.info("🚀 BotEventLoop starting...")
        
        # Setup signal handlers
        self._setup_signal_handlers()
        
        # Run startup callbacks
        for callback in self._on_startup_callbacks:
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Startup callback error: {e}")
        
        # Start all registered tasks by priority
        sorted_tasks = sorted(
            self._tasks. values(),
            key=lambda t: t.priority. value
        )
        
        for managed_task in sorted_tasks:
            if managed_task.enabled:
                await self._start_task(managed_task)
        
        logger.info(f"✅ Started {len(self._tasks)} tasks")
        
        # Main supervision loop
        try:
            await self._supervision_loop()
        except asyncio.CancelledError:
            logger.info("Event loop cancelled")
        finally:
            await self._shutdown()
    
    async def stop(self):
        """Request graceful shutdown"""
        logger.info("🛑 Shutdown requested...")
        self._running = False
        self._shutdown_event. set()
    
    def request_shutdown(self):
        """Non-async shutdown request (for signal handlers)"""
        self._running = False
        self._shutdown_event. set()
    
    def _setup_signal_handlers(self):
        """Setup OS signal handlers"""
        loop = asyncio.get_running_loop()
        
        for sig in (signal. SIGINT, signal.SIGTERM):
            try:
                loop. add_signal_handler(
                    sig,
                    self. request_shutdown
                )
                logger.debug(f"Registered handler for {sig. name}")
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: self.request_shutdown())
    
    async def _start_task(self, managed_task: ManagedTask):
        """Start a single managed task"""
        try:
            coro = managed_task.coro_factory()
            managed_task.task = asyncio.create_task(
                coro,
                name=managed_task.name
            )
            managed_task.task. add_done_callback(
                lambda t, mt=managed_task: self._task_done_callback(mt, t)
            )
            logger.info(f"▶️ Started task: {managed_task.name}")
        except Exception as e:
            logger.error(f"Failed to start task {managed_task. name}: {e}")
    
    def _task_done_callback(self, managed_task: ManagedTask, task: asyncio.Task):
        """Handle task completion/failure"""
        if not self._running:
            return
        
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.info(f"Task {managed_task.name} was cancelled")
            return
        
        if exc:
            logger.error(
                f"❌ Task {managed_task.name} crashed: {exc}",
                exc_info=exc
            )
            
            # Notify via Telegram if available
            if self. telegram_bot and hasattr(self.telegram_bot, 'send_error'):
                asyncio.create_task(
                    self.telegram_bot. send_error(
                        f"Task Crash: {managed_task.name}\n{exc}"
                    )
                )
            
            # Schedule restart if enabled
            if managed_task.restart_on_failure:
                if managed_task.restart_count < managed_task.max_restarts:
                    asyncio.create_task(
                        self._restart_task(managed_task)
                    )
                else:
                    logger.critical(
                        f"Task {managed_task.name} exceeded max restarts "
                        f"({managed_task.max_restarts})"
                    )
    
    async def _restart_task(self, managed_task: ManagedTask):
        """Restart a failed task with delay"""
        managed_task.restart_count += 1
        delay = managed_task. restart_delay * managed_task.restart_count
        
        logger.info(
            f"🔄 Restarting {managed_task.name} in {delay:. 1f}s "
            f"(attempt {managed_task.restart_count}/{managed_task.max_restarts})"
        )
        
        await asyncio.sleep(delay)
        
        if self._running and managed_task.enabled:
            managed_task.last_restart = time.time()
            await self._start_task(managed_task)
    
    async def _supervision_loop(self):
        """Main loop that monitors tasks and handles shutdown"""
        health_interval = 30.0
        last_health_check = 0.0
        
        while self._running:
            try:
                # Wait for shutdown or health check interval
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=health_interval
                    )
                    # Shutdown requested
                    break
                except asyncio. TimeoutError:
                    pass
                
                # Periodic health check
                now = time.time()
                if now - last_health_check >= health_interval:
                    await self._health_check()
                    last_health_check = now
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervision loop error: {e}")
                await asyncio.sleep(1.0)
    
    async def _health_check(self):
        """Check health of all tasks"""
        alive = 0
        dead = 0
        
        for name, managed_task in self._tasks.items():
            if managed_task.task:
                if managed_task.task.done():
                    dead += 1
                else:
                    alive += 1
        
        logger.debug(f"📊 Task health: {alive} alive, {dead} dead")
        
        # Check WebSocket health
        if self.ws_manager and hasattr(self.ws_manager, 'is_healthy'):
            if not self.ws_manager.is_healthy():
                logger.warning("⚠️ WebSocket connections unhealthy")
    
    async def _shutdown(self):
        """Graceful shutdown procedure"""
        logger. info("🛑 Initiating shutdown...")
        self._running = False
        
        # FIRST: Close all trades if enabled
        await self._close_all_trades_on_shutdown()
        
        # Run shutdown callbacks
        for callback in self._on_shutdown_callbacks:
            try:
                result = callback()
                if asyncio. iscoroutine(result):
                    await asyncio.wait_for(result, timeout=5.0)
            except Exception as e:
                logger.error(f"Shutdown callback error: {e}")
        
        # Cancel all tasks in reverse priority order
        sorted_tasks = sorted(
            self._tasks. values(),
            key=lambda t: t.priority.value,
            reverse=True
        )
        
        for managed_task in sorted_tasks:
            if managed_task.task and not managed_task. task.done():
                managed_task. task.cancel()
                logger.debug(f"Cancelled task: {managed_task.name}")
        
        # Wait for tasks to complete
        tasks = [mt.task for mt in self._tasks.values() if mt.task]
        if tasks:
            done, pending = await asyncio. wait(
                tasks,
                timeout=self._shutdown_timeout
            )
            
            if pending:
                logger.warning(f"Force-killing {len(pending)} tasks")
                for task in pending:
                    task.cancel()
        
        # Stop components
        await self._stop_components()
        
        logger.info("✅ Shutdown complete")
    
    async def _close_all_trades_on_shutdown(self):
        """
        Close all open positions on both exchanges before shutdown.
        Only runs if CLOSE_ALL_ON_SHUTDOWN is True in config.
        """
        import config
        
        if not getattr(config, 'CLOSE_ALL_ON_SHUTDOWN', False):
            logger.info("🔓 SHUTDOWN: Keeping trades open (CLOSE_ALL_ON_SHUTDOWN=False)")
            return
        
        if not self.x10_adapter or not self.lighter_adapter:
            logger.warning("🔓 SHUTDOWN: Adapters not available, skipping close-all")
            return
        
        logger.info("🔒 SHUTDOWN: Closing all open trades...")
        timeout = getattr(config, 'SHUTDOWN_CLOSE_TIMEOUT', 60)
        
        try:
            closed_count = 0
            failed_count = 0
            
            # Fetch positions from both exchanges
            x10_positions = await asyncio.wait_for(
                self.x10_adapter.fetch_open_positions(),
                timeout=10.0
            )
            lighter_positions = await asyncio.wait_for(
                self.lighter_adapter.fetch_open_positions(),
                timeout=10.0
            )
            
            total_positions = len(x10_positions or []) + len(lighter_positions or [])
            
            if total_positions == 0:
                logger.info("✅ SHUTDOWN: No open positions to close")
                return
            
            logger.info(f"📊 SHUTDOWN: Found {len(x10_positions or [])} X10 + {len(lighter_positions or [])} Lighter positions")
            
            # Close X10 positions
            for pos in (x10_positions or []):
                symbol = pos.get('symbol')
                size = pos.get('size', 0)
                
                if abs(size) < 1e-8:
                    continue
                
                try:
                    # Determine close side
                    close_side = "SELL" if size > 0 else "BUY"
                    price = self.x10_adapter.fetch_mark_price(symbol) or 0
                    notional = abs(size) * price
                    
                    logger.info(f"🔻 SHUTDOWN CLOSE X10: {symbol} (size={size:.6f})")
                    
                    success, order_id = await asyncio.wait_for(
                        self.x10_adapter.close_live_position(symbol, close_side, notional),
                        timeout=15.0
                    )
                    
                    if success:
                        closed_count += 1
                        logger.info(f"✅ SHUTDOWN: X10 {symbol} closed")
                    else:
                        failed_count += 1
                        logger.warning(f"⚠️ SHUTDOWN: X10 {symbol} close failed")
                        
                except asyncio.TimeoutError:
                    failed_count += 1
                    logger.warning(f"⚠️ SHUTDOWN: X10 {symbol} close timed out")
                except Exception as e:
                    failed_count += 1
                    logger.error(f"❌ SHUTDOWN: X10 {symbol} close error: {e}")
            
            # Close Lighter positions
            for pos in (lighter_positions or []):
                symbol = pos.get('symbol')
                size = pos.get('size', 0)
                
                if abs(size) < 1e-8:
                    continue
                
                try:
                    close_side = "SELL" if size > 0 else "BUY"
                    price = self.lighter_adapter.fetch_mark_price(symbol) or 0
                    notional = abs(size) * price
                    
                    logger.info(f"🔻 SHUTDOWN CLOSE LIGHTER: {symbol} (size={size:.6f})")
                    
                    success, order_id = await asyncio.wait_for(
                        self.lighter_adapter.close_live_position(symbol, close_side, notional),
                        timeout=15.0
                    )
                    
                    if success:
                        closed_count += 1
                        logger.info(f"✅ SHUTDOWN: Lighter {symbol} closed")
                    else:
                        failed_count += 1
                        logger.warning(f"⚠️ SHUTDOWN: Lighter {symbol} close failed")
                        
                except asyncio.TimeoutError:
                    failed_count += 1
                    logger.warning(f"⚠️ SHUTDOWN: Lighter {symbol} close timed out")
                except Exception as e:
                    failed_count += 1
                    logger.error(f"❌ SHUTDOWN: Lighter {symbol} close error: {e}")
            
            logger.info(f"🔒 SHUTDOWN COMPLETE: {closed_count} closed, {failed_count} failed")
            
        except asyncio.TimeoutError:
            logger.error("❌ SHUTDOWN: Close-all operation timed out")
        except Exception as e:
            logger.error(f"❌ SHUTDOWN: Close-all error: {e}")
    
    async def _stop_components(self):
        """Stop all bot components"""
        # WebSocket Manager
        if self. ws_manager and hasattr(self. ws_manager, 'stop'):
            try:
                await asyncio.wait_for(self.ws_manager.stop(), timeout=5.0)
            except Exception as e:
                logger.error(f"WebSocket stop error: {e}")
        
        # OI Tracker
        if self.oi_tracker and hasattr(self.oi_tracker, 'stop'):
            try:
                await asyncio.wait_for(self.oi_tracker.stop(), timeout=5.0)
            except Exception as e:
                logger.error(f"OI Tracker stop error: {e}")
        
        # Parallel Execution Manager
        if self. parallel_exec and hasattr(self.parallel_exec, 'stop'):
            try:
                await asyncio.wait_for(self.parallel_exec.stop(), timeout=5.0)
            except Exception as e:
                logger.error(f"Parallel exec stop error: {e}")
        
        # State Manager
        if self.state_manager and hasattr(self.state_manager, 'stop'):
            try:
                await asyncio. wait_for(self.state_manager. stop(), timeout=5.0)
            except Exception as e:
                logger.error(f"State manager stop error: {e}")
        
        # Telegram Bot
        if self.telegram_bot and hasattr(self.telegram_bot, 'stop'):
            try:
                await self.telegram_bot. send_message("🛑 Bot shutting down")
                await asyncio.wait_for(self.telegram_bot.stop(), timeout=5.0)
            except Exception as e:
                logger.debug(f"Telegram stop error: {e}")
    
    def get_task_status(self) -> Dict[str, Dict]:
        """Get status of all managed tasks"""
        status = {}
        for name, mt in self._tasks. items():
            task_status = "unknown"
            if mt.task:
                if mt.task.done():
                    task_status = "stopped"
                elif mt.task.cancelled():
                    task_status = "cancelled"
                else:
                    task_status = "running"
            else:
                task_status = "not_started"
            
            status[name] = {
                "status": task_status,
                "priority": mt.priority. name,
                "restart_count": mt.restart_count,
                "enabled": mt.enabled
            }
        return status
    
    def is_running(self) -> bool:
        return self._running


# ═══════════════════════════════════════════════════════════════
# SINGLETON & FACTORY
# ═══════════════════════════════════════════════════════════════
_event_loop: Optional[BotEventLoop] = None


def get_event_loop() -> BotEventLoop:
    """Get or create singleton event loop manager"""
    global _event_loop
    if _event_loop is None:
        _event_loop = BotEventLoop()
    return _event_loop


async def run_bot(
    x10_adapter,
    lighter_adapter,
    logic_loop_factory: Callable,
    farm_loop_factory: Callable,
    trade_management_factory: Callable,
    maintenance_factory: Callable,
    **kwargs
):
    """
    Main entry point to run the bot. 
    
    >>> SYSTEM BEREIT: BOT JETZT STARTEN <<<
    """
    from src.parallel_execution import ParallelExecutionManager
    from src.websocket_manager import get_websocket_manager, init_websocket_manager
    from src.open_interest_tracker import get_oi_tracker, init_oi_tracker
    from src. state_manager import get_state_manager
    
    loop = get_event_loop()
    
    # Initialize components
    parallel_exec = ParallelExecutionManager(x10_adapter, lighter_adapter)
    await parallel_exec.start()
    
    ws_manager = await init_websocket_manager(x10_adapter, lighter_adapter)
    oi_tracker = await init_oi_tracker(x10_adapter, lighter_adapter)
    state_manager = get_state_manager()
    
    # Set components
    loop.set_components(
        x10_adapter=x10_adapter,
        lighter_adapter=lighter_adapter,
        parallel_exec=parallel_exec,
        ws_manager=ws_manager,
        oi_tracker=oi_tracker,
        state_manager=state_manager,
        telegram_bot=kwargs.get('telegram_bot')
    )
    
    # Register tasks
    loop.register_task(
        "logic_loop",
        lambda: logic_loop_factory(lighter_adapter, x10_adapter, parallel_exec),
        priority=TaskPriority.NORMAL,
        restart_on_failure=True
    )
    
    loop.register_task(
        "farm_loop",
        lambda: farm_loop_factory(lighter_adapter, x10_adapter, parallel_exec),
        priority=TaskPriority.LOW,
        restart_on_failure=True
    )
    
    loop.register_task(
        "trade_management",
        lambda: trade_management_factory(lighter_adapter, x10_adapter),
        priority=TaskPriority.HIGH,
        restart_on_failure=True
    )
    
    loop.register_task(
        "maintenance",
        lambda: maintenance_factory(lighter_adapter, x10_adapter),
        priority=TaskPriority.LOW,
        restart_on_failure=True
    )
    
    # Start
    await loop.start()
    
    print("\n>>> SYSTEM BEREIT: BOT JETZT STARTEN <<<\n")