# src/websocket_manager.py - PUNKT 9: WEBSOCKETS REFACTOR MIT AUTO-RECONNECT

import asyncio
import json
import time
import logging
from typing import Dict, Optional, Callable, Any, Set, List
from dataclasses import dataclass, field
from enum import Enum

# Use the new asyncio API for better ping/pong handling
try:
    from websockets.asyncio.client import connect as ws_connect
    WEBSOCKETS_NEW_API = True
except ImportError:
    import websockets
    ws_connect = websockets.connect
    WEBSOCKETS_NEW_API = False

from websockets.exceptions import ConnectionClosed, InvalidStatusCode

logger = logging.getLogger(__name__)
import config


from src.utils import safe_float, mask_sensitive_data


@dataclass
class WSConfig:
    """WebSocket connection configuration
    
    ping_interval/ping_timeout: Library Keepalive (sendet Pings, antwortet auf Server-Pings)
    """
    url: str
    name: str
    ping_interval: Optional[float] = 20.0  # Library sendet Pings in diesem Intervall
    ping_timeout: Optional[float] = 20.0   # Library wartet so lange auf Pong
    reconnect_delay_initial: float = 1.0
    reconnect_delay_max: float = 60.0
    reconnect_delay_multiplier: float = 2.0
    max_reconnect_attempts: int = 0  # 0 = infinite
    message_queue_size: int = 10000  # Increased from 1000 to handle initial orderbook snapshot bursts
    headers: Optional[Dict[str, str]] = None


@dataclass
class WSMetrics:
    """Connection metrics for monitoring"""
    messages_received: int = 0
    messages_sent: int = 0
    reconnect_count: int = 0
    last_message_time: float = 0.0
    last_connect_time: float = 0.0
    last_error: Optional[str] = None
    uptime_seconds: float = 0.0


