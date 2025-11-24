# src/websocket_manager.py
import asyncio
import logging
from typing import List, Any

logger = logging.getLogger(__name__)

class WebSocketManager:
    """
    Centralized WebSocket Supervisor (Roadmap #9).
    Manages connection lifecycle, health checks, and automatic reconnections
    for all exchange adapters parallel to the main logic loop.
    """
    
    def __init__(self, adapters: List[Any]):
        self.adapters = adapters
        self.tasks = []
        self.running = False

    async def start(self):
        """Spawns supervisor tasks for all adapters"""
        self.running = True
        logger.info(f"WebSocket Manager: Starting streams for {len(self.adapters)} adapters...")
        
        for adapter in self.adapters:
            # Check if method exists AND is callable
            if hasattr(adapter, 'start_websocket') and callable(getattr(adapter, 'start_websocket', None)):
                # Wrap adapter's WS loop in a supervisor task
                task = asyncio.create_task(self._supervisor(adapter))
                self.tasks.append(task)
                logger.info(f"✅ WebSocket Manager: Started stream for {adapter.name}")
            else:
                logger.warning(f"⚠️ Adapter {adapter.name} has no start_websocket() method.")
        
        logger.info(f"WebSocket Manager: {len(self.tasks)} streams initialized.")

    async def _supervisor(self, adapter):
        """
        Monitors a single adapter's WebSocket connection.
        If the adapter's start_websocket() returns or raises, this restarts it.
        """
        while self.running:
            try:
                # The adapter's start_websocket should implement its own retry logic for 
                # connection drops, but if it crashes completely, we catch it here.
                await adapter.start_websocket()
            except asyncio.CancelledError:
                logger.info(f"WebSocket Manager: {adapter.name} stream cancelled.")
                break
            except Exception as e:
                logger.error(f"WebSocket Manager: Critical Crash in {adapter.name}: {e}")
                logger.info(f"WebSocket Manager: Restarting {adapter.name} in 5s...")
                await asyncio.sleep(5)

    async def stop(self):
        """Gracefully shuts down all WebSocket tasks"""
        self.running = False
        logger.info("WebSocket Manager: Stopping all streams...")
        
        for task in self.tasks:
            if not task.done():
                task.cancel()
        
        if self.tasks:
            # Wait for tasks to handle cancellation
            await asyncio.gather(*self.tasks, return_exceptions=True)
            
        self.tasks.clear()
        logger.info("WebSocket Manager: Shutdown complete.")