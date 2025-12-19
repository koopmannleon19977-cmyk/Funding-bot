# src/event_loop. py - PUNKT 10: EVENT-LOOP UMBAU

import asyncio
import signal
import logging
import traceback
from typing import Optional, List, Callable, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import time

from src.shutdown import get_shutdown_orchestrator

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
        # NOTE: This timeout is for cancelling *background tasks*.
        # The centralized ShutdownOrchestrator has its own longer timeout for closing positions safely.
        self._shutdown_timeout = 15.0
        self._shutdown_reason: str = ""
        
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
        
        # Keep centralized shutdown orchestrator in sync
        try:
            get_shutdown_orchestrator().configure(**components)
        except Exception:
            pass
    
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
        # Reset global shutdown latch
        try:
            import config
            setattr(config, "IS_SHUTTING_DOWN", False)
        except Exception:
            pass
        
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
            if not self._shutdown_reason:
                self._shutdown_reason = "cancelled"
            logger.info("Event loop cancelled")
        finally:
            await self._shutdown()
    
    async def stop(self):
        """Request graceful shutdown"""
        logger.info("🛑 Shutdown requested...")
        if not self._shutdown_reason:
            self._shutdown_reason = "manual_stop"
        self._running = False
        self._shutdown_event. set()
    
    def request_shutdown(self):
        """Non-async shutdown request (for signal handlers)"""
        if not self._shutdown_reason:
            self._shutdown_reason = "request_shutdown"
        self._running = False
        self._shutdown_event. set()
    
    def _setup_signal_handlers(self):
        """Setup OS signal handlers - Disabled for Windows compatibility"""
        # We rely on the Main Loop to catch KeyboardInterrupt
        pass
    
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
            if not self._shutdown_reason:
                self._shutdown_reason = f"task_crash:{managed_task.name}"
            
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
        """Graceful shutdown procedure.

        Critical ordering:
        - Block new trading immediately
        - Run ShutdownOrchestrator while WebSockets / adapters are still alive (so fills can be observed)
        - Only then cancel remaining background tasks
        """
        reason = self._shutdown_reason or "bot_event_loop"
        logger.info(f"🛑 Initiating shutdown... (reason={reason})")
        self._running = False
        self._shutdown_event.set()

        # Prevent supervisor from restarting tasks while we're shutting down
        for mt in self._tasks.values():
            mt.enabled = False

        # Set global shutdown latch to block new orders IMMEDIATELY
        try:
            import config
            setattr(config, "IS_SHUTTING_DOWN", True)
        except Exception:
            pass

        # Stop new executions ASAP (but do NOT cancel/kill components yet; orchestrator needs them)
        try:
            if self.parallel_exec is not None:
                setattr(self.parallel_exec, "is_running", False)
        except Exception:
            pass

        # Run shutdown callbacks early (best-effort)
        for callback in self._on_shutdown_callbacks:
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=5.0)
            except Exception as e:
                logger.error(f"Shutdown callback error: {e}")

        # Delegate to centralized orchestrator
        shutdown = get_shutdown_orchestrator()
        shutdown.configure(
            ws_manager=self.ws_manager,
            state_manager=self.state_manager,
            parallel_exec=self.parallel_exec,
            telegram_bot=self.telegram_bot,
            lighter=self.lighter_adapter,
            x10=self.x10_adapter,
        )

        # Let the orchestrator do position closes + persistence before we tear down tasks.
        try:
            await asyncio.shield(shutdown.shutdown(reason=reason))
        except Exception as e:
            logger.error(f"Shutdown orchestrator error: {e}", exc_info=True)

        # Cancel all remaining tasks in reverse priority order
        current = asyncio.current_task()
        sorted_tasks = sorted(
            self._tasks.values(),
            key=lambda t: t.priority.value,
            reverse=True
        )
        for managed_task in sorted_tasks:
            task = managed_task.task
            if not task or task.done() or task is current:
                continue
            task.cancel()
            logger.debug(f"Cancelled task: {managed_task.name}")

        # Wait for tasks to complete
        tasks = [mt.task for mt in self._tasks.values() if mt.task and mt.task is not current]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)

            # Retrieve exceptions from done tasks to prevent "never retrieved"
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Task {task.get_name()} had exception: {e}")

            if pending:
                pending_names = []
                for task in pending:
                    try:
                        pending_names.append(task.get_name())
                    except Exception:
                        pending_names.append("<unnamed>")
                logger.warning(f"Force-killing {len(pending)} tasks after orchestrator: {pending_names}")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

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
        
        logger.info("🔒 SHUTDOWN: Closing all open trades... (Please wait!)")
        
        # WICHTIG: Erhöhe Timeout für Lighter
        # Lighter braucht Zeit für Nonce, Signatur und API-Roundtrip
        shutdown_timeout = 30.0 
        
        try:
            # Wir nutzen hier direkt den ParallelExecutionManager, falls verfügbar
            if self.parallel_exec:  # Fix: Attribute name is self.parallel_exec not self.parallel_execution_manager
                # 1. Stoppe erst neue Trades
                self.parallel_exec.is_running = False
            
            # 2. Hole alle offenen Positionen
            # Safe access to adapters in case they are None
            if not self.lighter_adapter or not self.x10_adapter:
                logger.warning("Adapters missing, skipping close")
                return

            lighter_pos = await self.lighter_adapter.fetch_open_positions()
            x10_pos = await self.x10_adapter.fetch_open_positions()
            
            tasks = []
            
            # Lighter Positionen schließen (Priorität!)
            for pos in (lighter_pos or []):
                symbol = pos['symbol']
                size = float(pos['size'])
                if abs(size) < 1e-8: continue

                # WICHTIG: Nutze Market Orders oder aggressive Limit Orders
                logger.info(f"🛑 SHUTDOWN: Closing Lighter {symbol} ({size})...")
                
                # Check for specialized close method or use default
                # close_live_position usually takes (symbol, side_to_open, size_usd_or_tokens)
                # But here we assume the adapter handles "close" logic correctly
                # We simply call close_live_position with the CURRENT side so it reverses it?
                # No, close_live_position usually takes the side of the EXISTING position to close it.
                # Let's check LighterAdapter signature: open_live_position(symbol, side, ...)
                # LighterAdapter has close_live_position(symbol, position_side, size_usd)
                
                price = self.lighter_adapter.fetch_mark_price(symbol) or 0
                notional = abs(size) * price
                
                side_of_position = "BUY" if size > 0 else "SELL"
                
                tasks.append(
                    self.lighter_adapter.close_live_position(
                        symbol, 
                        side_of_position,
                        notional 
                    )
                )

            # X10 Positionen schließen
            for pos in (x10_pos or []):
                symbol = pos['symbol']
                size = float(pos['size'])
                if abs(size) < 1e-8: continue

                price = self.x10_adapter.fetch_mark_price(symbol) or 0
                notional = abs(size) * price
                side_of_position = "BUY" if size > 0 else "SELL"
                
                logger.info(f"🛑 SHUTDOWN: Closing X10 {symbol} ({size})...")
                tasks.append(
                    self.x10_adapter.close_live_position(
                        symbol, 
                        side_of_position,
                        notional
                    )
                )

            if not tasks:
                logger.info("✅ SHUTDOWN: No positions found.")
                return

            # 3. Warten bis ALLE fertig sind - Fehler abfangen!
            logger.info(f"⏳ Waiting up to {shutdown_timeout}s for {len(tasks)} close orders...")
            try:
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=shutdown_timeout)
                
                # ═══════════════════════════════════════════════════════════════
                # FIX (2025-12-18): MUST consume all results to prevent
                # "_GatheringFuture exception was never retrieved" error
                # ═══════════════════════════════════════════════════════════════
                for i, res in enumerate(results):
                    if isinstance(res, Exception):
                        if isinstance(res, asyncio.CancelledError):
                            logger.debug(f"Shutdown close task {i} cancelled (expected)")
                        else:
                            logger.error(f"❌ Shutdown Close Error (Task {i}): {res}")
                    else:
                        logger.info(f"✅ Position closed (Task {i}).")
            except asyncio.CancelledError:
                logger.debug("Shutdown close tasks cancelled")
                # Cancel any pending tasks and consume their exceptions
                for task in tasks:
                    if hasattr(task, 'cancel'):
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass

        except asyncio.CancelledError:
            logger.debug("_close_all_trades_on_shutdown cancelled")
        except asyncio.TimeoutError:
            logger.error("❌ SHUTDOWN: Timed out while closing positions!")
        except Exception as e:
            logger.error(f"❌ SHUTDOWN: Error closing trades: {e}")
    
    async def _stop_components(self):
        """Stop all bot components"""
        # WebSocket Manager
        if self. ws_manager and hasattr(self. ws_manager, 'stop'):
            try:
                await asyncio.wait_for(self.ws_manager.stop(), timeout=5.0)
            except Exception as e:
                logger.error(
                    f"WebSocket stop error: {e.__class__.__name__}: {e}",
                    exc_info=True
                )
        
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