class WSState(Enum):
    """WebSocket connection state"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPED = "stopped"


class ManagedWebSocket:
    """
    Single WebSocket connection with auto-reconnect and health monitoring. 
    """
    
    def __init__(self, config: WSConfig, message_handler: Callable):
        self.config = config
        self.message_handler = message_handler
        
        self._ws: Optional[websockets. WebSocketClientProtocol] = None
        self._state = WSState. DISCONNECTED
        self._running = False
        
        self._reconnect_delay = config.reconnect_delay_initial
        self._reconnect_attempts = 0
        
        self._subscriptions: Set[str] = set()
        self._pending_subscriptions: Set[str] = set()
        
        self._metrics = WSMetrics()
        self._connect_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._process_task: Optional[asyncio.Task] = None  # Message processing task
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=config.message_queue_size)  # Use config value (10000) to handle snapshot bursts
        
        self._lock = asyncio.Lock()
    
    @property
    def state(self) -> WSState:
        return self._state
    
    @property
    def is_connected(self) -> bool:
        return self._state == WSState.CONNECTED and self._ws is not None
    
    @property
    def metrics(self) -> WSMetrics:
        if self._state == WSState.CONNECTED and self._metrics.last_connect_time > 0:
            self._metrics.uptime_seconds = time.time() - self._metrics.last_connect_time
        return self._metrics
    
    async def start(self):
        """Start connection with auto-reconnect"""
        if self._running:
            return
        
        self._running = True
        self._state = WSState.CONNECTING
        
        self._connect_task = asyncio.create_task(
            self._connection_loop(),
            name=f"ws_{self.config.name}_connect"
        )
        
        logger.info(f"🔌 [{self.config.name}] WebSocket manager started")
    
    async def stop(self):
        """Stop connection gracefully"""
        self._running = False
        self._state = WSState.STOPPED
        
        # Cancel tasks
        tasks = []
        for task in [self._receive_task, self._heartbeat_task, self._connect_task, self._process_task]:
            if task and not task.done():
                task.cancel()
                tasks.append(task)
                
        # Wait for handlers to finish
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.debug(f"[{self.config.name}] WebSocket tasks stopped (Timeout/Cancelled).")
            except Exception as e:
                logger.warning(f"[{self.config.name}] WebSocket stop error: {e}")
        
        # Close connection
        try:
            # FIX: Timeout erhöhen oder Fehler ignorieren, da wir eh abschalten
            if self._ws:
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug(f"[{self.config.name}] WebSocket connection closed forcefully (Timeout).")
        except Exception as e:
            logger.debug(f"[{self.config.name}] Error closing WebSocket connection: {e}")
        finally:
            self._ws = None
        
        logger.info(f"🔌 [{self.config.name}] WebSocket stopped")
    
    async def subscribe(self, channel: str, auth: Optional[str] = None):
        """Subscribe to a channel"""
        self._pending_subscriptions.add(channel)
        
        if self. is_connected:
            await self._send_subscription(channel, auth)
    
    async def unsubscribe(self, channel: str):
        """Unsubscribe from a channel"""
        self._subscriptions.discard(channel)
        self._pending_subscriptions.discard(channel)
        
        if self.is_connected:
            try:
                msg = {"type": "unsubscribe", "channel": channel}
                await self._ws.send(json. dumps(msg))
            except Exception as e:
                logger. debug(f"[{self.config. name}] Unsubscribe error: {e}")
    
    async def send(self, message: dict):
        """Send a message"""
        if not self.is_connected:
            logger.warning(f"[{self.config.name}] Cannot send: not connected")
            return False
        
        try:
            await self._ws.send(json.dumps(message))
            self._metrics.messages_sent += 1
            return True
        except Exception as e:
            logger.error(f"[{self.config.name}] Send error: {e}")
            return False
    
    async def _connection_loop(self):
        """Main connection loop with auto-reconnect"""
        while self._running:
            try:
                await self._connect()
                
                if self. is_connected:
                    # Reset reconnect delay on successful connection
                    self._reconnect_delay = self.config.reconnect_delay_initial
                    self._reconnect_attempts = 0
                    
                    # Start receive and heartbeat tasks
                    self._receive_task = asyncio. create_task(
                        self._receive_loop(),
                        name=f"ws_{self.config.name}_receive"
                    )
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(),
                        name=f"ws_{self.config.name}_heartbeat"
                    )
                    self._process_task = asyncio.create_task(
                        self._process_loop(),
                        name=f"ws_{self.config.name}_process"
                    )
                    
                    # KRITISCH: Warte auf erste Nachrichten/Pings bevor wir subscriben!
                    # X10 sendet erste Ping sehr schnell (nach ~15s) - wir müssen erst darauf antworten können
                    # Server pingt alle 15s, erwartet Pong innerhalb 10s
                    if self.config.name.startswith("x10"):
                        await asyncio.sleep(3.0)  # 3 Sekunden warten für erste Ping/Pong-Sequenz
                    
                    # Resubscribe to channels IN SEPARATEM TASK für X10
                    # Das verhindert Event Loop Blockierung während Ping/Pong
                    if self.config.name.startswith("x10"):
                        # Start resubscription in background task
                        asyncio.create_task(
                            self._resubscribe_all(),
                            name=f"ws_{self.config.name}_resubscribe"
                        )
                    else:
                        # Für andere: direkt subscriben
                        await self._resubscribe_all()
                    
                    # Wait for tasks to complete (connection lost)
                    # NOTE: Keepalive is now handled by websockets library (ping_interval/ping_timeout)
                    tasks_to_wait = [self._receive_task, self._heartbeat_task, self._process_task]
                    
                    done, pending = await asyncio.wait(
                        tasks_to_wait,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # Cancel remaining tasks
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._metrics.last_error = str(e)
                logger.error(f"[{self.config.name}] Connection error: {e}")
            
            # Reconnect logic
            if self._running:
                self._state = WSState. RECONNECTING
                self._metrics.reconnect_count += 1
                self._reconnect_attempts += 1
                
                if (self. config.max_reconnect_attempts > 0 and 
                    self._reconnect_attempts >= self.config.max_reconnect_attempts):
                    logger. error(f"[{self.config. name}] Max reconnect attempts reached")
                    break
                
                logger.info(
                    f"🔄 [{self.config.name}] Reconnecting in {self._reconnect_delay:.1f}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio. sleep(self._reconnect_delay)
                
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * self.config.reconnect_delay_multiplier,
                    self.config.reconnect_delay_max
                )
        
        self._state = WSState.STOPPED
    
    async def _connect(self):
        """Establish WebSocket connection"""
        self._state = WSState.CONNECTING
        
        try:
            connect_kwargs = {
                "close_timeout": 5.0,
                # CRITICAL für X10: max_size erhöhen für große Messages
                "max_size": 2**22,  # 4MB statt 1MB default
            }
            
            # Ping-Konfiguration:
            # 
            # websockets Library Verhalten:
            # - ping_interval: Sendet Client-Pings in diesem Intervall (None = keine Client-Pings)
            # - ping_timeout: Wartet so lange auf Pong vom Server für Client-Pings (None = nicht warten)
            # - Die Library antwortet AUTOMATISCH auf Server-Pings mit Pongs (unabhängig von ping_timeout!)
            #
            # X10 Anforderung:
            # - Server sendet Pings alle ~20s, erwartet Pong innerhalb 10s
            # - Wir setzen ping_interval=19s um aktiv zu pingen (knapp unter Server-Intervall)
            # - ping_timeout=None damit wir NICHT auf Server-Pongs für Client-Pings warten
            # - Die automatische Pong-Antwort auf Server-Pings funktioniert trotzdem!
            
            # KRITISCH: ping_interval und ping_timeout EXPLIZIT setzen!
            # - ping_interval=None: Keine Client-Pings, aber automatische Pongs auf Server-Pings
            # - ping_timeout=None: Warte nicht auf Server-Pongs (verhindert Timeouts)
            if self.config.ping_interval is not None:
                connect_kwargs["ping_interval"] = self.config.ping_interval
            if self.config.ping_timeout is not None:
                connect_kwargs["ping_timeout"] = self.config.ping_timeout
            
            # Add headers if configured
            if self.config.headers:
                connect_kwargs["additional_headers"] = self.config.headers
            
            # SECURITY: Mask sensitive data (API keys, tokens) before logging
            safe_kwargs = mask_sensitive_data(connect_kwargs)
            logger.info(f"🔌 [{self.config.name}] Connecting with kwargs: {safe_kwargs}")
            
            self._ws = await asyncio.wait_for(
                ws_connect(self.config.url, **connect_kwargs),
                timeout=30.0
            )
            
            self._state = WSState.CONNECTED
            self._metrics.last_connect_time = time.time()
            
            logger.info(f"✅ [{self.config.name}] Connected to {self.config.url}")
            if self.config.ping_interval:
                timeout_str = f"{self.config.ping_timeout}s" if self.config.ping_timeout else "None (auto-pong)"
                logger.info(f"✅ [{self.config.name}] Keepalive enabled: ping_interval={self.config.ping_interval}s, ping_timeout={timeout_str}")
            
        except asyncio.TimeoutError:
            logger.error(f"[{self.config. name}] Connection timeout")
            raise
        except InvalidStatusCode as e:
            logger.error(f"[{self.config.name}] Invalid status: {e. status_code}")
            raise
        except Exception as e:
            logger.error(f"[{self. config.name}] Connection failed: {e}")
            raise
    
    async def _receive_loop(self):
        """
        Receive messages and queue them for processing.
        
        KRITISCH: Async Iterator erlaubt der Library die Frame-Verarbeitung!
        Mit asyncio.wait_for(recv()) konnte die interne Ping/Pong Verarbeitung
        gestört werden.
        """
        try:
            logger.info(f"[{self.config.name}] Receive loop started (async iterator)")
            
            # KRITISCH: Async Iterator erlaubt der Library die Frame-Verarbeitung!
            async for message in self._ws:
                if not self._running:
                    break
                
                self._metrics.messages_received += 1
                self._metrics.last_message_time = time.time()
                
                # DEBUG logging
                if self.config.name.startswith("x10"):
                    preview = str(message)[:100] if message else "empty"
                    # logger.debug(f"[{self.config.name}] RAW MSG: {preview}")  # Commented out to reduce log noise
                
                # JSON ping handling (falls vorhanden)
                if isinstance(message, str) and '"ping"' in message:
                    try:
                        data = json.loads(message)
                        if "ping" in data:
                            ping_value = data["ping"]
                            await self._ws.send(json.dumps({"pong": ping_value}))
                            logger.debug(f"[{self.config.name}] JSON pong sent: {ping_value}")
                            continue
                    except json.JSONDecodeError:
                        pass
                
                # Queue message
                try:
                    self._message_queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning(f"[{self.config.name}] Message queue full")
                
                # KRITISCH: Yield nach JEDER Message für Ping/Pong-Verarbeitung!
                # X10 Server erwartet Pong innerhalb 10s - wir müssen reaktionsfähig bleiben
                await asyncio.sleep(0)  # Yield immediately to event loop
        
        except ConnectionClosed as e:
            logger.warning(f"[{self.config.name}] Connection closed: {e.code} {e.reason}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.config.name}] Receive error: {e}")
            raise
    
    async def _process_loop(self):
        """
        Process queued messages in a separate task.
        
        This decouples message processing from the receive loop,
        ensuring the receive loop stays free for ping/pong handling.
        
        OPTIMIZED: Process messages in batches during high-volume periods
        (e.g., initial orderbook snapshot burst) to prevent queue backup.
        """
        batch_size = 100  # Process up to 100 messages before yielding
        
        while self._running:
            try:
                # Wait for at least one message with timeout
                try:
                    message = await asyncio.wait_for(
                        self._message_queue.get(), 
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Process the first message
                await self._process_single_message(message)
                
                # Batch process additional queued messages without blocking
                # REDUCED batch size for X10 to prevent Event Loop blocking
                max_batch = 10 if self.config.name.startswith("x10") else batch_size
                processed = 1
                while processed < max_batch:
                    try:
                        message = self._message_queue.get_nowait()
                        await self._process_single_message(message)
                        processed += 1
                        
                        # Yield frequently for X10 to allow ping/pong processing
                        if self.config.name.startswith("x10") and processed % 5 == 0:
                            await asyncio.sleep(0)  # Yield every 5 messages
                    except asyncio.QueueEmpty:
                        break
                
                # Log queue depth periodically if it's backing up
                qsize = self._message_queue.qsize()
                if qsize > 1000:
                    logger.warning(f"[{self.config.name}] Message queue depth: {qsize}")
                
                # Yield to event loop after batch - KRITISCH für Ping/Pong!
                await asyncio.sleep(0)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.config.name}] Process loop error: {e}")
                await asyncio.sleep(0.1)
    
    async def _process_single_message(self, message: str):
        """Process a single message - extracted for batch processing."""
        try:
            # Handle binary frames (could be ping frames)
            if isinstance(message, bytes):
                logger.debug(f"[{self.config.name}] Received binary frame: {message[:20]}")
                return
            
            data = json.loads(message)
            
            # =================================================================
            # X10 APPLICATION-LEVEL PING HANDLING
            # =================================================================
            # X10 sends: {"ping": 12345}
            # We must respond: {"pong": 12345}
            # This is DIFFERENT from WebSocket protocol pings!
            # =================================================================
            if "ping" in data:
                ping_value = data["ping"]
                if self._ws and self.is_connected:
                    try:
                        await self._ws.send(json.dumps({"pong": ping_value}))
                        logger.debug(f"[{self.config.name}] Responded to X10 ping with pong: {ping_value}")
                    except Exception as e:
                        logger.warning(f"[{self.config.name}] Failed to send X10 pong: {e}")
                return  # Don't process ping as regular message
            
            # Handle other JSON-based ping/pong formats (Lighter uses {"type": "ping"})
            msg_type = data.get('type', '').lower() if isinstance(data.get('type'), str) else str(data.get('type', '')).lower()
            msg_event = data.get('event', '').lower() if isinstance(data.get('event'), str) else ''
            
            # Respond to {"type": "ping"} style pings (Lighter)
            if msg_type == 'ping' or msg_event == 'ping':
                if self._ws and self.is_connected:
                    try:
                        await self._ws.send(json.dumps({'type': 'pong'}))
                        logger.debug(f"[{self.config.name}] Responded to JSON ping with pong")
                    except Exception as e:
                        logger.debug(f"[{self.config.name}] Failed to send JSON pong: {e}")
                return
            
            # Handle PONG responses - pass to message handler for health tracking
            # (x10_account uses pongs for keepalive health monitoring)
            if msg_type == 'pong' or "pong" in data:
                logger.debug(f"[{self.config.name}] Received PONG response")
                # Don't return - let the message handler track pongs for health monitoring
                await self.message_handler(self.config.name, data)
                return
            
            await self.message_handler(self.config.name, data)
        except json.JSONDecodeError:
            logger.debug(f"[{self.config.name}] Invalid JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"[{self.config.name}] Handler error: {e}")
    
    async def _heartbeat_loop(self):
        """Enhanced heartbeat with specific checks for Firehose streams.
        
        X10 und Lighter WebSocket Ping-Verhalten:
        - Die websockets Library handhabt WebSocket-Protokoll-Pings automatisch
        - Sie antwortet auf Server-Pings mit Pongs
        - Mit ping_interval sendet sie auch Client-Pings
        
        Diese Methode überwacht die Verbindungsgesundheit mit unterschiedlichen
        Thresholds für Firehose-Streams (die nicht kontinuierlich senden) vs.
        reguläre Streams.
        
        SPECIAL HANDLING for x10_account:
        - The account stream may not send messages if no account activity
        - Health monitoring is handled separately by WebSocketManager._x10_account_keepalive_loop
        - This heartbeat only logs status, doesn't trigger reconnect (handled by keepalive loop)
        """
        # Unterschiedliche Thresholds für verschiedene Stream-Typen
        if self.config.name in ["x10_trades", "x10_funding"]:
            # Firehose: Mehr Toleranz, da nicht jede Sekunde Updates kommen
            stale_threshold = 300.0  # 5 Minuten für Firehose
            expected_interval = 60.0  # Erwarte mindestens alle 60s ein Update
        elif self.config.name == "x10_account":
            # Account stream: Relaxed threshold - health check handled by keepalive loop
            stale_threshold = 300.0  # 5 Minuten - reconnect handled by keepalive loop
            expected_interval = 30.0  # Just for logging
        else:
            # Regular streams: Strenger
            stale_threshold = 180.0  # 3 Minuten
            expected_interval = 30.0
        
        check_interval = 10.0
        connect_time = time.time()
        
        logger.debug(f"💓 [{self.config.name}] Heartbeat started (stale_threshold={stale_threshold}s)")
        
        while self._running and self.is_connected:
            try:
                await asyncio.sleep(check_interval)
                
                now = time.time()
                uptime = now - connect_time
                
                if self._metrics.last_message_time > 0:
                    silence = now - self._metrics.last_message_time
                    
                    # Warning at expected_interval
                    if silence > expected_interval and silence < stale_threshold:
                        logger.debug(
                            f"⚠️ [{self.config.name}] No messages for {silence:.0f}s "
                            f"(expected every {expected_interval:.0f}s)"
                        )
                    
                    # Reconnect at stale_threshold
                    # NOTE: x10_account reconnect is handled by dedicated keepalive loop
                    if silence > stale_threshold and self.config.name != "x10_account":
                        logger.warning(
                            f"🔴 [{self.config.name}] Stream stale ({silence:.0f}s), reconnecting..."
                        )
                        break
                    else:
                        logger.debug(
                            f"💓 [{self.config.name}] Alive (uptime={uptime:.0f}s, "
                            f"last_msg={silence:.0f}s ago, msgs={self._metrics.messages_received})"
                        )
                else:
                    # No messages yet
                    if uptime > 60.0:
                        logger.warning(
                            f"⚠️ [{self.config.name}] No messages received after {uptime:.0f}s"
                        )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.config.name}] Heartbeat error: {e}")
    
    async def _send_subscription(self, channel: str, auth: Optional[str] = None):
        """Send subscription message"""
        try:
            msg = {"type": "subscribe", "channel": channel}
            if auth:
                msg["auth"] = auth
            
            await self._ws.send(json.dumps(msg))
            self._subscriptions.add(channel)
            self._pending_subscriptions.discard(channel)
            
            logger.debug(f"[{self.config.name}] Subscribed to {channel}")
        except Exception as e:
            logger.error(f"[{self.config.name}] Subscription error: {e}")
    
    async def _resubscribe_all(self):
        """Resubscribe to all channels after reconnect - VERY slowly to allow ping/pong.
        
        CRITICAL: X10 server sends first ping very quickly after connection.
        We MUST keep the event loop free to respond, or we get disconnected.
        
        NOTE: X10 Firehose streams (x10_trades, x10_funding) don't require subscriptions.
        They automatically send data for all markets.
        """
        # Skip resubscription for Firehose streams (they don't need subscriptions)
        if self.config.name in ["x10_trades", "x10_funding"]:
            logger.debug(f"[{self.config.name}] Firehose stream - no subscription needed")
            return
        
        all_channels = list(self._subscriptions | self._pending_subscriptions)
        self._subscriptions.clear()
        
        if not all_channels:
            return
        
        logger.info(f"[{self.config.name}] Resubscribing to {len(all_channels)} channels (paced)...")
        
        # SEHR LANGSAM subscriben - Event Loop muss frei bleiben für Ping/Pong!
        # X10 sendet Pings regelmäßig (alle 20s) - wir müssen sofort antworten können!
        # Mit ping_interval=20.0 antwortet die Library automatisch, aber nur wenn Event Loop frei ist!
        
        if self.config.name.startswith("x10"):
            # X10: EXTREM LANGSAM - 300ms nach jeder Subscription!
            # Server sendet Pings alle 15s, erwartet Pong innerhalb 10s
            # Wenn Resubscription läuft, muss Event Loop frei bleiben für Ping/Pong!
            # 134 Channels × 300ms = ~40 Sekunden - das gibt genug Zeit für Ping/Pong!
            pause_after_each = 0.3  # 300ms nach jeder Subscription (EXTREM langsam!)
            pause_after_batch = 1.0  # Extra 1 Sekunde alle 3 Subscriptions für Ping/Pong!
            batch_size = 3  # Sehr kleine Batches für häufigeres Yielden
        else:
            # Andere: Normal langsam
            pause_after_each = 0.0  # Keine Pause nach jeder
            pause_after_batch = 0.1  # 100ms alle 10
            batch_size = 10
        
        start_time = time.time()
        for i, channel in enumerate(all_channels):
            try:
                msg = {"type": "subscribe", "channel": channel}
                await self._ws.send(json.dumps(msg))
                self._subscriptions.add(channel)
                
                # Yield nach JEDER Subscription - KRITISCH für Ping/Pong!
                # Längere Pause gibt der Library Zeit, Ping-Frames zu verarbeiten!
                await asyncio.sleep(pause_after_each)  # Pause für Ping/Pong-Verarbeitung
                
                # Extra Pause um Event Loop definitiv frei zu halten
                # X10 Server pingt alle 15s, erwartet Pong innerhalb 10s - wir müssen reaktionsfähig bleiben!
                if (i + 1) % batch_size == 0:
                    elapsed = time.time() - start_time
                    
                    # KRITISCH: Pause während erwarteter Ping/Pong-Fenster!
                    # Server pingt alle 15s - pausiere um diese Zeitpunkte herum
                    if self.config.name.startswith("x10"):
                        # Pausiere länger um die 15s-Marken herum (±2s Fenster)
                        time_since_start = elapsed % 15.0
                        if 13.0 <= time_since_start <= 17.0 or time_since_start <= 2.0:
                            # In Ping/Pong-Fenster - EXTRA lange Pause!
                            await asyncio.sleep(pause_after_batch * 2)  # Doppelte Pause!
                            logger.debug(f"[{self.config.name}] Paused in ping/pong window at {elapsed:.1f}s")
                        else:
                            await asyncio.sleep(pause_after_batch)  # Normale Pause
                    else:
                        await asyncio.sleep(pause_after_batch)  # Normale Pause
                    
                    logger.debug(f"[{self.config.name}] Resubscribed {i + 1}/{len(all_channels)} channels")
                    
            except Exception as e:
                logger.error(f"[{self.config.name}] Resubscription error for {channel}: {e}")
        
        logger.info(f"[{self.config.name}] Resubscribed to {len(all_channels)} channels")


class WebSocketManager:
    """
    Manages multiple WebSocket connections for X10 and Lighter.
    
    Features:
    - Auto-reconnect with exponential backoff
    - Health monitoring and metrics
    - Subscription management
    - Message routing to handlers
    - X10 Account WebSocket health monitoring with keepalive pings
    """
    
    # Exchange WebSocket URLs
    LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
    X10_ACCOUNT_WS_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"  # Private: Balance, Orders, Positions
    X10_TRADES_WS_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/publicTrades"  # Public: Trades (Firehose - all markets)
    X10_FUNDING_WS_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/funding"  # Public: Funding Rates (Firehose - all markets)


    
    def __init__(self):
        self._connections: Dict[str, ManagedWebSocket] = {}
        self._message_handlers: Dict[str, List[Callable]] = {}
        self._running = False
        
        # Adapters for price/funding updates
        self. x10_adapter = None
        self.lighter_adapter = None
        
        # Prediction engine
        self. predictor = None
        
        # OI Tracker
        self. oi_tracker = None
        
        # ═══════════════════════════════════════════════════════════════
        # X10 Account WebSocket Health Tracking
        # The account stream may not send messages if no account activity
        # (no trades, position changes, etc.) - we need active keepalive
        # ═══════════════════════════════════════════════════════════════
        self._x10_account_stale_threshold = 180.0  # 3 minutes without ANY message triggers reconnect
        self._x10_account_ping_interval = 30.0     # Send application-level ping every 30s
        self._x10_account_keepalive_task: Optional[asyncio.Task] = None
        self._x10_account_last_pong_time = 0.0     # Track when we last received a pong
    
    def set_adapters(self, x10_adapter, lighter_adapter):
        """Set exchange adapters for data updates"""
        self. x10_adapter = x10_adapter
        self.lighter_adapter = lighter_adapter
    
    def set_predictor(self, predictor):
        """Set funding predictor for OI velocity updates"""
        self. predictor = predictor
    
    def set_oi_tracker(self, oi_tracker):
        """Set OI tracker for real-time updates"""
        self. oi_tracker = oi_tracker
    
    # ═══════════════════════════════════════════════════════════════
    # X10 ACCOUNT WEBSOCKET HEALTH MONITORING
    # ═══════════════════════════════════════════════════════════════
    
    async def _x10_account_keepalive_loop(self):
        """
        Dedicated keepalive loop for X10 account WebSocket.
        
        X10 account stream may not send messages if no account activity,
        but we need to verify the connection is still alive by sending
        application-level pings and checking for pong responses.
        """
        logger.info("💓 [x10_account] Keepalive loop started")
        
        while self._running:
            try:
                await asyncio.sleep(self._x10_account_ping_interval)
                
                if not self._running:
                    break
                
                # Check if we have a connection
                conn = self._connections.get("x10_account")
                if not conn or not conn.is_connected:
                    logger.debug("💓 [x10_account] Not connected, skipping keepalive ping")
                    continue
                
                # Send application-level ping
                try:
                    # X10 uses JSON ping format: {"type": "ping"} or {"ping": timestamp}
                    ping_msg = {"type": "ping", "ts": int(time.time() * 1000)}
                    success = await conn.send(ping_msg)
                    
                    if success:
                        logger.debug(f"💓 [x10_account] Sent keepalive ping")
                    else:
                        logger.warning(f"⚠️ [x10_account] Failed to send keepalive ping")
                        
                except Exception as e:
                    logger.warning(f"⚠️ [x10_account] Ping failed: {e}")
                
                # Check connection health
                is_healthy = await self._check_x10_account_health()
                if not is_healthy:
                    logger.warning(f"⚠️ [x10_account] Health check failed - triggering reconnect...")
                    await self._reconnect_x10_account()
                    
            except asyncio.CancelledError:
                logger.debug("💓 [x10_account] Keepalive loop cancelled")
                break
            except Exception as e:
                logger.error(f"[x10_account] Keepalive error: {e}")
                await asyncio.sleep(5.0)  # Brief pause on error
        
        logger.info("💓 [x10_account] Keepalive loop stopped")
    
    async def _check_x10_account_health(self) -> bool:
        """
        Check if X10 account WebSocket is healthy.
        
        Returns True if healthy, False if needs reconnect.
        A connection is considered unhealthy if:
        - WebSocket is closed
        - No messages received for longer than stale_threshold
        """
        conn = self._connections.get("x10_account")
        if not conn:
            logger.warning(f"⚠️ [x10_account] Connection object not found")
            return False
        
        # Check connection state
        if not conn.is_connected:
            logger.warning(f"⚠️ [x10_account] WebSocket not connected (state: {conn.state})")
            return False
        
        # Check message freshness (ANY message, including pongs)
        now = time.time()
        last_msg = conn.metrics.last_message_time or 0
        
        # Also consider pong time if available
        effective_last_msg = max(last_msg, self._x10_account_last_pong_time)
        
        if effective_last_msg > 0:
            silence_duration = now - effective_last_msg
            
            if silence_duration > self._x10_account_stale_threshold:
                logger.warning(
                    f"⚠️ [x10_account] STALE - No messages for {silence_duration:.0f}s "
                    f"(threshold: {self._x10_account_stale_threshold}s)"
                )
                return False
            else:
                logger.debug(
                    f"💓 [x10_account] Health OK - last activity {silence_duration:.0f}s ago"
                )
        
        return True
    
    async def _reconnect_x10_account(self):
        """
        Force reconnect of X10 account WebSocket.
        
        This stops the current connection and lets the auto-reconnect
        mechanism in ManagedWebSocket handle the reconnection.
        """
        conn = self._connections.get("x10_account")
        if not conn:
            return
        
        logger.warning(f"🔄 [x10_account] Forcing reconnection due to stale connection...")
        
        try:
            # Stop the current connection - this will trigger the auto-reconnect loop
            await conn.stop()
            
            # Brief pause before restart
            await asyncio.sleep(1.0)
            
            # Restart the connection
            await conn.start()
            
            logger.info(f"✅ [x10_account] Reconnection initiated")
            
        except Exception as e:
            logger.error(f"❌ [x10_account] Reconnection failed: {e}")
    
    def _handle_x10_account_pong(self, msg: dict):
        """
        Handle pong response from X10 account WebSocket.
        
        Updates the last pong time to indicate the connection is alive,
        even if no account events are occurring.
        """
        msg_type = msg.get("type", "")
        
        if msg_type == "pong" or "pong" in msg:
            self._x10_account_last_pong_time = time.time()
            logger.debug(f"💓 [x10_account] Received pong - connection alive")
            return True
        
        return False
    
    async def start(self, ping_interval: Optional[float] = None, ping_timeout: Optional[float] = None):
        """Start all WebSocket connections
        
        Args:
            ping_interval: WebSocket ping interval in seconds (for X10 connection, default: None)
            ping_timeout: WebSocket ping timeout in seconds (for X10 connection, default: None)
        """
        if self._running:
            return
        
        self._running = True
        
        # 1. Create Lighter connection (bleibt wie sie ist)
        lighter_config = WSConfig(
            url=self.LIGHTER_WS_URL,
            name="lighter",
            ping_interval=20.0,
            ping_timeout=10.0
        )
        self._connections["lighter"] = ManagedWebSocket(
            lighter_config, 
            self._handle_message
        )
        
        # X10 Header Setup
        x10_headers = {
            "X-Api-Key": getattr(config, "X10_API_KEY", ""),
            "User-Agent": "X10PythonTradingClient/0.4.5",
        }
        
        # X10 WebSocket Ping-Strategie (basierend auf Discord-Info):
        # 
        # WICHTIG (aus Discord): "The server sends pings every 15 seconds and expects 
        # a pong response within 10 seconds."
        # 
        # Das bedeutet:
        # - Server pingt alle 15 Sekunden
        # - Erwartet Pong innerhalb 10 Sekunden
        # - Timeout nach ~25 Sekunden wenn kein Pong kommt
        # 
        # KRITISCHER FEHLER: ping_interval=None DEAKTIVIERT die automatische Pong-Antwort!
        # Die websockets Library antwortet NUR automatisch auf Server-Pings, wenn ping_interval gesetzt ist!
        # 
        # LÖSUNG:
        # - ping_interval=15.0: Sendet Client-Pings alle 15s (wie Server) UND aktiviert automatische Pong-Antwort
        # - ping_timeout=None: Warte NICHT auf Server-Pongs für unsere Client-Pings (verhindert Timeouts)
        # - Die Library antwortet automatisch auf Server-Pings (nur wenn ping_interval gesetzt!)
        
        # 2. X10 PRIVATE Connection (Account Stream) - NUR Authentifiziert
        # WICHTIG: Hier ping_interval=15.0 setzen, da X10 Server Pings sendet.
        # Wir antworten nur auf Server-Pings (macht die Library automatisch bei eingehenden Pings).
        x10_account_config = WSConfig(
            url=self.X10_ACCOUNT_WS_URL,  # PRIVATE URL
            name="x10_account",
            ping_interval=15.0,  # Client sendet aktiv Pings um Verbindung offen zu halten
            ping_timeout=None,  # Ignoriere fehlende Pongs vom Server (verhindert 1011 Fehler clientseitig)
            headers=x10_headers,
        )
        self._connections["x10_account"] = ManagedWebSocket(
            x10_account_config,
            self._handle_message
        )
        
        # 3. X10 TRADES Connection (Public Firehose) - NEU!
        # Liefert automatisch Trades für ALLE Märkte (kein Subscribe nötig)
        x10_trades_config = WSConfig(
            url=self.X10_TRADES_WS_URL,  # PUBLIC TRADES URL
            name="x10_trades",
            ping_interval=15.0,
            ping_timeout=None,
            headers=x10_headers,  # Header oft auch für Public nötig wegen Rate Limits
        )
        self._connections["x10_trades"] = ManagedWebSocket(
            x10_trades_config,
            self._handle_message
        )
        
        # 4. X10 FUNDING Connection (Public Firehose) - NEU!
        # Liefert automatisch Funding Rates für ALLE Märkte (kein Subscribe nötig)
        x10_funding_config = WSConfig(
            url=self.X10_FUNDING_WS_URL,  # PUBLIC FUNDING URL
            name="x10_funding",
            ping_interval=15.0,
            ping_timeout=None,
            headers=x10_headers,  # Header oft auch für Public nötig wegen Rate Limits
        )
        self._connections["x10_funding"] = ManagedWebSocket(
            x10_funding_config,
            self._handle_message
        )

        # 5. X10 ORDERBOOK Connection (Public, Delta Updates)
        # Correct URL: plural 'orderbooks'
        x10_orderbook_config = WSConfig(
            url="wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks",
            name="x10_orderbooks", 
            ping_interval=15.0,
            ping_timeout=None,
            headers=x10_headers,
        )
        self._connections["x10_orderbooks"] = ManagedWebSocket(
            x10_orderbook_config,
            self._handle_message
        )

        
        # Start all connections (use return_exceptions to prevent "exception was never retrieved")
        await asyncio.gather(*[
            conn.start() for conn in self._connections.values()
        ], return_exceptions=True)
        
        # Start X10 account keepalive loop (dedicated health monitoring)
        self._x10_account_keepalive_task = asyncio.create_task(
            self._x10_account_keepalive_loop(),
            name="x10_account_keepalive"
        )
        
        logger.info("✅ WebSocketManager started (Lighter + X10 Account/Trades/Funding + Keepalive)")
    
    async def stop(self):
        """Stop all WebSocket connections"""
        self._running = False
        
        # Stop X10 account keepalive task first
        if self._x10_account_keepalive_task and not self._x10_account_keepalive_task.done():
            self._x10_account_keepalive_task.cancel()
            try:
                await asyncio.wait_for(self._x10_account_keepalive_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        
        await asyncio.gather(*[
            conn. stop() for conn in self._connections.values()
        ], return_exceptions=True)
        
        self._connections.clear()
        logger.info("✅ WebSocketManager stopped")
    
    async def subscribe_lighter(self, channels: List[str]):
        """Subscribe to Lighter channels"""
        conn = self._connections. get("lighter")
        if conn:
            for channel in channels:
                await conn.subscribe(channel)
    
    async def subscribe_x10(self, channels: List[str], use_trades_stream: bool = False, use_funding_stream: bool = False):
        """Subscribe to X10 channels
        
        NOTE: X10 Firehose streams (trades/funding) don't require manual subscription.
        They automatically send data for all markets. This method is kept for compatibility
        but only works with x10_account for account-related subscriptions.
        
        Args:
            channels: List of channel names
            use_trades_stream: If True, use x10_trades connection (NOTE: Firehose, no subscribe needed)
            use_funding_stream: If True, use x10_funding connection (NOTE: Firehose, no subscribe needed)
        """
        if use_trades_stream or use_funding_stream:
            logger.warning("⚠️ X10 Trades/Funding streams are Firehose - no manual subscription needed!")
            return
        
        # Only account stream supports manual subscriptions
        conn = self._connections.get("x10_account")
        if conn:
            for channel in channels:
                await conn.subscribe(channel)
        else:
            logger.error("❌ X10 Account Connection not found!")
    
    async def subscribe_market_data(self, symbols: List[str]):
        """Subscribe to market data for given symbols"""
        # Lighter subscriptions
        lighter_conn = self._connections.get("lighter")
        if lighter_conn:
            # Subscribe to all markets stats
            await lighter_conn.subscribe("market_stats/all")
            
            # Subscribe to orderbooks and trades for each symbol
            for symbol in symbols:
                market_id = self._get_lighter_market_id(symbol)
                if market_id is not None:
                    # Lighter verträgt mehr Subscriptions
                    await lighter_conn.subscribe(f"order_book/{market_id}")
                    await lighter_conn.subscribe(f"trade/{market_id}")
                    await asyncio.sleep(0.02)
        
        # 2. X10 Subscriptions
        # WICHTIG: Wir müssen NICHTS mehr senden!
        # Die Verbindung zu .../v1/publicTrades und .../v1/funding liefert automatisch 
        # Daten für ALLE Märkte (Firehose). Das manuelle Subscriben entfällt.
        x10_trades_conn = self._connections.get("x10_trades")
        x10_funding_conn = self._connections.get("x10_funding")
        
        if x10_trades_conn and x10_funding_conn:
            logger.info(f"[x10] Connected to Firehose streams (Trades & Funding) for all markets.")
        else:
            if not x10_trades_conn:
                logger.error("❌ X10 Trades Connection not found!")
            if not x10_funding_conn:
                logger.error("❌ X10 Funding Connection not found!")
    
    def _get_lighter_market_id(self, symbol: str) -> Optional[int]:
        """Get Lighter market ID for symbol"""
        if self.lighter_adapter and hasattr(self.lighter_adapter, 'market_info'):
            market = self.lighter_adapter.market_info.get(symbol)
            if market:
                return market. get('i') or market.get('market_id')
        return None
    
    async def _handle_message(self, source: str, msg: dict):
        """Route message to appropriate handler"""
        try:
            if source == "lighter":
                await self._handle_lighter_message(msg)
            
            # X10 Routing
            elif source == "x10_account":
                # Check for pong response first (keepalive health tracking)
                if self._handle_x10_account_pong(msg):
                    return  # Pong handled, no further processing needed
                
                # Account Updates (Balance, Orders, Positions)
                await self._handle_x10_message(msg)
            
            elif source == "x10_trades":
                # Public Trades Firehose - sendet direkt Trade-Daten für alle Märkte
                # Simuliere die Struktur, die der Adapter erwartet
                if "channel" not in msg:
                    # X10 Firehose sendet direkt Trade-Daten, kein "channel" Feld
                    # Wir setzen es für Kompatibilität mit dem Handler
                    pass
                await self._handle_x10_trade(msg)
            
            elif source == "x10_funding":
                # Funding Rates Firehose - sendet direkt Funding-Daten für alle Märkte
                await self._handle_x10_funding(msg)
            
            # Call registered handlers
            # Normalize source name for handlers (alle x10_* Quellen mapen zu "x10")
            handler_source = "x10" if source.startswith("x10") else source
            handlers = self._message_handlers.get(handler_source, [])
            for handler in handlers:
                try:
                    await handler(msg)
                except Exception as e:
                    logger.error(f"Message handler error: {e}")
        
        except Exception as e:
            logger.error(f"Message routing error ({source}): {e}")
    
    async def _handle_lighter_message(self, msg: dict):
        """Handle Lighter WebSocket messages"""
        msg_type = msg. get("type", "")
        channel = msg.get("channel", "")
        
        # Market stats update
        if "market_stats" in msg_type or "market_stats" in channel:
            await self._handle_lighter_market_stats(msg)
        
        # Order book update
        elif "order_book" in msg_type or "order_book" in channel:
            await self._handle_lighter_orderbook(msg)
        
        # Trade update
        elif "trade" in msg_type:
            await self._handle_lighter_trade(msg)
    
    async def _handle_lighter_market_stats(self, msg: dict):
        """Process Lighter market stats"""
        stats = msg.get("market_stats", {})
        if not stats:
            return
        
        market_id = stats. get("market_id")
        if market_id is None:
            return
        
        # Find symbol for market ID
        symbol = self._lighter_market_id_to_symbol(market_id)
        if not symbol:
            return
        
        # Update adapter caches
        if self.lighter_adapter:
            # Mark price
            mark_price = stats. get("mark_price")
            if mark_price:
                self.lighter_adapter._price_cache[symbol] = float(mark_price)
                self.lighter_adapter._price_cache_time[symbol] = time.time()
            
            # Funding rate
            funding_rate = stats.get("current_funding_rate") or stats.get("funding_rate")
            if funding_rate:
                self.lighter_adapter._funding_cache[symbol] = float(funding_rate) / 100
                self.lighter_adapter._funding_cache_time[symbol] = time.time()
            
            # Open interest
            open_interest = stats.get("open_interest")
            if open_interest and self.oi_tracker:
                self. oi_tracker. update_from_websocket(symbol, "lighter", float(open_interest))
    
    async def _handle_lighter_orderbook(self, msg: dict):
        """Process Lighter orderbook update - INCREMENTAL DELTAS
        
        Lighter sends delta updates, not full snapshots:
        - size > 0: Add or update the price level
        - size = 0: Remove the price level
        """
        data = msg.get("order_book", {})
        if not data:
            return
        
        # Extract market from channel
        channel = msg.get("channel", "")
        if ":" in channel:
            market_id = int(channel.split(":")[1])
            symbol = self._lighter_market_id_to_symbol(market_id)
            
            if symbol and self.lighter_adapter:
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                
                if bids or asks:
                    # Use the adapter's delta-merge handler
                    if hasattr(self.lighter_adapter, 'handle_orderbook_snapshot'):
                        self.lighter_adapter.handle_orderbook_snapshot(symbol, bids, asks)
                    else:
                        # Fallback: Must still do proper delta merging!
                        self._merge_lighter_orderbook_fallback(symbol, bids, asks)
    
    def _merge_lighter_orderbook_fallback(self, symbol: str, bids: list, asks: list):
        """
        Fallback delta merge for Lighter orderbook updates.
        Only used if lighter_adapter.handle_orderbook_snapshot is unavailable.
        
        Lighter sends INCREMENTAL DELTA updates:
        - size > 0: Add or update the price level
        - size = 0: Remove the price level from orderbook
        """
        current = self.lighter_adapter._orderbook_cache.get(symbol, {})
        
        # Get or initialize internal dicts
        bid_dict = current.get('_bid_dict', {})
        ask_dict = current.get('_ask_dict', {})
        
        # If no internal dicts, convert from lists
        if not bid_dict and current.get('bids'):
            bid_dict = {float(b[0]): float(b[1]) for b in current['bids'] if len(b) >= 2}
        if not ask_dict and current.get('asks'):
            ask_dict = {float(a[0]): float(a[1]) for a in current['asks'] if len(a) >= 2}
        
        # Merge bids
        for bid in bids:
            if isinstance(bid, (list, tuple)) and len(bid) >= 2:
                try:
                    price, size = float(bid[0]), float(bid[1])
                    if size == 0:
                        bid_dict.pop(price, None)
                    else:
                        bid_dict[price] = size
                except (ValueError, TypeError):
                    continue
        
        # Merge asks
        for ask in asks:
            if isinstance(ask, (list, tuple)) and len(ask) >= 2:
                try:
                    price, size = float(ask[0]), float(ask[1])
                    if size == 0:
                        ask_dict.pop(price, None)
                    else:
                        ask_dict[price] = size
                except (ValueError, TypeError):
                    continue
        
        # Store back sorted
        sorted_bids = sorted(bid_dict.items(), key=lambda x: x[0], reverse=True)
        sorted_asks = sorted(ask_dict.items(), key=lambda x: x[0])
        
        self.lighter_adapter._orderbook_cache[symbol] = {
            'bids': [[p, s] for p, s in sorted_bids],
            'asks': [[p, s] for p, s in sorted_asks],
            'timestamp': time.time(),
            '_bid_dict': bid_dict,
            '_ask_dict': ask_dict,
        }
    
    async def _handle_lighter_trade(self, msg: dict):
        """Process Lighter trade"""
        trades = msg.get("trades", [])
        for trade in trades:
            market_id = trade. get("market_id")
            price = safe_float(trade.get("price"), 0.0)
            
            if market_id is not None and price:
                symbol = self._lighter_market_id_to_symbol(market_id)
                if symbol and self.lighter_adapter:
                    self.lighter_adapter._price_cache[symbol] = float(price)
                    self. lighter_adapter._price_cache_time[symbol] = time. time()
    
    async def _handle_x10_message(self, msg: dict):
        """Handle X10 WebSocket messages from account stream.
        
        Handles both account-specific messages (ORDER, TRADE, BALANCE, POSITION)
        and public stream messages routed here.
        """
        msg_type = msg.get("type", "")
        
        # ═══════════════════════════════════════════════════════════════
        # ACCOUNT STREAM MESSAGE TYPES (x10_account connection)
        # These messages come automatically - no subscription needed!
        # ═══════════════════════════════════════════════════════════════
        
        # Order updates (new, filled, cancelled, etc.)
        if msg_type == "ORDER":
            await self._handle_x10_order_update(msg)
            return
        
        # Trade/fill notifications (CRITICAL for position tracking!)
        if msg_type == "TRADE":
            await self._handle_x10_trade_notification(msg)
            return
        
        # Balance updates
        if msg_type == "BALANCE":
            await self._handle_x10_balance_update(msg)
            return
        
        # Position updates
        if msg_type == "POSITION":
            await self._handle_x10_position_update(msg)
            return
        
        # ═══════════════════════════════════════════════════════════════
        # PUBLIC STREAM MESSAGE TYPES (from other connections)
        # ═══════════════════════════════════════════════════════════════
        
        # Mark price
        if msg_type == "MP":
            await self._handle_x10_mark_price(msg)
        
        # Funding
        elif "funding" in str(msg.get("channel", "")).lower():
            await self._handle_x10_funding(msg)
        
        # Orderbook
        elif "orderbook" in str(msg.get("channel", "")).lower():
            await self._handle_x10_orderbook(msg)
        
        # Public Trades
        elif "publicTrades" in str(msg.get("channel", "")):
            await self._handle_x10_trade(msg)
        
        # Open Interest
        elif msg.get("channel") == "open_interest":
            await self._handle_x10_open_interest(msg)
    
    async def _handle_x10_mark_price(self, msg: dict):
        """Process X10 mark price"""
        data = msg.get("data", {})
        market = data.get("m", "")
        price = data.get("p")
        
        if market and price:
            symbol = market.replace("/", "-")
            if self.x10_adapter:
                self. x10_adapter._price_cache[symbol] = float(price)
                self. x10_adapter._price_cache_time[symbol] = time.time()
    
    async def _handle_x10_funding(self, msg: dict):
        """Process X10 funding rate
        
        Handles both:
        - Firehose format (direct from /v1/funding): msg contains market and rate directly
        - Subscription format (from account stream): msg contains {"data": {"m": market, "f": rate}}
        """
        # Try Firehose format first (direct fields)
        market = msg.get("m") or msg.get("market", "")
        rate = msg.get("f") or msg.get("funding_rate") or msg.get("rate")
        
        # Fallback to subscription format (wrapped in "data")
        if not market or rate is None:
            data = msg.get("data", {})
            market = market or data.get("m", "") or data.get("market", "")
            rate = rate if rate is not None else data.get("f") or data.get("funding_rate")
        
        if market and rate is not None:
            symbol = market.replace("/", "-")
            if self.x10_adapter:
                self.x10_adapter._funding_cache[symbol] = float(rate)
                self.x10_adapter._funding_cache_time[symbol] = time.time()
    
    async def _handle_x10_orderbook(self, msg: dict):
        """Process X10 orderbook update"""
        data = msg.get("data", {})
        if not data:
            return

        # Market ID/Symbol
        market = data.get("m") or data.get("market") or msg.get("market")
        if not market:
            return

        symbol = market.replace("/", "-")
        
        # Bids/Asks
        bids = data.get("b") or data.get("bids") or []
        asks = data.get("a") or data.get("asks") or []

        if self.x10_adapter:
            msg_type = msg.get("type", "SNAPSHOT") # Default to snapshot if missing
            
            if msg_type == "SNAPSHOT":
                if hasattr(self.x10_adapter, 'handle_orderbook_snapshot'):
                    self.x10_adapter.handle_orderbook_snapshot(symbol, bids, asks)
            elif msg_type == "DELTA":
                if hasattr(self.x10_adapter, 'handle_orderbook_update'):
                    self.x10_adapter.handle_orderbook_update(symbol, bids, asks)
        
    
    async def _handle_x10_open_interest(self, msg: dict):
        """Process X10 open interest"""
        market = msg.get("market", "").replace("/", "-")
        oi = msg.get("open_interest")
        
        if market and oi:
            if self.oi_tracker:
                self.oi_tracker.update_from_websocket(market, "x10", float(oi))
            
            if self.predictor:
                self.predictor.update_oi_velocity(market, float(oi))
    
    # ═══════════════════════════════════════════════════════════════
    # X10 ACCOUNT STREAM HANDLERS (ORDER, TRADE, BALANCE, POSITION)
    # ═══════════════════════════════════════════════════════════════
    
    async def _handle_x10_order_update(self, msg: dict):
        """Process X10 order update from account stream.
        
        Message format:
        {
            "type": "ORDER",
            "data": {
                "orders": [{
                    "id": 123,
                    "market": "BTC-USD",
                    "status": "NEW" | "PARTIALLY_FILLED" | "FILLED" | "CANCELLED",
                    "side": "BUY" | "SELL",
                    "price": "12400.000000",
                    "qty": "10.000000",
                    "filledQty": "3.513000",
                    ...
                }]
            },
            "ts": 1715885884837,
            "seq": 1
        }
        """
        try:
            data = msg.get("data", {})
            orders = data.get("orders", [])
            
            for order in orders:
                market = order.get("market", "")
                status = order.get("status", "")
                order_id = order.get("id")
                side = order.get("side", "")
                price = order.get("price", "0")
                qty = order.get("qty", "0")
                filled_qty = order.get("filledQty", "0")
                
                logger.info(
                    f"📋 [x10_account] ORDER: {market} {side} {status} "
                    f"qty={qty} filled={filled_qty} @ ${price} (id={order_id})"
                )
                
        except Exception as e:
            logger.error(f"[x10_account] Order update error: {e}")

    async def _handle_x10_trade_notification(self, msg: dict):
        """Process X10 trade/fill notification from account stream.
        
        CRITICAL: This tells us when orders are filled!
        
        Message format:
        {
            "type": "TRADE",
            "data": {
                "trades": [{
                    "id": 123,
                    "market": "BTC-USD",
                    "orderId": 456,
                    "side": "BUY",
                    "price": "58853.40",
                    "qty": "0.09",
                    "fee": "0.00",
                    "isTaker": true,
                    ...
                }]
            }
        }
        """
        try:
            data = msg.get("data", {})
            trades = data.get("trades", [])
            
            for trade in trades:
                market = trade.get("market", "")
                side = trade.get("side", "")
                price = float(trade.get("price", 0))
                qty = float(trade.get("qty", 0))
                fee = float(trade.get("fee", 0))
                order_id = trade.get("orderId")
                is_taker = trade.get("isTaker", True)
                
                logger.info(
                    f"📊 [x10_account] FILL: {market} {side} {qty} @ ${price:.4f} "
                    f"(Order: {order_id}, Fee: ${fee:.4f}, Taker: {is_taker})"
                )
                
                # Notify fill callbacks (for parallel_execution)
                if hasattr(self, '_fill_callbacks'):
                    symbol = market.replace("/", "-")
                    for callback in self._fill_callbacks.get(symbol, []):
                        try:
                            await callback(trade)
                        except Exception as cb_err:
                            logger.error(f"Fill callback error: {cb_err}")
                            
        except Exception as e:
            logger.error(f"[x10_account] Trade notification error: {e}")

    async def _handle_x10_balance_update(self, msg: dict):
        """Process X10 balance update from account stream."""
        try:
            data = msg.get("data", {})
            balance = data.get("balance", {})
            
            if balance:
                equity = balance.get("equity", "0")
                available = balance.get("availableForTrade", "0")
                unrealized_pnl = balance.get("unrealisedPnl", "0")
                
                logger.debug(
                    f"💰 [x10_account] BALANCE: equity=${equity}, "
                    f"available=${available}, uPnL=${unrealized_pnl}"
                )
                
        except Exception as e:
            logger.error(f"[x10_account] Balance update error: {e}")

    async def _handle_x10_position_update(self, msg: dict):
        """Process X10 position update from account stream."""
        try:
            data = msg.get("data", {})
            positions = data.get("positions", [])
            
            for pos in positions:
                market = pos.get("market", "")
                side = pos.get("side", "")
                size = pos.get("size", "0")
                unrealized_pnl = pos.get("unrealisedPnl", "0")
                mark_price = pos.get("markPrice", "0")
                
                logger.debug(
                    f"📈 [x10_account] POSITION: {market} {side} size={size} "
                    f"markPrice=${mark_price} uPnL=${unrealized_pnl}"
                )
                
        except Exception as e:
            logger.error(f"[x10_account] Position update error: {e}")

    def register_fill_callback(self, symbol: str, callback):
        """Register callback to be notified of fills for a symbol."""
        if not hasattr(self, '_fill_callbacks'):
            self._fill_callbacks: Dict[str, List] = {}
        if symbol not in self._fill_callbacks:
            self._fill_callbacks[symbol] = []
        self._fill_callbacks[symbol].append(callback)

    async def _handle_x10_trade(self, msg: dict):
        """Process X10 public trades
        
        Handles both:
        - Firehose format (direct from /v1/publicTrades): msg contains trade data directly
        - Subscription format (from account stream): msg contains {"data": [...]}
        """
        # Try Firehose format first (direct trade object or list)
        trades = []
        if isinstance(msg, list):
            trades = msg
        elif "data" in msg:
            # Subscription format (wrapped in "data")
            data = msg.get("data", [])
            if isinstance(data, list):
                trades = data
            else:
                trades = [data]
        else:
            # Single trade object (Firehose)
            trades = [msg]
        
        for trade in trades:
            # Extract market from trade object
            market = trade.get("m") or trade.get("market", "")
            if not market:
                # Fallback: try to extract from channel if present
                channel = msg.get("channel", "")
                if "publicTrades/" in channel:
                    market = channel.replace("publicTrades/", "").replace("/", "-")
                else:
                    continue
            
            # Convert market format (BTC/USD -> BTC-USD)
            symbol = market.replace("/", "-")
            
            # Extract price
            price = trade.get("p") or trade.get("price")
            
            if symbol and price and self.x10_adapter:
                self.x10_adapter._price_cache[symbol] = float(price)
                self.x10_adapter._price_cache_time[symbol] = time.time()

    def _lighter_market_id_to_symbol(self, market_id: int) -> Optional[str]:
        """Convert Lighter market ID to symbol"""
        if not self.lighter_adapter or not hasattr(self.lighter_adapter, 'market_info'):
            return None
        
        for symbol, info in self.lighter_adapter. market_info.items():
            if info.get('i') == market_id or info.get('market_id') == market_id:
                return symbol
        return None
    
    def register_handler(self, source: str, handler: Callable):
        """Register additional message handler"""
        if source not in self._message_handlers:
            self._message_handlers[source] = []
        self._message_handlers[source].append(handler)
    
    def get_connection_status(self) -> Dict[str, dict]:
        """Get status of all connections"""
        return {
            name: {
                'state': conn.state.value,
                'connected': conn.is_connected,
                'metrics': {
                    'messages_received': conn.metrics.messages_received,
                    'messages_sent': conn.metrics.messages_sent,
                    'reconnect_count': conn.metrics.reconnect_count,
                    'uptime_seconds': conn.metrics.uptime_seconds,
                    'last_error': conn.metrics.last_error
                }
            }
            for name, conn in self._connections.items()
        }
    
    def is_healthy(self) -> bool:
        """Check if all connections are healthy"""
        return all(conn.is_connected for conn in self._connections.values())


# ═══════════════════════════════════════════════════════════════
# SINGLETON & FACTORY
# ═══════════════════════════════════════════════════════════════
_ws_manager: Optional[WebSocketManager] = None


def get_websocket_manager() -> WebSocketManager:
    """Get or create singleton WebSocket manager"""
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = WebSocketManager()
    return _ws_manager


async def init_websocket_manager(x10_adapter, lighter_adapter, symbols: List[str] = None, 
                                  ping_interval: Optional[float] = None, ping_timeout: Optional[float] = None):
    """Initialize and start WebSocket manager
    
    Args:
        x10_adapter: X10 exchange adapter
        lighter_adapter: Lighter exchange adapter
        symbols: List of symbols to subscribe to
        ping_interval: WebSocket ping interval in seconds (for X10 connection)
        ping_timeout: WebSocket ping timeout in seconds (for X10 connection)
    """
    manager = get_websocket_manager()
    manager.set_adapters(x10_adapter, lighter_adapter)
    
    await manager.start(ping_interval=ping_interval, ping_timeout=ping_timeout)
    
    if symbols:
        await manager.subscribe_market_data(symbols)
    
    return manager