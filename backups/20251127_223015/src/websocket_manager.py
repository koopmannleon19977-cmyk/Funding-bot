# src/websocket_manager.py
import asyncio
import logging
import json
from decimal import Decimal
from typing import List, Any, Dict, Optional

logger = logging.getLogger(__name__)

from src.prediction_v2 import get_predictor


class WebSocketManager:
    """
    Centralized WebSocket Supervisor (Roadmap #9).
    Manages connection lifecycle, health checks, and automatic reconnections
    for all exchange adapters parallel to the main logic loop.
    """
    
    def __init__(self, adapters: List[Any]):
        self.adapters = adapters
        self.tasks = []
        self.predictor = get_predictor()  # ← Punkt 7: Live-Feeds für Prediction v2
        self.orderbook_cache: Dict[str, dict] = {}
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

    # === ORDERBOOK & OI LIVE FEED (Punkt 7) ===
    async def _handle_x10_orderbook(self, msg: dict):
        try:
            data = msg.get("result", {}) if "result" in msg else msg
            channel = data.get("channel", "")
            if "orderbook" not in channel:
                return

            symbol = data.get("market", "").replace("/", "-")
            if not symbol:
                return

            bids = data.get("data", {}).get("bids", [])
            asks = data.get("data", {}).get("asks", [])
            if not bids or not asks:
                return

            # Cache für Imbalance-Berechnung
            self.orderbook_cache[symbol] = {"bids": bids, "asks": asks}

            # Imbalance berechnen (genau wie in prediction_v2)
            bid_vol = sum(float(p) * float(q) for p, q in bids[:20])
            ask_vol = sum(float(p) * float(q) for p, q in asks[:20])
            total = bid_vol + ask_vol
            imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

            self.predictor.update_orderbook_imbalance(symbol, imbalance)
        except Exception as e:
            logger.debug(f"X10 OB parse error: {e}")

    async def _handle_x10_open_interest(self, msg: dict):
        try:
            if msg.get("channel") != "open_interest":
                return
            symbol = msg.get("market", "").replace("/", "-")
            oi = float(msg.get("open_interest", 0))
            self.predictor.update_oi_velocity(symbol, oi)
        except Exception as e:
            logger.debug(f"X10 OI parse error: {e}")

    async def _handle_lighter_orderbook(self, msg: dict):
        try:
            if msg.get("type") != "snapshot" and msg.get("type") != "l2update":
                return
            symbol = msg.get("market", "")
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            if not bids or not asks:
                return

            self.orderbook_cache[symbol] = {"bids": bids, "asks": asks}

            bid_vol = sum(float(p) * float(q) for p, q in bids[:20])
            ask_vol = sum(float(p) * float(q) for p, q in asks[:20])
            total = bid_vol + ask_vol
            imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

            self.predictor.update_orderbook_imbalance(symbol, imbalance)
        except Exception as e:
            logger.debug(f"Lighter OB parse error: {e}")

    async def _handle_lighter_open_interest(self, msg: dict):
        try:
            if "open_interest" not in msg.get("channel", ""):
                return
            symbol = msg.get("market", "")
            oi = float(msg.get("open_interest", 0))
            self.predictor.update_oi_velocity(symbol, oi)
        except Exception as e:
            logger.debug(f"Lighter OI parse error: {e}")

    async def _supervisor(self, adapter):
        """
        Monitors a single adapter's WebSocket connection.
        If the adapter's start_websocket() returns or raises, this restarts it.
        """
        while self.running:
            ws_task = None
            consumer_task = None
            try:
                # Run the adapter websocket in background so we can also consume its message stream
                ws_task = asyncio.create_task(adapter.start_websocket())

                # Consumer: if adapter exposes `ws_message_stream`, iterate and handle messages
                if hasattr(adapter, 'ws_message_stream') and callable(getattr(adapter, 'ws_message_stream')):
                    async def _consume():
                        try:
                            async for msg in adapter.ws_message_stream():
                                try:
                                    if adapter.name == "X10":
                                        await self._handle_x10_orderbook(msg)
                                        await self._handle_x10_open_interest(msg)
                                    elif adapter.name == "Lighter":
                                        await self._handle_lighter_orderbook(msg)
                                        await self._handle_lighter_open_interest(msg)
                                except Exception:
                                    continue
                        except asyncio.CancelledError:
                            return
                        except Exception as e:
                            logger.debug(f"WebSocketManager: consumer error for {adapter.name}: {e}")

                    consumer_task = asyncio.create_task(_consume())

                # Wait until the websocket task finishes (it should run until error/cancel)
                await ws_task
                # If ws_task ends, allow loop to restart it after cleanup
                logger.warning(f"WebSocket Manager: {adapter.name} start_websocket() returned; will restart.")
            except asyncio.CancelledError:
                logger.info(f"WebSocket Manager: {adapter.name} stream cancelled.")
                if consumer_task and not consumer_task.done():
                    consumer_task.cancel()
                if ws_task and not ws_task.done():
                    ws_task.cancel()
                break
            except Exception as e:
                logger.error(f"WebSocket Manager: Critical Crash in {adapter.name}: {e}")
                logger.info(f"WebSocket Manager: Restarting {adapter.name} in 5s...")
                # Ensure consumer is cancelled
                if consumer_task and not consumer_task.done():
                    consumer_task.cancel()
                if ws_task and not ws_task.done():
                    ws_task.cancel()
                await asyncio.sleep(10)  # Erhöhe Backoff
            finally:
                # Cleanup tasks before retrying
                if consumer_task:
                    try:
                        await asyncio.gather(consumer_task, return_exceptions=True)
                    except Exception:
                        pass
                if ws_task:
                    try:
                        await asyncio.gather(ws_task, return_exceptions=True)
                    except Exception:
                        pass

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