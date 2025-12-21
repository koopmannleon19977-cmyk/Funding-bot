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
        self._signal_handlers_installed = False
        self._prev_signal_handlers: Dict[int, Any] = {}
        
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
        # #region agent log
        import json, time
        DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H2", "location": "event_loop.py:112", "message": "start() entry", "data": {"_running": self._running, "shutdown_event_set": self._shutdown_event.is_set()}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        if self._running:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H2", "location": "event_loop.py:115", "message": "start() early return - already running", "data": {}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
            logger.warning("Event loop already running")
            return
        
        # FIX: Reset state completely before starting
        # This ensures that if the singleton is reused from a previous run,
        # it starts in a clean state
        self._running = False
        self._shutdown_event.clear()
        self._shutdown_reason = ""
        
        # Now set running to True
        self._running = True
        # Reset global shutdown latch
        try:
            import config
            setattr(config, "IS_SHUTTING_DOWN", False)
        except Exception:
            pass
        
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H2", "location": "event_loop.py:127", "message": "About to log BotEventLoop starting", "data": {"_running": self._running, "tasks_count": len(self._tasks)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
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
        
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H3", "location": "event_loop.py:147", "message": "Before starting tasks", "data": {"tasks_count": len(sorted_tasks), "enabled_tasks": sum(1 for t in sorted_tasks if t.enabled)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        for managed_task in sorted_tasks:
            if managed_task.enabled:
                await self._start_task(managed_task)
        
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H3", "location": "event_loop.py:156", "message": "After starting tasks", "data": {"tasks_count": len(self._tasks)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        logger.info(f"✅ Started {len(self._tasks)} tasks")
        
        # FIX: Double-check that _running is still True before entering supervision loop
        if not self._running:
            logger.error(f"❌ CRITICAL: _running became False after starting tasks! Re-setting to True.")
            self._running = True
            self._shutdown_event.clear()
            self._shutdown_reason = ""
        
        logger.info(f"🔍 Pre-supervision check: _running={self._running}, _shutdown_event={self._shutdown_event.is_set()}, _shutdown_reason='{self._shutdown_reason}', tasks={len(self._tasks)}")
        
        # Main supervision loop
        try:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "event_loop.py:154", "message": "Before _supervision_loop", "data": {"_running": self._running, "shutdown_event_set": self._shutdown_event.is_set(), "tasks_count": len(self._tasks)}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
            await self._supervision_loop()
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "event_loop.py:156", "message": "After _supervision_loop", "data": {"_running": self._running, "shutdown_reason": self._shutdown_reason}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
        except asyncio.CancelledError as e:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H5", "location": "event_loop.py:170", "message": "CancelledError in supervision loop", "data": {"error": str(e)}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
            if not self._shutdown_reason:
                self._shutdown_reason = "cancelled"
            logger.info("Event loop cancelled")
        except Exception as e:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H5", "location": "event_loop.py:178", "message": "Exception in supervision loop", "data": {"error": str(e), "error_type": type(e).__name__}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
            logger.error(f"❌ Exception in supervision loop: {e}", exc_info=True)
            raise
        finally:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H6", "location": "event_loop.py:186", "message": "Before _shutdown()", "data": {"_running": self._running, "shutdown_reason": self._shutdown_reason}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
            await self._shutdown()
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H6", "location": "event_loop.py:192", "message": "After _shutdown()", "data": {}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
    
    async def stop(self):
        """Request graceful shutdown"""
        logger.info(f"🛑 stop() called - current _running={self._running}, _shutdown_reason='{self._shutdown_reason}'")
        if not self._shutdown_reason:
            self._shutdown_reason = "manual_stop"
        self._running = False
        self._shutdown_event.set()
        logger.info(f"🛑 stop() completed - _running={self._running}, _shutdown_event={self._shutdown_event.is_set()}")
    
    def request_shutdown(self):
        """Non-async shutdown request (for signal handlers)"""
        logger.info(f"🛑 request_shutdown() called - current _running={self._running}, _shutdown_reason='{self._shutdown_reason}'")
        if not self._shutdown_reason:
            self._shutdown_reason = "request_shutdown"
        self._running = False
        self._shutdown_event.set()
        logger.info(f"🛑 request_shutdown() completed - _running={self._running}, _shutdown_event={self._shutdown_event.is_set()}")
    
    def _setup_signal_handlers(self):
        """Setup OS signal handlers (SIGINT, SIGTERM) for graceful shutdown."""
        if self._signal_handlers_installed:
            return

        self._signal_handlers_installed = True

        def _handle_signal(signum, frame):
            try:
                sig_name = signal.Signals(signum).name
            except Exception:
                sig_name = str(signum)
            logger.info(f"[signal] Received {sig_name}; requesting shutdown")
            self.request_shutdown()
            prev_handler = self._prev_signal_handlers.get(signum)
            if callable(prev_handler):
                try:
                    prev_handler(signum, frame)
                except Exception as e:
                    logger.debug(f"[signal] Previous handler error: {e}")

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                self._prev_signal_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_signal)
            except Exception as e:
                logger.debug(f"[signal] handler setup failed for {sig}: {e}")
    
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
        # #region agent log
        import json, time
        DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H", "location": "event_loop.py:198", "message": "_task_done_callback", "data": {"task_name": managed_task.name, "_running": self._running, "task_done": task.done()}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        if not self._running:
            return
        
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.info(f"Task {managed_task.name} was cancelled")
            return
        
        if exc:
            # #region agent log
            try:
                with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H", "location": "event_loop.py:209", "message": "Task exception detected", "data": {"task_name": managed_task.name, "error": str(exc), "error_type": type(exc).__name__}, "timestamp": int(time.time() * 1000)}) + "\n")
            except: pass
            # #endregion
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
        health_interval = 10.0  # Reduced from 30s to 10s for faster error detection
        last_health_check = 0.0
        
        # #region agent log
        import json
        DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "event_loop.py:258", "message": "_supervision_loop entry", "data": {"_running": self._running, "shutdown_event_set": self._shutdown_event.is_set()}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        # FIX: Ensure shutdown event is cleared at the start of supervision loop
        # This prevents immediate exit if the event was set from a previous run
        if self._shutdown_event.is_set():
            logger.warning("⚠️ Shutdown event was set at start of supervision loop - clearing it")
            self._shutdown_event.clear()
        
        logger.info(f"🔄 Supervision loop starting (running={self._running}, shutdown_event={self._shutdown_event.is_set()})")
        
        # FIX: Ensure _running is True before entering the loop
        if not self._running:
            logger.error(f"❌ CRITICAL: _running is False at start of supervision loop! This should never happen.")
            logger.error(f"   _shutdown_reason: {self._shutdown_reason}")
            logger.error(f"   _shutdown_event.is_set(): {self._shutdown_event.is_set()}")
            # Force it to True to keep the bot running
            self._running = True
            logger.warning("⚠️ Forced _running to True to prevent immediate exit")
        
        loop_iterations = 0
        while self._running:
            loop_iterations += 1
            if loop_iterations == 1:
                logger.info(f"✅ Supervision loop entered successfully (iteration {loop_iterations})")
            elif loop_iterations % 10 == 0:
                logger.debug(f"🔄 Supervision loop iteration {loop_iterations} (running={self._running})")
            
            # Double-check _running at start of each iteration
            if not self._running:
                logger.warning(f"⚠️ _running became False during loop (iteration {loop_iterations}) - breaking")
                break
                
            try:
                # Wait for shutdown or health check interval
                try:
                    # #region agent log
                    try:
                        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "event_loop.py:263", "message": "Before wait_for shutdown_event", "data": {"_running": self._running, "shutdown_event_set": self._shutdown_event.is_set(), "timeout": health_interval}, "timestamp": int(time.time() * 1000)}) + "\n")
                    except: pass
                    # #endregion
                    
                    # CRITICAL: Check state BEFORE wait() to prevent immediate return
                    event_before_wait = self._shutdown_event.is_set()
                    running_before_wait = self._running
                    reason_before_wait = self._shutdown_reason
                    
                    logger.info(f"🔍 [Iteration {loop_iterations}] Before wait(): _running={running_before_wait}, event_set={event_before_wait}, reason='{reason_before_wait}'")
                    
                    # If event is already set, we need to handle it BEFORE wait()
                    if event_before_wait:
                        logger.warning(f"⚠️ [Iteration {loop_iterations}] Shutdown event is ALREADY SET before wait()!")
                        # Only break if we're actually shutting down
                        if not running_before_wait or reason_before_wait:
                            logger.info(f"🛑 [Iteration {loop_iterations}] Breaking: _running={running_before_wait}, reason='{reason_before_wait}'")
                            break
                        else:
                            # Event set but still running - clear it and continue
                            logger.warning(f"⚠️ [Iteration {loop_iterations}] Clearing unexpected shutdown event and continuing")
                            self._shutdown_event.clear()
                            # Skip wait() and continue loop
                            await asyncio.sleep(0.1)  # Small delay to prevent tight loop
                            continue
                    
                    # Only wait if event is not already set
                    try:
                        logger.info(f"⏳ [Iteration {loop_iterations}] Waiting for shutdown event (timeout={health_interval}s)...")
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=health_interval
                        )
                        logger.info(f"✅ [Iteration {loop_iterations}] wait_for() returned (event was set or timeout)")
                    except asyncio.TimeoutError:
                        # Normal timeout - continue loop
                        logger.debug(f"⏱️ [Iteration {loop_iterations}] wait_for() timeout (normal - continuing)")
                        pass
                    except asyncio.CancelledError:
                        # If we are still running, treat this as a spurious cancellation.
                        if self._running and not self._shutdown_reason:
                            logger.debug(
                                f"[Iteration {loop_iterations}] wait_for cancelled while running; continuing"
                            )
                            await asyncio.sleep(0.1)
                            continue
                        logger.info(
                            f"[Iteration {loop_iterations}] Cancelled and shutting down - breaking"
                        )
                        raise
                    
                    # Check state AFTER wait()
                    event_after_wait = self._shutdown_event.is_set()
                    running_after_wait = self._running
                    reason_after_wait = self._shutdown_reason
                    
                    logger.info(f"🔍 [Iteration {loop_iterations}] After wait(): _running={running_after_wait}, event_set={event_after_wait}, reason='{reason_after_wait}'")
                    
                    # CRITICAL FIX: Check _running FIRST before breaking
                    # If _running is False, we MUST break (shutdown was requested)
                    if not running_after_wait:
                        logger.info(f"🛑 [Iteration {loop_iterations}] Breaking supervision loop: _running={running_after_wait} (shutdown requested)")
                        break
                    
                    # If shutdown_reason is set, we MUST break (shutdown was requested)
                    if reason_after_wait:
                        logger.info(f"🛑 [Iteration {loop_iterations}] Breaking supervision loop: _shutdown_reason='{reason_after_wait}' (shutdown requested)")
                        break
                    
                    # If event was set but we're still running and no shutdown reason, clear it and continue
                    # This handles the case where the event was set by mistake or from a previous run
                    if event_after_wait:
                        logger.warning(f"⚠️ [Iteration {loop_iterations}] Shutdown event set but bot still running - clearing event and continuing")
                        logger.warning(f"   _running={running_after_wait}, _shutdown_reason='{reason_after_wait}'")
                        self._shutdown_event.clear()
                        # Continue the loop - don't break!
                        continue
                        
                except asyncio.TimeoutError:
                    # This should not happen here, but handle it anyway
                    logger.debug(f"⏱️ [Iteration {loop_iterations}] Outer TimeoutError (unexpected)")
                    pass
                
                # Periodic health check (non-blocking)
                now = time.time()
                if now - last_health_check >= health_interval:
                    logger.info(f"🏥 [Iteration {loop_iterations}] Running health check...")
                    # Run health check in background to avoid blocking
                    asyncio.create_task(self._health_check())
                    last_health_check = now
                    logger.debug(f"✅ [Iteration {loop_iterations}] Health check scheduled (non-blocking)")
                
            except asyncio.CancelledError:
                # #region agent log
                try:
                    with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "E", "location": "event_loop.py:277", "message": "CancelledError in supervision loop", "data": {"_running": self._running}, "timestamp": int(time.time() * 1000)}) + "\n")
                except: pass
                # #endregion
                # CRITICAL FIX: Only break if we're actually shutting down
                # If we're cancelled but still supposed to be running, continue the loop
                if self._running and not self._shutdown_reason:
                    logger.warning(f"⚠️ [Iteration {loop_iterations}] CancelledError but _running=True - this is unexpected!")
                    logger.warning(f"   Attempting to continue the loop...")
                    # Try to continue - don't break
                    # But first, check if we can actually continue
                    # If the task itself is cancelled, we can't continue
                    try:
                        # Create a new task to continue the loop
                        # Actually, we can't do that - the task is already cancelled
                        # So we need to break, but log it as unexpected
                        logger.error(f"❌ [Iteration {loop_iterations}] Cannot continue after CancelledError - task is cancelled")
                        logger.error(f"   This indicates the supervision loop task was cancelled externally")
                        logger.error(f"   _running={self._running}, _shutdown_reason='{self._shutdown_reason}'")
                        break
                    except Exception as e:
                        logger.error(f"❌ Error trying to continue after CancelledError: {e}")
                        break
                else:
                    logger.warning(f"⚠️ [Iteration {loop_iterations}] CancelledError in supervision loop - breaking (shutdown requested)")
                    break
            except Exception as e:
                # #region agent log
                try:
                    with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "F", "location": "event_loop.py:279", "message": "Exception in supervision loop", "data": {"error": str(e), "error_type": type(e).__name__}, "timestamp": int(time.time() * 1000)}) + "\n")
                except: pass
                # #endregion
                logger.error(f"❌ [Iteration {loop_iterations}] Supervision loop error: {e}", exc_info=True)
                # Don't break the loop on exception - continue running
                await asyncio.sleep(1.0)
        
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "G", "location": "event_loop.py:281", "message": "_supervision_loop exit", "data": {"_running": self._running, "shutdown_reason": self._shutdown_reason, "loop_iterations": loop_iterations}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        
        logger.warning(f"⚠️ Supervision loop exited! _running={self._running}, _shutdown_reason='{self._shutdown_reason}', iterations={loop_iterations}")
    
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


def reset_event_loop():
    """Reset the singleton event loop (useful for testing or clean restarts)"""
    global _event_loop
    _event_loop = None


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
