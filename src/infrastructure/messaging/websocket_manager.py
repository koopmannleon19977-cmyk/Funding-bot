# src/infrastructure/messaging/websocket_manager.py - PUNKT 9: WEBSOCKETS REFACTOR MIT AUTO-RECONNECT
# Note: This file has been moved to infrastructure/messaging/ for better organization

import asyncio
import json
import time
import logging
from typing import Dict, Optional, Callable, Any, Set, List, Tuple
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
    
    Enhanced 1006 Handling:
    - max_consecutive_1006: After this many 1006 errors, trigger extended wait
    - extended_delay_1006: Delay to use after consecutive 1006 errors
    - health_check_interval: How often to check connection health
    
    Lighter-specific 1006 Prevention:
    - json_ping_interval: Interval for JSON {"type":"ping"} messages (Lighter-specific)
    - json_pong_timeout: Max time to wait for pong before marking unhealthy
    - send_ping_on_connect: Send immediate ping after connection established
    """
    url: str
    name: str
    ping_interval: Optional[float] = 15.0  # Reduced from 20 to 15 for faster detection
    ping_timeout: Optional[float] = 10.0   # Increased from 8 to 10 for tolerance
    reconnect_delay_initial: float = 2.0   # Reduced from 5 to 2 for faster reconnect
    reconnect_delay_max: float = 120.0     # Increased from 60 to 120 for server recovery
    reconnect_delay_multiplier: float = 1.5  # Smoother exponential backoff
    max_reconnect_attempts: int = 0  # 0 = infinite
    message_queue_size: int = 10000  # Handle initial orderbook snapshot bursts
    headers: Optional[Dict[str, str]] = None
    # Enhanced 1006 error handling
    max_consecutive_1006: int = 5          # Trigger extended wait after this many 1006s
    extended_delay_1006: float = 30.0      # Extended delay after repeated 1006 errors
    health_check_interval: float = 30.0    # Health check interval in seconds
    # Lighter-specific 1006 Prevention (JSON ping/pong)
    json_ping_interval: Optional[float] = None  # Set to 10.0 for Lighter
    json_pong_timeout: float = 15.0             # Max time without pong before unhealthy
    send_ping_on_connect: bool = False          # Send immediate ping after connect


@dataclass
class WSMetrics:
    """Connection metrics for monitoring with enhanced error tracking"""
    messages_received: int = 0
    messages_sent: int = 0
    reconnect_count: int = 0
    last_message_time: float = 0.0
    last_connect_time: float = 0.0
    last_error: Optional[str] = None
    uptime_seconds: float = 0.0
    # Enhanced 1006 error tracking
    error_counts: Dict[int, int] = field(default_factory=dict)  # Track errors by code
    last_error_code: int = 0                                     # Last disconnect code
    last_pong_time: float = 0.0                                  # Last pong received
    is_healthy: bool = False                                     # Connection health status
    # Enhanced pong tracking for 1006 prevention
    last_ping_sent_time: float = 0.0                             # When we last sent a ping
    pings_sent: int = 0                                          # Total pings sent
    pongs_received: int = 0                                      # Total pongs received
    missed_pongs: int = 0                                        # Consecutive missed pongs


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
    
    def __init__(
        self, 
        config: WSConfig, 
        message_handler: Callable, 
        on_reconnect: Optional[Callable] = None, 
        on_pong: Optional[Callable] = None,
        on_health_change: Optional[Callable[[str, bool], None]] = None
    ):
        self.config = config
        self.message_handler = message_handler
        self.on_reconnect = on_reconnect  # Callback when connection is reestablished
        self.on_pong = on_pong  # Callback when pong is received (for manager-level tracking)
        self.on_health_change = on_health_change  # Callback when health state changes (name, is_healthy)
        
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
    
    def _set_health(self, healthy: bool) -> None:
        """
        Set health status and invoke callback if state changed.
        
        Args:
            healthy: New health status
        """
        old_health = self._metrics.is_healthy
        self._metrics.is_healthy = healthy
        
        # Only invoke callback if state actually changed
        if old_health != healthy and self.on_health_change:
            try:
                self.on_health_change(self.config.name, healthy)
            except Exception as e:
                logger.warning(f"[{self.config.name}] Health callback error: {e}")
    
    def classify_error(self, code: int) -> Tuple[str, bool, float]:
        """
        Classify WebSocket error and determine reconnect behavior.
        
        Args:
            code: WebSocket close code
            
        Returns:
            Tuple of (error_description, should_reconnect, extra_delay)
            - error_description: Human readable error description
            - should_reconnect: Whether to attempt reconnection
            - extra_delay: Additional delay before reconnecting (seconds)
        """
        ERROR_MAP = {
            # Normal closure - don't reconnect
            1000: ("Normal closure", False, 0.0),
            # Server/protocol errors - reconnect with normal delay
            1001: ("Going away", True, 0.0),
            1002: ("Protocol error", True, 5.0),
            1003: ("Unsupported data", True, 5.0),
            # 1006: Abnormal closure (no close frame) - COMMON, needs special handling
            1006: ("Abnormal closure (no close frame)", True, 0.0),  # Delay handled specially
            # Server errors - reconnect with delay
            1011: ("Server error", True, 10.0),
            1012: ("Service restart", True, 15.0),
            1013: ("Try again later", True, 30.0),
            1014: ("Bad gateway", True, 10.0),
            1015: ("TLS handshake failed", True, 5.0),
        }
        
        if code in ERROR_MAP:
            return ERROR_MAP[code]
        
        # Unknown error codes - safe default (reconnect with small delay)
        return (f"Unknown error ({code})", True, 5.0)
    
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
            # ═══════════════════════════════════════════════════════════════
            # FIX: Use consistent timeout with connect_kwargs (5.0s)
            # Also: Send close frame without waiting for server acknowledgment
            # during shutdown to avoid blocking
            # ═══════════════════════════════════════════════════════════════
            if self._ws:
                # First, try to close gracefully with the configured close_timeout
                # The websockets library will send a close frame and wait for response
                try:
                    await asyncio.wait_for(self._ws.close(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Server didn't respond in time - force close the transport
                    # This is normal during shutdown when server is slow to respond
                    if hasattr(self._ws, 'transport') and self._ws.transport:
                        self._ws.transport.close()
                    logger.debug(f"[{self.config.name}] WebSocket closed (server slow to respond)")
        except asyncio.CancelledError:
            logger.debug(f"[{self.config.name}] WebSocket close cancelled")
        except Exception as e:
            logger.debug(f"[{self.config.name}] WebSocket close: {e}")
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
        """Main connection loop with auto-reconnect and improved 1006 handling"""
        consecutive_1006_errors = 0
        is_first_connect = True
        
        while self._running:
            try:
                await self._connect()
                
                if self.is_connected:
                    # Reset counters on successful connection
                    self._reconnect_delay = self.config.reconnect_delay_initial
                    self._reconnect_attempts = 0
                    consecutive_1006_errors = 0  # Reset 1006 counter on successful connection
                    
                    # Reset ping/pong metrics for fresh connection (1006 prevention tracking)
                    self._metrics.pings_sent = 0
                    self._metrics.pongs_received = 0
                    self._metrics.missed_pongs = 0
                    self._metrics.last_ping_sent_time = 0.0
                    self._metrics.last_pong_time = time.time()  # Initialize to now
                    
                    # ═══════════════════════════════════════════════════════════════
                    # CRITICAL: Call on_reconnect callback to invalidate orderbooks
                    # This prevents crossed book conditions after WebSocket reconnects
                    # ═══════════════════════════════════════════════════════════════
                    if not is_first_connect and self.on_reconnect:
                        try:
                            self.on_reconnect(self.config.name)
                        except Exception as e:
                            logger.error(f"[{self.config.name}] on_reconnect callback error: {e}")
                    is_first_connect = False
                    
                    # Start receive and heartbeat tasks
                    self._receive_task = asyncio.create_task(
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
            
            # Reconnect logic with enhanced error classification
            if self._running:
                self._state = WSState.RECONNECTING
                self._metrics.reconnect_count += 1
                self._reconnect_attempts += 1
                self._set_health(False)
                
                # Extract error code for classification
                error_code = 0
                if self._metrics.last_error:
                    # Try to extract numeric code from error message
                    import re
                    match = re.search(r'(\d{4})', str(self._metrics.last_error))
                    if match:
                        error_code = int(match.group(1))
                
                # Classify the error and get reconnect parameters
                error_desc, should_reconnect, extra_delay = self.classify_error(error_code)
                
                # Track error counts per code
                self._metrics.error_counts[error_code] = self._metrics.error_counts.get(error_code, 0) + 1
                self._metrics.last_error_code = error_code
                
                logger.warning(
                    f"⚠️ [{self.config.name}] Disconnected: {error_code} - {error_desc} "
                    f"(error count for {error_code}: {self._metrics.error_counts[error_code]})"
                )
                
                # ═══════════════════════════════════════════════════════════════════
                # ENHANCED 1006 ERROR HANDLING with Diagnostics
                # ═══════════════════════════════════════════════════════════════════
                if error_code == 1006:
                    consecutive_1006_errors += 1
                    
                    # Calculate diagnostic info
                    now = time.time()
                    time_since_last_pong = now - self._metrics.last_pong_time if self._metrics.last_pong_time > 0 else -1
                    time_since_last_msg = now - self._metrics.last_message_time if self._metrics.last_message_time > 0 else -1
                    time_since_last_ping = now - self._metrics.last_ping_sent_time if self._metrics.last_ping_sent_time > 0 else -1
                    
                    logger.warning(
                        f"⚠️ [{self.config.name}] 1006 error #{consecutive_1006_errors} - "
                        f"connection closed without close frame\n"
                        f"    📊 Diagnostics:\n"
                        f"    - Last pong: {time_since_last_pong:.1f}s ago\n"
                        f"    - Last message: {time_since_last_msg:.1f}s ago\n"
                        f"    - Last ping sent: {time_since_last_ping:.1f}s ago\n"
                        f"    - Pings sent: {self._metrics.pings_sent}, Pongs received: {self._metrics.pongs_received}\n"
                        f"    - Missed pongs: {self._metrics.missed_pongs}"
                    )
                    
                    # Log possible causes based on diagnostics
                    if time_since_last_pong > 20:
                        logger.warning(f"    ⚠️ Possible cause: Server stopped responding to pings")
                    if self._metrics.pings_sent == 0:
                        logger.warning(f"    ⚠️ Possible cause: No pings were sent before disconnect")
                    if self._metrics.missed_pongs > 0:
                        logger.warning(f"    ⚠️ Possible cause: {self._metrics.missed_pongs} pongs were missed")
                    
                    # After multiple 1006 errors, use extended wait to let server recover
                    if consecutive_1006_errors >= self.config.max_consecutive_1006:
                        extended_delay = self.config.extended_delay_1006
                        # Increase delay further if still getting 1006s after extended waits
                        if self._metrics.error_counts.get(1006, 0) > 10:
                            extended_delay = min(extended_delay * 2, self.config.reconnect_delay_max)
                            
                        logger.warning(
                            f"🔄 [{self.config.name}] Frequent 1006 errors ({self._metrics.error_counts.get(1006, 0)} total) - "
                            f"extended wait {extended_delay:.1f}s\n"
                            f"    💡 Recommendations:\n"
                            f"    - Check network stability\n"
                            f"    - Verify Lighter API status\n"
                            f"    - Consider reducing subscription count"
                        )
                        await asyncio.sleep(extended_delay)
                        consecutive_1006_errors = 0  # Reset after extended wait
                        continue
                else:
                    # Reset 1006 counter on non-1006 error
                    consecutive_1006_errors = 0
                
                if not should_reconnect:
                    logger.info(f"[{self.config.name}] Not reconnecting (error type: {error_desc})")
                    break
                
                if (self.config.max_reconnect_attempts > 0 and 
                    self._reconnect_attempts >= self.config.max_reconnect_attempts):
                    logger.critical(
                        f"🚨 [{self.config.name}] Max reconnect attempts ({self.config.max_reconnect_attempts}) reached! "
                        f"Error counts: {dict(self._metrics.error_counts)}"
                    )
                    self._state = WSState.FAILED
                    break
                
                # Calculate delay with extra_delay from error classification
                total_delay = self._reconnect_delay + extra_delay
                
                logger.info(
                    f"🔄 [{self.config.name}] Reconnecting in {total_delay:.1f}s "
                    f"(attempt {self._reconnect_attempts}, delay={self._reconnect_delay:.1f}s + extra={extra_delay:.1f}s)"
                )
                await asyncio.sleep(total_delay)
                
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * self.config.reconnect_delay_multiplier,
                    self.config.reconnect_delay_max
                )
        
        self._state = WSState.STOPPED
    
    async def _connect(self):
        """Establish WebSocket connection with pre-cleanup"""
        self._state = WSState.CONNECTING
        
        # PRE-CLEANUP: Ensure old connection is fully closed before reconnecting
        # This prevents resource leaks and stale connection issues (especially for 1006 errors)
        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
            except Exception:
                pass
            finally:
                self._ws = None
        
        # Clear message queue to prevent stale data after reconnect
        while not self._message_queue.empty():
            try:
                self._message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        
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
            
            # KRITISCH: ping_interval und ping_timeout IMMER explizit setzen, auch wenn None.
            # Sonst greift der Default der websockets-Library (~20s) und sendet Protokoll-Pings,
            # was bei Lighter zu 1006/1011 führen kann.
            connect_kwargs["ping_interval"] = self.config.ping_interval  # darf None sein
            connect_kwargs["ping_timeout"] = self.config.ping_timeout    # darf None sein
            
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
            self._set_health(True)
            self._metrics.last_pong_time = time.time()  # Initialize for health tracking
            
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
                
                # DEBUG logging: capture potential ping/pong frames for x10_account only (noise-sensitive)
                if self.config.name == "x10_account":
                    preview = str(message)[:300] if message else "empty"
                    # Log only when ping/pong appears to avoid flooding
                    if isinstance(message, (str, bytes)) and (b'"ping"' in message if isinstance(message, bytes) else '"ping"' in message or '"pong"' in message):
                        logger.debug(f"[{self.config.name}] RAW MSG: {preview}")
                
                # ═══════════════════════════════════════════════════════════════════
                # JSON PING HANDLING: Server sends ping, we respond with pong
                # X10 sends: {"ping": timestamp_ms} -> we respond: {"pong": timestamp_ms}
                # ═══════════════════════════════════════════════════════════════════
                # Some servers use different JSON keepalive formats:
                # - {"ping": <ts>}                 -> respond {"pong": <ts>}
                # - {"type": "ping"} / {"type":"PING"} -> respond {"type":"pong"/"PONG"} (optionally echo timestamp)
                if isinstance(message, str) and '"ping"' in message.lower():
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        data = None

                    if isinstance(data, dict):
                        if "ping" in data:
                            ping_value = data.get("ping")
                            await self._ws.send(json.dumps({"pong": ping_value}))
                            self._metrics.last_pong_time = time.time()  # Track pong sent
                            logger.debug(f"[{self.config.name}] JSON pong sent: {ping_value}")
                            continue

                        msg_type = data.get("type") or data.get("event") or data.get("e")
                        if isinstance(msg_type, str) and msg_type.lower() == "ping":
                            pong_type = "PONG" if msg_type.isupper() else "pong"
                            pong_payload = {"type": pong_type}
                            for ts_key in ("timestamp", "ts", "time", "t"):
                                if ts_key in data:
                                    pong_payload[ts_key] = data[ts_key]
                                    break
                            await self._ws.send(json.dumps(pong_payload))
                            self._metrics.last_pong_time = time.time()
                            logger.debug(f"[{self.config.name}] Typed pong sent: {pong_payload}")
                            continue
                
                # ═══════════════════════════════════════════════════════════════════
                # JSON PONG HANDLING: Server responds to OUR keepalive pings
                # X10 responds: {"pong": timestamp_ms} to our {"ping": timestamp_ms}
                # CRITICAL: Handle x10_account pongs IMMEDIATELY in receive loop!
                # ═══════════════════════════════════════════════════════════════════
                if isinstance(message, str) and '"pong"' in message:
                    try:
                        data = json.loads(message)
                        if "pong" in data:
                            pong_value = data["pong"]
                            self._metrics.last_pong_time = time.time()
                            self._metrics.pongs_received += 1
                            self._metrics.missed_pongs = 0
                            self._set_health(True)
                            
                            # CALL MANAGER's ON_PONG CALLBACK (if registered)
                            # This updates manager-level tracking like _x10_account_last_pong_time
                            if self.on_pong:
                                try:
                                    self.on_pong(data)
                                except Exception as e:
                                    logger.warning(f"[{self.config.name}] on_pong callback error: {e}")
                            
                            # Log with latency
                            if self.config.name == "x10_account":
                                logger.info(
                                    f"💓 [x10_account] Received pong #{self._metrics.pongs_received}: "
                                    f"{pong_value} (latency: {(time.time() - self._metrics.last_ping_sent_time)*1000:.0f}ms)"
                                )
                            else:
                                logger.debug(f"[{self.config.name}] Pong received: {pong_value}")
                            
                            continue  # Don't queue pong messages, handled here
                    except json.JSONDecodeError:
                        pass
                
                # ═══════════════════════════════════════════════════════════════════
                # ENHANCED PONG TRACKING for 1006 Prevention (legacy formats)
                # Handles: {"type": "pong"}, {"type":"pong"}
                # ═══════════════════════════════════════════════════════════════════
                if isinstance(message, str):
                    msg_lower = message.lower()
                    is_pong = (
                        '"type": "pong"' in msg_lower or
                        '"type":"pong"' in msg_lower  # Without spaces
                    )
                    if is_pong:
                        self._metrics.last_pong_time = time.time()
                        self._metrics.pongs_received += 1
                        self._metrics.missed_pongs = 0  # Reset missed counter on successful pong
                        self._set_health(True)
                        if self.config.name == "lighter":
                            logger.debug(
                                f"💓 [{self.config.name}] Received pong #{self._metrics.pongs_received} - "
                                f"connection healthy (latency: "
                                f"{(time.time() - self._metrics.last_ping_sent_time)*1000:.0f}ms)"
                            )
                
                # Queue message
                try:
                    self._message_queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning(f"[{self.config.name}] Message queue full")
                
                # KRITISCH: Yield nach JEDER Message für Ping/Pong-Verarbeitung!
                # X10 Server erwartet Pong innerhalb 10s - wir müssen reaktionsfähig bleiben
                await asyncio.sleep(0)  # Yield immediately to event loop
        
        except ConnectionClosed as e:
            # Capture error code for classification in reconnect logic
            self._metrics.last_error = f"ConnectionClosed: {e.code} {e.reason}"
            self._metrics.last_error_code = e.code
            self._set_health(False)
            logger.warning(f"[{self.config.name}] Connection closed: {e.code} {e.reason}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._metrics.last_error = str(e)
            self._set_health(False)
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
        """Process a single message - handle X10 ping/pong correctly."""
        try:
            # Handle binary frames (could be ping frames)
            if isinstance(message, bytes):
                logger.debug(f"[{self.config.name}] Received binary frame: {message[:20]}")
                return
            
            data = json.loads(message)
            
            # =================================================================
            # X10 APPLICATION-LEVEL PING HANDLING (FIXED FORMAT)
            # =================================================================
            # X10 server sends: {"ping": 12345678}
            # We must respond: {"pong": 12345678}
            # This is DIFFERENT from WebSocket protocol pings!
            # =================================================================
            if "ping" in data and isinstance(data.get("ping"), (int, float)):
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
            
            # ═══════════════════════════════════════════════════════════════════════
            # KRITISCH: Respond to SERVER {"type": "ping"} with {"type": "pong"}
            # Lighter sendet Pings alle ~60s und erwartet Pong-Antwort!
            # Ohne Antwort → "no pong" / connection rejected / 1006
            # ═══════════════════════════════════════════════════════════════════════
            if msg_type == 'ping' or msg_event == 'ping':
                if self._ws and self.is_connected:
                    try:
                        await self._ws.send(json.dumps({'type': 'pong'}))
                        # Track that we received a server ping and responded
                        self._metrics.last_pong_time = time.time()  # Track server ping time
                        self._metrics.pongs_received += 1  # Count as "server pings received"
                        self._metrics.missed_pongs = 0  # Reset missed counter
                        self._set_health(True)
                        logger.info(f"💓 [{self.config.name}] Received SERVER ping → sent pong (ping #{self._metrics.pongs_received})")
                    except Exception as e:
                        logger.warning(f"⚠️ [{self.config.name}] Failed to respond to server ping: {e}")
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
        """Enhanced heartbeat with specific checks for Firehose streams and 1006 prevention.
        
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
        
        LIGHTER 1006 PREVENTION (Updated based on Discord community info):
        - Der SERVER sendet uns {"type":"ping"} alle ~60s
        - WIR antworten mit {"type":"pong"}
        - Wenn wir nicht antworten → connection rejected
        - Wir senden KEINE eigenen Pings (Server antwortet nicht darauf!)
        - Überwachung: Warnung wenn >90s kein Server-Ping kam
        """
        # Unterschiedliche Thresholds für verschiedene Stream-Typen
        if self.config.name in ["x10_trades", "x10_funding"]:
            # Firehose: Mehr Toleranz, da nicht jede Sekunde Updates kommen
            stale_threshold = 300.0  # 5 Minuten für Firehose
            expected_interval = 60.0  # Erwarte mindestens alle 60s ein Update
        elif self.config.name == "x10_account":
            # Account stream: Relaxed threshold - health check handled by keepalive loop
            stale_threshold = 300.0  # 5 Minuten - reconnect handled by keepalive loop
            expected_interval = 60.0  # Relaxed expectation - account stream can be quiet
        else:
            # Regular streams: Strenger
            stale_threshold = 180.0  # 3 Minuten
            expected_interval = 30.0
        
        check_interval = 5.0  # Reduced from 10.0 for faster detection
        
        # ═══════════════════════════════════════════════════════════════════════════
        # LIGHTER /stream: Passive mode - we wait for server pings
        # For /jsonapi (Order API): Active client pings would be needed (TS SDK pattern)
        # ═══════════════════════════════════════════════════════════════════════════
        json_ping_interval = self.config.json_ping_interval  # None for Lighter /stream!
        json_pong_timeout = self.config.json_pong_timeout    # 120s relaxed threshold
        
        connect_time = time.time()
        last_json_ping = 0.0  # Track when we last sent a ping (not used for Lighter /stream)
        
        # ═══════════════════════════════════════════════════════════════════════════
        # OPTIONAL: Immediate ping on connect (only for endpoints that support it)
        # Lighter /stream does NOT respond to client pings - so we skip this!
        # ═══════════════════════════════════════════════════════════════════════════
        if self.config.send_ping_on_connect and self.config.name != "lighter":
            # Only for non-Lighter connections (e.g., if X10 needs it)
            if self._ws and self.is_connected:
                try:
                    ping_msg = {"type": "ping"}
                    await self._ws.send(json.dumps(ping_msg))
                    self._metrics.pings_sent += 1
                    self._metrics.last_ping_sent_time = time.time()
                    last_json_ping = time.time()
                    logger.info(f"💓 [{self.config.name}] Sent IMMEDIATE ping on connect")
                except Exception as e:
                    logger.warning(f"[{self.config.name}] Failed to send immediate ping: {e}")
        elif self.config.name == "lighter":
            logger.info(f"💓 [{self.config.name}] Passive mode - waiting for SERVER pings (every ~60-90s)")
        
        logger.debug(
            f"💓 [{self.config.name}] Heartbeat started "
            f"(stale_threshold={stale_threshold}s, json_ping_interval={json_ping_interval}s)"
        )
        
        while self._running and self.is_connected:
            try:
                await asyncio.sleep(check_interval)
                
                now = time.time()
                uptime = now - connect_time
                
                # ═══════════════════════════════════════════════════════════════════
                # JSON HEARTBEAT - For non-Lighter connections that need active pings
                # Lighter /stream uses passive mode (server sends pings to us)
                # ═══════════════════════════════════════════════════════════════════
                if (
                    json_ping_interval
                    and self._ws
                    and self.is_connected
                    and now - last_json_ping >= json_ping_interval
                ):
                    try:
                        await self._ws.send(json.dumps({"type": "ping"}))
                        self._metrics.pings_sent += 1
                        self._metrics.last_ping_sent_time = now
                        last_json_ping = now
                        logger.debug(f"💓 [{self.config.name}] Sent JSON ping #{self._metrics.pings_sent}")
                    except Exception as e:
                        logger.warning(f"[{self.config.name}] Failed to send JSON ping: {e}")
                        self._set_health(False)
                        break
                
                # ═══════════════════════════════════════════════════════════════════
                # LIGHTER /stream: Server Ping Monitoring (Passive Mode)
                # The server sends us pings, we respond with pongs (handled in _process_single_message)
                # We just monitor if server pings are arriving - no action if they don't
                # ═══════════════════════════════════════════════════════════════════
                if self.config.name == "lighter" and self._metrics.last_pong_time > 0:
                    time_since_last_server_ping = now - self._metrics.last_pong_time
                    
                    # Info-level warning only if server pings are very stale (>120s)
                    # This is just monitoring, not a trigger for action
                    if time_since_last_server_ping > json_pong_timeout:
                        # Only log occasionally to avoid spam (every 60s)
                        if not hasattr(self, '_last_stale_warning_time') or now - self._last_stale_warning_time > 60.0:
                            logger.info(
                                f"ℹ️ [{self.config.name}] No server ping for {time_since_last_server_ping:.0f}s "
                                f"(threshold: {json_pong_timeout:.0f}s). Connection active via data stream."
                            )
                            self._last_stale_warning_time = now
                
                # Für nicht-Lighter: Original Pong-Timeout-Logik
                elif json_ping_interval and self._metrics.last_ping_sent_time > 0:
                    time_since_last_pong = now - self._metrics.last_pong_time
                    time_since_last_ping = now - self._metrics.last_ping_sent_time
                    
                    if (
                        self._metrics.pings_sent > 0 
                        and time_since_last_pong > json_pong_timeout
                        and time_since_last_ping > 2.0
                    ):
                        self._metrics.missed_pongs += 1
                        logger.warning(
                            f"⚠️ [{self.config.name}] PONG TIMEOUT! "
                            f"No pong for {time_since_last_pong:.1f}s "
                            f"(timeout={json_pong_timeout}s, missed={self._metrics.missed_pongs})"
                        )
                        
                        if self._metrics.missed_pongs >= 2:
                            logger.error(
                                f"🔴 [{self.config.name}] Connection unhealthy - "
                                f"{self._metrics.missed_pongs} missed pongs, triggering reconnect"
                            )
                            self._set_health(False)
                            break
                
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
                        self._set_health(False)
                        break
                    else:
                        # Periodic health log mit erweiterten Metriken
                        pong_age = now - self._metrics.last_pong_time if self._metrics.last_pong_time > 0 else -1
                        if self.config.name == "lighter":
                            # Für Lighter: pongs_received = Anzahl der Server-Pings auf die wir geantwortet haben
                            logger.debug(
                                f"💓 [{self.config.name}] Alive (uptime={uptime:.0f}s, "
                                f"last_msg={silence:.0f}s, last_server_ping={pong_age:.0f}s, "
                                f"server_pings_answered={self._metrics.pongs_received})"
                            )
                        else:
                            logger.debug(
                                f"💓 [{self.config.name}] Alive (uptime={uptime:.0f}s, "
                                f"last_msg={silence:.0f}s, last_pong={pong_age:.0f}s, "
                                f"pings={self._metrics.pings_sent}, pongs={self._metrics.pongs_received})"
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
        
        NOTE: X10 Account stream automatically sends all account events after authentication.
        No explicit subscription required - it's also a "firehose" for YOUR account data.
        """
        # Skip resubscription for Firehose streams (they don't need subscriptions)
        # NOTE: x10_account is also effectively a firehose for account data (orders, positions, fills, balance)
        # It automatically sends all YOUR account events after authentication via X-Api-Key header
        if self.config.name in ["x10_trades", "x10_funding", "x10_account"]:
            logger.debug(f"[{self.config.name}] Firehose/Account stream - no explicit subscription needed")
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
        # Orderbook Provider for invalidation on reconnect
        # ═══════════════════════════════════════════════════════════════
        self._orderbook_provider = None
        
        # ═══════════════════════════════════════════════════════════════
        # X10 Account WebSocket Health Tracking
        # The account stream may not send messages if no account activity
        # (no trades, position changes, etc.) - we need active keepalive
        # ═══════════════════════════════════════════════════════════════
        self._x10_account_stale_threshold = 600.0  # 10 minutes without ANY message triggers reconnect
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX: X10 Server trennt nach ~5 Minuten Inaktivität (1011 Ping Timeout).
        # Der Account-Stream sendet nur bei Account-Aktivität Daten.
        # Wir müssen AKTIV JSON-Pings senden um die Verbindung am Leben zu halten.
        # Intervall: Alle 45s (4x unter dem 5-Min Timeout, gibt 4 Chancen vor Disconnect)
        # ═══════════════════════════════════════════════════════════════════════════
        self._x10_account_ping_interval: Optional[float] = None  # Disable JSON keepalive pings; rely on protocol pings
        self._x10_account_keepalive_task: Optional[asyncio.Task] = None
        self._x10_account_last_pong_time = 0.0     # Track when we last received a pong
        self._x10_account_last_msg_time = 0.0      # Track any message (incl. pong) for health logs
        self._x10_balance_cache: dict = {}         # Latest balance snapshot
        
        # ═══════════════════════════════════════════════════════════════
        # X10 Account Message Type Counters (for health diagnostics)
        # ═══════════════════════════════════════════════════════════════
        self._x10_account_msg_counts: Dict[str, int] = {
            "ORDER": 0,
            "TRADE": 0,
            "POSITION": 0,
            "BALANCE": 0,
            "PONG": 0,
            "OTHER": 0,
        }
    
    def set_orderbook_provider(self, provider):
        """Set orderbook provider for invalidation on reconnect"""
        self._orderbook_provider = provider
        logger.info("📚 OrderbookProvider connected to WebSocketManager")
    
    def _on_websocket_reconnect(self, ws_name: str):
        """
        Handle WebSocket reconnection - invalidate orderbook caches.
        
        CRITICAL: After a WebSocket reconnection, we must:
        1. Clear ALL cached orderbook data (deltas are invalid without base)
        2. Set a cooldown period to ignore incoming deltas
        3. Wait for fresh REST snapshots before processing new deltas
        
        This prevents crossed book conditions caused by:
        - Stale delta data being applied to old base
        - Missing deltas during disconnect window
        - Out-of-order delta messages after reconnect
        
        Args:
            ws_name: Name of the WebSocket that reconnected
        """
        # Map WebSocket name to exchange
        if ws_name.startswith("lighter"):
            exchange = "lighter"
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: Clear Lighter orderbook caches immediately
            # ═══════════════════════════════════════════════════════════════
            self._invalidate_all_lighter_orderbooks()
        elif ws_name.startswith("x10"):
            exchange = "x10"
        else:
            exchange = None  # Invalidate all
            self._invalidate_all_lighter_orderbooks()
        
        logger.warning(
            f"🔄 [{ws_name}] Reconnected - cleared {exchange or 'all'} orderbook caches. "
            f"Fresh REST snapshots required before trading!"
        )
        
        # Invalidate orderbook provider caches and set cooldown
        if self._orderbook_provider:
            self._orderbook_provider.invalidate_all(
                reason=f"{ws_name} WebSocket reconnect",
                exchange=exchange
            )
        else:
            logger.warning(f"🔄 [{ws_name}] No OrderbookProvider set - manual cooldown not applied")
    
    def _on_health_change(self, ws_name: str, is_healthy: bool):
        """
        Handle WebSocket health state changes.
        
        Called when a connection transitions between healthy/unhealthy states.
        Useful for proactive monitoring, alerts, and trading decisions.
        
        Args:
            ws_name: Name of the WebSocket (lighter, x10_account, etc.)
            is_healthy: New health state
        """
        if is_healthy:
            logger.info(f"✅ [{ws_name}] Connection became healthy")
        else:
            logger.warning(f"⚠️ [{ws_name}] Connection became unhealthy")
        
        # Future: Can trigger alerts, pause trading, etc.
    
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
        
        CRITICAL: X10 Server disconnects with 1011 after ~5 minutes of inactivity.
        We send pings every 45s to ensure we stay well under this threshold.
        """
        if not self._x10_account_ping_interval:
            logger.info("💓 [x10_account] Keepalive loop disabled (using protocol ping_interval only)")
            return
        logger.info("💓 [x10_account] Keepalive loop started (interval={:.0f}s)".format(self._x10_account_ping_interval))
        
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
                    # X10 expects JSON ping format: {"ping": timestamp_ms}
                    timestamp_ms = int(time.time() * 1000)
                    ping_msg = {"ping": timestamp_ms}
                    success = await conn.send(ping_msg)
                    
                    if success:
                        # ═══════════════════════════════════════════════════════════════
                        # FIX: Update connection metrics so heartbeat shows correct counts
                        # ═══════════════════════════════════════════════════════════════
                        conn._metrics.pings_sent += 1
                        conn._metrics.last_ping_sent_time = time.time()
                        
                        # Calculate time since last pong for health monitoring
                        time_since_pong = time.time() - self._x10_account_last_pong_time if self._x10_account_last_pong_time > 0 else -1
                        
                        logger.info(
                            f"💓 [x10_account] Sent keepalive ping #{conn._metrics.pings_sent} "
                            f"(pongs_received={conn._metrics.pongs_received}, last_pong={time_since_pong:.0f}s ago)"
                        )
                    else:
                        logger.warning(f"⚠️ [x10_account] Failed to send keepalive ping")
                        
                except Exception as e:
                    logger.warning(f"⚠️ [x10_account] Ping failed: {e}")
                
                # Check connection health
                is_healthy = await self._check_x10_account_health()
                if not is_healthy:
                    logger.warning(f"⚠️ [x10_account] Health check failed - triggering reconnect...")
                    await self._reconnect_x10_account()
                else:
                    # Log message type statistics at DEBUG level (frequent)
                    total_msgs = sum(self._x10_account_msg_counts.values())
                    counts_str = ", ".join([f"{k}:{v}" for k, v in self._x10_account_msg_counts.items() if v > 0])
                    logger.debug(
                        f"💓 [x10_account] Health OK - msgs={total_msgs} ({counts_str or 'none'})"
                    )
                    
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
        effective_last_msg = max(
            last_msg,
            self._x10_account_last_pong_time,
            self._x10_account_last_msg_time,
        )
        
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
        
        # Log final message counts before reset
        total_msgs = sum(self._x10_account_msg_counts.values())
        counts_str = ", ".join([f"{k}:{v}" for k, v in self._x10_account_msg_counts.items() if v > 0])
        logger.info(f"📊 [x10_account] Pre-reconnect stats - Total: {total_msgs}, Types: {counts_str or 'none'}")
        
        # Reset message counters for fresh statistics
        for key in self._x10_account_msg_counts:
            self._x10_account_msg_counts[key] = 0
        
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
        
        X10 pong format: {"pong": timestamp_ms}
        Also treats any pong-style message as proof of life.
        
        IMPORTANT: This is called for pong responses to OUR keepalive pings.
        """
        # Check for explicit pong payload
        if "pong" in msg:
            self._x10_account_last_pong_time = time.time()
            pong_value = msg.get("pong")
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Update connection metrics so heartbeat shows correct counts
            # ═══════════════════════════════════════════════════════════════
            conn = self._connections.get("x10_account")
            if conn:
                conn._metrics.pongs_received += 1
                conn._metrics.last_pong_time = time.time()
                conn._metrics.missed_pongs = 0
                conn._set_health(True)
            
            # LOG at INFO level to verify pongs are being received
            logger.info(f"💓 [x10_account] Received pong #{conn._metrics.pongs_received if conn else '?'}: {pong_value}")
            return True
        
        # Fallback: legacy {"type": "pong"} format
        msg_type = msg.get("type", "")
        if msg_type == "pong":
            self._x10_account_last_pong_time = time.time()
            
            # FIX: Also update metrics for legacy format
            conn = self._connections.get("x10_account")
            if conn:
                conn._metrics.pongs_received += 1
                conn._metrics.last_pong_time = time.time()
                conn._metrics.missed_pongs = 0
                conn._set_health(True)
            
            logger.info(f"💓 [x10_account] Received type:pong #{conn._metrics.pongs_received if conn else '?'}")
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
        
        # 1. Create Lighter connection with enhanced 1006 handling
        # 
        # WICHTIG: Das TS SDK Pattern (ws-order-client.ts mit aktiven Client-Pings) gilt nur für 
        # den /jsonapi Order-Endpoint, NICHT für den /stream Market-Data-Endpoint!
        # 
        # Für /stream (Market Data):
        # - Der SERVER sendet uns {"type":"ping"} alle ~60-90s
        # - WIR antworten mit {"type":"pong"} (automatisch in _process_single_message)
        # - Wir senden KEINE eigenen Pings (Server antwortet nicht darauf!)
        # 
        # Die Connection bleibt aktiv durch:
        # 1. Unsere Pong-Antworten auf Server-Pings
        # 2. Die kontinuierlichen Market-Daten (Orderbook, Prices, etc.)
        # ═══════════════════════════════════════════════════════════════════════════
        lighter_ping_interval = None  # Keine WebSocket-Protokoll-Pings
        lighter_ping_timeout = None
        
        # Load Lighter-specific settings from config with fallbacks
        # WICHTIG: json_ping_interval=None da /stream-Endpoint nicht auf Client-Pings antwortet!
        lighter_json_ping_interval = None   # DEAKTIVIERT - Server sendet UNS Pings!
        lighter_json_pong_timeout = 120.0   # 120s - Server pingt alle ~60-90s, relaxed threshold
        lighter_1006_extended_delay = getattr(config, 'WS_1006_EXTENDED_DELAY', 30)
        lighter_1006_threshold = getattr(config, 'WS_1006_ERROR_THRESHOLD', 3)
        lighter_ping_on_connect = False     # DEAKTIVIERT - Server startet Ping/Pong!
        
        lighter_config = WSConfig(
            url=self.LIGHTER_WS_URL,
            name="lighter",
            ping_interval=lighter_ping_interval,   # None - we manage JSON pings ourselves
            ping_timeout=lighter_ping_timeout,     # None - no protocol-level ping timeout
            reconnect_delay_initial=2.0,           # Fast initial reconnect attempt
            reconnect_delay_max=120.0,             # Allow server time to recover
            max_consecutive_1006=lighter_1006_threshold,   # From config (default 3)
            extended_delay_1006=lighter_1006_extended_delay,  # From config (default 30s)
            health_check_interval=30.0,            # Check health every 30s
            # ═══════════════════════════════════════════════════════════════════════
            # LIGHTER /stream: Passive mode - Server pings us, we respond with pong
            # ═══════════════════════════════════════════════════════════════════════
            json_ping_interval=lighter_json_ping_interval,  # None - we wait for server pings
            json_pong_timeout=lighter_json_pong_timeout,    # 120s - relaxed threshold for warnings
            send_ping_on_connect=lighter_ping_on_connect,   # False - server initiates ping/pong
        )
        self._connections["lighter"] = ManagedWebSocket(
            lighter_config, 
            self._handle_message,
            on_reconnect=self._on_websocket_reconnect,  # Invalidate orderbooks on reconnect
            on_health_change=self._on_health_change     # H2: Health state callbacks
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
            ping_interval=15.0,              # Client sends pings actively
            ping_timeout=None,               # Ignore missing pongs (prevents client-side 1011)
            reconnect_delay_initial=2.0,     # Fast reconnect
            reconnect_delay_max=120.0,       # Allow server recovery time
            max_consecutive_1006=5,          # Extended delay after 5 consecutive 1006s
            extended_delay_1006=30.0,        # 30s extended delay
            headers=x10_headers,
        )
        self._connections["x10_account"] = ManagedWebSocket(
            x10_account_config,
            self._handle_message,
            on_reconnect=self._on_websocket_reconnect,  # Invalidate orderbooks on reconnect
            on_pong=self._handle_x10_account_pong,      # Update manager-level pong tracking
            on_health_change=self._on_health_change    # H2: Health state callbacks
        )
        
        # 3. X10 TRADES Connection (Public Firehose)
        # Delivers trades for ALL markets automatically (no subscribe needed)
        x10_trades_config = WSConfig(
            url=self.X10_TRADES_WS_URL,  # PUBLIC TRADES URL
            name="x10_trades",
            ping_interval=15.0,
            ping_timeout=None,
            reconnect_delay_initial=2.0,
            reconnect_delay_max=120.0,
            max_consecutive_1006=5,
            extended_delay_1006=30.0,
            headers=x10_headers,
        )
        self._connections["x10_trades"] = ManagedWebSocket(
            x10_trades_config,
            self._handle_message,
            on_reconnect=self._on_websocket_reconnect,  # Invalidate orderbooks on reconnect
            on_health_change=self._on_health_change    # H2: Health state callbacks
        )
        
        # 4. X10 FUNDING Connection (Public Firehose)
        # Delivers funding rates for ALL markets automatically (no subscribe needed)
        x10_funding_config = WSConfig(
            url=self.X10_FUNDING_WS_URL,  # PUBLIC FUNDING URL
            name="x10_funding",
            ping_interval=15.0,
            ping_timeout=None,
            reconnect_delay_initial=2.0,
            reconnect_delay_max=120.0,
            max_consecutive_1006=5,
            extended_delay_1006=30.0,
            headers=x10_headers,
        )
        self._connections["x10_funding"] = ManagedWebSocket(
            x10_funding_config,
            self._handle_message,
            on_reconnect=self._on_websocket_reconnect,  # Invalidate orderbooks on reconnect
            on_health_change=self._on_health_change    # H2: Health state callbacks
        )

        # 5. X10 ORDERBOOK Connection (Public, Delta Updates)
        x10_orderbook_config = WSConfig(
            url="wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks",
            name="x10_orderbooks", 
            ping_interval=15.0,
            ping_timeout=None,
            reconnect_delay_initial=2.0,
            reconnect_delay_max=120.0,
            max_consecutive_1006=5,
            extended_delay_1006=30.0,
            headers=x10_headers,
        )
        self._connections["x10_orderbooks"] = ManagedWebSocket(
            x10_orderbook_config,
            self._handle_message,
            on_reconnect=self._on_websocket_reconnect,  # Invalidate orderbooks on reconnect
            on_health_change=self._on_health_change    # H2: Health state callbacks
        )

        
        # Start all connections (use return_exceptions to prevent "exception was never retrieved")
        await asyncio.gather(*[
            conn.start() for conn in self._connections.values()
        ], return_exceptions=True)
        
        # Start X10 account keepalive loop (dedicated health monitoring) if enabled
        if self._x10_account_ping_interval:
            self._x10_account_keepalive_task = asyncio.create_task(
                self._x10_account_keepalive_loop(),
                name="x10_account_keepalive"
            )
        
        logger.info("✅ WebSocketManager started (Lighter + X10 Account/Trades/Funding + Keepalive)")
    
    async def stop(self):
        """Stop all WebSocket connections"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Prevent duplicate stop calls during shutdown
        # ═══════════════════════════════════════════════════════════════
        if hasattr(self, '_stopped') and self._stopped:
            return
        self._stopped = True
        
        self._running = False
        
        # Stop X10 account keepalive task first
        if self._x10_account_keepalive_task and not self._x10_account_keepalive_task.done():
            self._x10_account_keepalive_task.cancel()
            try:
                await asyncio.wait_for(self._x10_account_keepalive_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        
        await asyncio.gather(*[
            conn.stop() for conn in self._connections.values()
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
        """Subscribe to market data for given symbols
        
        OPTION 3 IMPLEMENTATION:
        - order_book/{market_id} for ALL symbols (real-time orderbook updates)
        - market_stats/all for prices, funding rates, OI (1 subscription for ALL markets)
        - NO individual trade/{market_id} subscriptions (saves 50% subscriptions)
        
        Lighter WebSocket Limits (https://apidocs.lighter.xyz/docs/rate-limits):
        - Max 100 subscriptions per connection
        - Max 1000 total subscriptions per IP
        
        With this approach:
        - 65 order_book subscriptions + 1 market_stats/all = 66 total < 100 ✅
        """
        lighter_conn = self._connections.get("lighter")
        if lighter_conn:
            # Subscribe to market_stats/all FIRST (1 subscription)
            # This provides: last_trade_price, funding_rate, mark_price, index_price, 
            # open_interest for ALL markets in a single feed
            await lighter_conn.subscribe("market_stats/all")
            logger.info("📊 [lighter] Subscribed to market_stats/all (prices, funding, OI for all markets)")
            
            # Subscribe to orderbooks for ALL symbols (no limit needed with Option 3)
            # Each order_book subscription = 1 subscription
            subscribed_count = 0
            for symbol in symbols:
                market_id = self._get_lighter_market_id(symbol)
                if market_id is not None:
                    await lighter_conn.subscribe(f"order_book/{market_id}")
                    subscribed_count += 1
                    # Pace subscriptions to avoid overwhelming the server
                    if subscribed_count % 10 == 0:
                        await asyncio.sleep(0.05)
            
            total_subs = 1 + subscribed_count  # market_stats/all + order_books
            logger.info(
                f"✅ [lighter] Subscribed to {total_subs} channels "
                f"(1 market_stats/all + {subscribed_count} order_books)"
            )
            
            if total_subs >= 95:
                logger.warning(
                    f"⚠️ [lighter] Approaching subscription limit: {total_subs}/100"
                )
            
            # NOTE: Lighter WS orderbook processing is DISABLED
            # We rely on REST polling for orderbooks to avoid crossed books
            # WS is still used for: prices, funding rates, market stats
            logger.info(f"ℹ️ [lighter] Orderbook data: REST polling only (WS deltas disabled)")
        
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
                # Track any account-stream message (including pongs) for health logging
                self._x10_account_last_msg_time = time.time()
                
                # Extract message type for routing and stats
                msg_type = (msg.get("type") or msg.get("e") or msg.get("event") or "UNKNOWN").upper()
                
                # ═══════════════════════════════════════════════════════════════
                # FIX #11: Position Entry Price Logging unvollständig
                # Log vollständige POSITION Messages (nicht abgeschnitten)
                # ═══════════════════════════════════════════════════════════════
                if msg_type == "POSITION":
                    # Log vollständige POSITION Message mit Pretty Print
                    import json
                    try:
                        msg_json = json.dumps(msg, indent=2, default=str)
                        logger.debug(f"📨 [x10_account] RAW: POSITION - {msg_json}")
                    except Exception as e:
                        # Fallback auf str() wenn json.dumps fehlschlägt
                        logger.debug(f"📨 [x10_account] RAW: POSITION - {str(msg)}")
                else:
                    # Für andere Message-Typen: Begrenzung auf 200 Zeichen (um Log-Spam zu vermeiden)
                    logger.debug(f"📨 [x10_account] RAW: {msg_type} - {str(msg)[:200]}")
                
                # Update message type counter for health diagnostics
                if msg_type in self._x10_account_msg_counts:
                    self._x10_account_msg_counts[msg_type] += 1
                else:
                    self._x10_account_msg_counts["OTHER"] += 1
                
                # Check for pong response first (keepalive health tracking)
                if self._handle_x10_account_pong(msg):
                    self._x10_account_msg_counts["PONG"] += 1
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
        stats = msg.get("market_stats")
        if not stats:
            return

        # Normalize to list to support market_stats/all payloads.
        entries = stats if isinstance(stats, list) else [stats]

        if self.lighter_adapter:
            self.lighter_adapter.mark_ws_market_stats()

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            market_id = (
                entry.get("market_id")
                or entry.get("marketId")
                or entry.get("market_index")
                or entry.get("marketIndex")
            )
            if market_id is None:
                continue

            # Find symbol for market ID
            symbol = self._lighter_market_id_to_symbol(market_id)
            if not symbol:
                continue

            # Update adapter caches
            if self.lighter_adapter:
                # Mark price
                mark_price = entry.get("mark_price")
                if mark_price:
                    self.lighter_adapter._price_cache[symbol] = float(mark_price)
                    self.lighter_adapter._price_cache_time[symbol] = time.time()
                
                # Funding rate
                funding_rate = entry.get("current_funding_rate") or entry.get("funding_rate")
                if funding_rate:
                    self.lighter_adapter._funding_cache[symbol] = float(funding_rate) / 100
                    self.lighter_adapter._funding_cache_time[symbol] = time.time()
            
            # Open interest
            open_interest = stats.get("open_interest")
            if open_interest and self.oi_tracker:
                self. oi_tracker. update_from_websocket(symbol, "lighter", float(open_interest))
    
    async def _handle_lighter_orderbook(self, msg: dict):
        """Process Lighter orderbook update - DISABLED
        
        ═══════════════════════════════════════════════════════════════════════
        WebSocket orderbook processing is DISABLED to prevent crossed books.
        
        The WS delta approach caused consistent crossed orderbook issues due to:
        1. Race conditions between WS deltas and REST snapshots
        2. Deltas arriving faster than processing speed
        3. No sequence guarantee in WS messages
        
        We now rely EXCLUSIVELY on REST polling via fetch_orderbook().
        WS is still used for: prices, funding rates, market stats.
        ═══════════════════════════════════════════════════════════════════════
        """
        # DISABLED - REST polling only for orderbooks
        # Simply return without processing to avoid any orderbook corruption
        return
    
    def _invalidate_lighter_orderbook(self, symbol: str):
        """
        Invalidate a single symbol's orderbook cache after crossed book detection.
        
        This clears the cached orderbook data so that the next validation
        will trigger a REST fallback fetch.
        """
        if self.lighter_adapter:
            # Clear both caches
            self.lighter_adapter._orderbook_cache.pop(symbol, None)
            self.lighter_adapter.orderbook_cache.pop(symbol, None)
            self.lighter_adapter._orderbook_cache_time.pop(symbol, None)
            logger.info(f"🗑️ [{symbol}] Orderbook cache invalidated due to crossed book")
    
    def _invalidate_all_lighter_orderbooks(self):
        """
        Invalidate ALL Lighter orderbook caches.
        
        Called on WebSocket reconnect to ensure we start fresh with
        REST snapshots instead of accumulating potentially stale deltas.
        """
        if self.lighter_adapter:
            symbols = list(self.lighter_adapter._orderbook_cache.keys())
            self.lighter_adapter._orderbook_cache.clear()
            self.lighter_adapter.orderbook_cache.clear()
            self.lighter_adapter._orderbook_cache_time.clear()
            logger.warning(f"🗑️ All Lighter orderbooks invalidated ({len(symbols)} symbols)")
    
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
        
        # ═══════════════════════════════════════════════════════════════
        # CROSSED BOOK CHECK AFTER MERGE
        # ═══════════════════════════════════════════════════════════════
        if sorted_bids and sorted_asks:
            best_bid = sorted_bids[0][0]
            best_ask = sorted_asks[0][0]
            if best_ask <= best_bid:
                logger.warning(
                    f"⚠️ CROSSED BOOK after fallback merge for {symbol}: "
                    f"ask={best_ask} <= bid={best_bid} - invalidating"
                )
                self._invalidate_lighter_orderbook(symbol)
                return
        
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
        
        X10 Account Stream Message Format (from API docs):
        {
            "type": "ORDER" | "TRADE" | "BALANCE" | "POSITION",
            "data": { ... },
            "ts": 1715885884837,
            "seq": 1
        }
        
        Alternative formats that may be used:
        - "e" field instead of "type" (Binance-style)
        - "event" field
        - Nested "data" object or flat structure
        """
        # ═══════════════════════════════════════════════════════════════
        # FLEXIBLE MESSAGE TYPE DETECTION
        # X10 may use different field names: type, e, event, channel
        # ═══════════════════════════════════════════════════════════════
        msg_type = (
            msg.get("type") or 
            msg.get("e") or 
            msg.get("event") or 
            msg.get("channel") or 
            ""
        ).upper()
        
        # ═══════════════════════════════════════════════════════════════
        # ACCOUNT STREAM MESSAGE TYPES (x10_account connection)
        # These messages come automatically after authentication!
        # ═══════════════════════════════════════════════════════════════
        
        # Order updates (new, filled, cancelled, etc.)
        # Possible type values: ORDER, order, ORDERS, orders, ORDER_UPDATE, orderUpdate
        if msg_type in ["ORDER", "ORDERS", "ORDER_UPDATE", "ORDERUPDATE"]:
            await self._handle_x10_order_update(msg)
            return
        
        # Trade/fill notifications (CRITICAL for position tracking!)
        # Possible type values: TRADE, trade, TRADES, trades, FILL, fill, EXECUTION
        if msg_type in ["TRADE", "TRADES", "FILL", "FILLS", "EXECUTION"]:
            await self._handle_x10_trade_notification(msg)
            return
        
        # Balance updates
        # Possible type values: BALANCE, balance, ACCOUNT, account, BALANCE_UPDATE
        if msg_type in ["BALANCE", "ACCOUNT", "BALANCE_UPDATE", "BALANCEUPDATE"]:
            await self._handle_x10_balance_update(msg)
            return
        
        # Position updates
        # Possible type values: POSITION, position, POSITIONS, positions, POSITION_UPDATE
        if msg_type in ["POSITION", "POSITIONS", "POSITION_UPDATE", "POSITIONUPDATE"]:
            await self._handle_x10_position_update(msg)
            return
        
        # ═══════════════════════════════════════════════════════════════
        # ALTERNATIVE: Check if "data" contains typed objects
        # Some APIs send {"data": {"orders": [...], "positions": [...], ...}}
        # ═══════════════════════════════════════════════════════════════
        data = msg.get("data", {})
        if isinstance(data, dict):
            # Check for orders array
            if "orders" in data and data["orders"]:
                logger.info(f"📋 [x10_account] Found orders array in data: {len(data['orders'])} orders")
                await self._handle_x10_order_update(msg)
                # Don't return - might have multiple arrays
            
            # Check for trades array
            if "trades" in data and data["trades"]:
                logger.info(f"💰 [x10_account] Found trades array in data: {len(data['trades'])} trades")
                await self._handle_x10_trade_notification(msg)
            
            # Check for positions array
            if "positions" in data and data["positions"]:
                logger.info(f"📊 [x10_account] Found positions array in data: {len(data['positions'])} positions")
                await self._handle_x10_position_update(msg)
            
            # Check for balance object
            if "balance" in data and data["balance"]:
                logger.info(f"💰 [x10_account] Found balance in data")
                await self._handle_x10_balance_update(msg)
        
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
        
        # ═══════════════════════════════════════════════════════════════
        # UNKNOWN MESSAGE TYPE - Log for analysis
        # ═══════════════════════════════════════════════════════════════
        if msg_type and msg_type not in ["PONG", "PING", "HEARTBEAT", "SUBSCRIBED", "MP"]:
            logger.debug(f"[x10_account] Unhandled message type '{msg_type}': {str(msg)[:200]}")
    
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
        
        Alternative formats:
        - Direct order object (not wrapped in "data")
        - Single order (not array)
        - Different field names (orderId, symbol, quantity, etc.)
        """
        try:
            # Extract orders - handle multiple possible structures
            data = msg.get("data", msg)  # Fallback to msg itself if no "data" key
            
            # Orders could be in various places
            orders = (
                data.get("orders") or 
                data.get("order") or 
                (data if "id" in data or "orderId" in data else None) or
                []
            )
            
            # Ensure it's a list
            if isinstance(orders, dict):
                orders = [orders]
            elif not orders:
                orders = []
            
            for order in orders:
                # Flexible field extraction
                market = order.get("market") or order.get("symbol") or order.get("m") or ""
                status = order.get("status") or order.get("orderStatus") or order.get("s") or ""
                order_id = order.get("id") or order.get("orderId") or order.get("order_id") or order.get("i")
                side = order.get("side") or order.get("orderSide") or ""
                order_price = order.get("price") or order.get("p") or "0"
                qty = order.get("qty") or order.get("quantity") or order.get("amount") or order.get("q") or "0"
                filled_qty = order.get("filledQty") or order.get("filled_qty") or order.get("executedQty") or order.get("fq") or "0"

                # Some streams include an average fill price; keep order price separate from fill price.
                avg_fill_price = (
                    order.get("avgFillPrice")
                    or order.get("avg_fill_price")
                    or order.get("avgPrice")
                    or order.get("averagePrice")
                    or order.get("fillPrice")
                )
                avg_fill_str = ""
                try:
                    afp = float(avg_fill_price) if avg_fill_price is not None else 0.0
                    if afp > 0:
                        avg_fill_str = f" avgFill=${afp}"
                except Exception:
                    avg_fill_str = ""
                
                # Log with emoji based on status
                status_upper = str(status).upper()
                emoji = "📋"
                if status_upper == "FILLED":
                    emoji = "✅"
                elif status_upper == "CANCELLED":
                    emoji = "❌"
                elif status_upper == "NEW":
                    emoji = "🆕"
                elif "PARTIAL" in status_upper:
                    emoji = "⏳"
                
                logger.info(
                    f"{emoji} [x10_account] ORDER UPDATE: {market} {side} {status} "
                    f"qty={qty} filled={filled_qty} orderPrice=${order_price}{avg_fill_str} (id={order_id})"
                )
                
                # Notify X10 Adapter if available
                if hasattr(self, 'x10_adapter') and self.x10_adapter:
                    if hasattr(self.x10_adapter, 'on_order_update'):
                        try:
                            await self.x10_adapter.on_order_update(order)
                        except Exception as adapter_err:
                            logger.debug(f"[x10_account] Adapter order callback error: {adapter_err}")
                
        except Exception as e:
            logger.error(f"[x10_account] Order update error: {e}", exc_info=True)

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
        
        Alternative formats:
        - Direct trade object (not wrapped in "data")
        - Single trade (not array)
        - Different field names (tradeId, symbol, quantity, commission, etc.)
        """
        try:
            # Extract trades - handle multiple possible structures
            data = msg.get("data", msg)  # Fallback to msg itself if no "data" key
            
            # Trades could be in various places
            trades = (
                data.get("trades") or 
                data.get("trade") or 
                data.get("fills") or
                data.get("fill") or
                data.get("executions") or
                (data if "tradeId" in data or "trade_id" in data or ("id" in data and "price" in data) else None) or
                []
            )
            
            # Ensure it's a list
            if isinstance(trades, dict):
                trades = [trades]
            elif not trades:
                trades = []
            
            for trade in trades:
                # Flexible field extraction
                market = trade.get("market") or trade.get("symbol") or trade.get("m") or ""
                side = trade.get("side") or trade.get("orderSide") or trade.get("s") or ""
                trade_id = trade.get("id") or trade.get("tradeId") or trade.get("trade_id") or trade.get("t")
                order_id = trade.get("orderId") or trade.get("order_id") or trade.get("oid")
                
                # Parse numeric fields safely
                try:
                    price = float(trade.get("price") or trade.get("p") or 0)
                except (ValueError, TypeError):
                    price = 0.0
                
                try:
                    qty = float(trade.get("qty") or trade.get("quantity") or trade.get("amount") or trade.get("q") or 0)
                except (ValueError, TypeError):
                    qty = 0.0
                
                try:
                    fee = float(trade.get("fee") or trade.get("commission") or trade.get("f") or 0)
                except (ValueError, TypeError):
                    fee = 0.0
                
                is_taker = trade.get("isTaker", trade.get("is_taker", trade.get("taker", True)))
                
                # Log with prominent emoji - this is a CRITICAL event!
                logger.info(
                    f"💰💰💰 [x10_account] FILL/TRADE: {market} {side} {qty} @ ${price:.4f} "
                    f"(Trade: {trade_id}, Order: {order_id}, Fee: ${fee:.4f}, Taker: {is_taker})"
                )
                
                # Notify X10 Adapter if available
                if hasattr(self, 'x10_adapter') and self.x10_adapter:
                    if hasattr(self.x10_adapter, 'on_fill_update'):
                        try:
                            await self.x10_adapter.on_fill_update(trade)
                        except Exception as adapter_err:
                            logger.debug(f"[x10_account] Adapter fill callback error: {adapter_err}")
                
                # Notify fill callbacks (for parallel_execution)
                if hasattr(self, '_fill_callbacks'):
                    symbol = market.replace("/", "-")
                    for callback in self._fill_callbacks.get(symbol, []):
                        try:
                            await callback(trade)
                        except Exception as cb_err:
                            logger.error(f"Fill callback error: {cb_err}")
                            
        except Exception as e:
            logger.error(f"[x10_account] Trade notification error: {e}", exc_info=True)

    async def _handle_x10_balance_update(self, msg: dict):
        """Process X10 balance update from account stream.
        
        Message format:
        {
            "type": "BALANCE",
            "data": {
                "balance": {
                    "equity": "10500.00",
                    "availableForTrade": "10000.00",
                    "unrealisedPnl": "500.00",
                    ...
                }
            }
        }
        """
        try:
            # Extract balance - handle multiple possible structures
            data = msg.get("data", msg)
            
            # Balance could be nested or direct
            balance = (
                data.get("balance") or 
                data.get("account") or 
                data.get("balances") or
                (data if "equity" in data or "available" in data else None) or
                {}
            )
            
            if balance:
                # Flexible field extraction
                equity = balance.get("equity") or balance.get("totalEquity") or balance.get("e") or "0"
                available = (
                    balance.get("availableForTrade") or 
                    balance.get("available") or 
                    balance.get("availableBalance") or 
                    balance.get("free") or 
                    balance.get("a") or 
                    "0"
                )
                unrealized_pnl = (
                    balance.get("unrealisedPnl") or 
                    balance.get("unrealizedPnl") or 
                    balance.get("uPnl") or 
                    balance.get("pnl") or 
                    "0"
                )
                margin_used = balance.get("marginUsed") or balance.get("usedMargin") or balance.get("m") or "0"
                
                # Log at DEBUG level (balance updates are frequent)
                logger.debug(
                    f"💰 [x10_account] BALANCE: equity=${equity}, "
                    f"available=${available}, uPnL=${unrealized_pnl}"
                )
                
                # Cache balance for health monitoring
                self._x10_balance_cache = balance
                
        except Exception as e:
            logger.error(f"[x10_account] Balance update error: {e}", exc_info=True)

    async def _handle_x10_position_update(self, msg: dict):
        """Process X10 position update from account stream.
        
        Message format:
        {
            "type": "POSITION",
            "data": {
                "positions": [{
                    "market": "BTC-USD",
                    "side": "LONG" | "SHORT",
                    "size": "0.5",
                    "entryPrice": "58000.00",
                    "markPrice": "58500.00",
                    "unrealisedPnl": "250.00",
                    ...
                }]
            }
        }
        """
        try:
            # Extract positions - handle multiple possible structures
            data = msg.get("data", msg)
            
            # Positions could be in various places
            positions = (
                data.get("positions") or 
                data.get("position") or 
                (data if "market" in data or "symbol" in data else None) or
                []
            )
            
            # Ensure it's a list
            if isinstance(positions, dict):
                positions = [positions]
            elif not positions:
                positions = []
            
            for pos in positions:
                # ═══════════════════════════════════════════════════════════════
                # FIX #11: Log wichtige Position-Felder einzeln für Debugging
                # ═══════════════════════════════════════════════════════════════
                # Log alle verfügbaren Felder für Debugging
                import json
                try:
                    pos_fields = {k: v for k, v in pos.items()}
                    logger.debug(f"📊 [x10_account] POSITION FIELDS: {json.dumps(pos_fields, indent=2, default=str)}")
                except Exception as e:
                    logger.debug(f"📊 [x10_account] POSITION FIELDS (fallback): {str(pos)}")
                
                # Flexible field extraction
                market = pos.get("market") or pos.get("symbol") or pos.get("m") or ""
                side = pos.get("side") or pos.get("positionSide") or pos.get("s") or ""
                size = pos.get("size") or pos.get("quantity") or pos.get("qty") or pos.get("q") or "0"
                status = pos.get("status", "UNKNOWN")
                
                # ═══════════════════════════════════════════════════════════════
                # FIX #14: Entry Price Logging für CLOSED Positions
                # Extract openPrice also for CLOSED positions (for logging purposes)
                # ═══════════════════════════════════════════════════════════════
                entry_price = (
                    pos.get("entryPrice") or 
                    pos.get("entry_price") or 
                    pos.get("avgPrice") or 
                    pos.get("ep") or 
                    pos.get("open_price") or 
                    pos.get("openPrice") or  # Fix #14: Also check openPrice for CLOSED positions
                    pos.get("avgEntryPrice") or 
                    pos.get("averageEntryPrice") or 
                    "0"
                )
                
                mark_price = pos.get("markPrice") or pos.get("mark_price") or pos.get("mp") or "0"
                unrealized_pnl = pos.get("unrealisedPnl") or pos.get("unrealizedPnl") or pos.get("uPnl") or pos.get("pnl") or "0"
                leverage = pos.get("leverage") or pos.get("lev") or ""
                liquidation_price = pos.get("liquidationPrice") or pos.get("liqPrice") or ""
                
                # ═══════════════════════════════════════════════════════════════
                # IMPROVEMENT: Call adapter first to update entry price in data dict
                # Then log with corrected entry price
                # ═══════════════════════════════════════════════════════════════
                # Notify X10 Adapter if available (this will update entry_price in pos dict)
                if hasattr(self, 'x10_adapter') and self.x10_adapter:
                    if hasattr(self.x10_adapter, 'on_position_update'):
                        try:
                            await self.x10_adapter.on_position_update(pos)
                            # Re-extract entry_price after adapter update (may have been corrected)
                            # Fix #14: Also check openPrice for CLOSED positions after adapter update
                            entry_price = (
                                pos.get("entryPrice") or 
                                pos.get("entry_price") or 
                                pos.get("open_price") or 
                                pos.get("openPrice") or  # Fix #14: Also check openPrice for CLOSED positions
                                pos.get("avgPrice") or 
                                pos.get("avgEntryPrice") or 
                                pos.get("averageEntryPrice") or 
                                entry_price
                            )
                        except Exception as adapter_err:
                            logger.debug(f"[x10_account] Adapter position callback error: {adapter_err}")
                
                # Fix #14: For CLOSED positions, ensure we use openPrice if entry_price is still 0
                if status == "CLOSED" and (not entry_price or float(entry_price) == 0.0):
                    open_price = pos.get("openPrice") or pos.get("open_price") or "0"
                    if open_price and float(open_price) > 0:
                        entry_price = open_price
                        logger.debug(f"[x10_account] Fix #14: Using openPrice={open_price} for CLOSED position {market}")
                
                # Determine emoji based on PnL
                try:
                    pnl_val = float(unrealized_pnl)
                    emoji = "📈" if pnl_val >= 0 else "📉"
                except (ValueError, TypeError):
                    emoji = "📊"
                
                # Log AFTER adapter callback so entry_price is corrected
                logger.info(
                    f"{emoji} [x10_account] POSITION UPDATE: {market} {side} size={size} "
                    f"entry=${entry_price} mark=${mark_price} uPnL=${unrealized_pnl}"
                )
                
        except Exception as e:
            logger.error(f"[x10_account] Position update error: {e}", exc_info=True)

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
        """Get status of all connections with enhanced error tracking"""
        return {
            name: {
                'state': conn.state.value,
                'connected': conn.is_connected,
                'is_healthy': conn.metrics.is_healthy,
                'metrics': {
                    'messages_received': conn.metrics.messages_received,
                    'messages_sent': conn.metrics.messages_sent,
                    'reconnect_count': conn.metrics.reconnect_count,
                    'uptime_seconds': conn.metrics.uptime_seconds,
                    'last_error': conn.metrics.last_error,
                    'last_error_code': conn.metrics.last_error_code,
                    'error_counts': dict(conn.metrics.error_counts),  # Track errors by code
                    'last_pong_time': conn.metrics.last_pong_time,
                    # Enhanced 1006 prevention metrics
                    'pings_sent': conn.metrics.pings_sent,
                    'pongs_received': conn.metrics.pongs_received,
                    'missed_pongs': conn.metrics.missed_pongs,
                    'last_ping_sent_time': conn.metrics.last_ping_sent_time,
                }
            }
            for name, conn in self._connections.items()
        }
    
    def is_healthy(self) -> bool:
        """Check if all connections are healthy (connected and responding)"""
        for conn in self._connections.values():
            if not conn.is_connected:
                return False
            # Also check if connection is marked as healthy (receiving messages/pongs)
            if hasattr(conn.metrics, 'is_healthy') and not conn.metrics.is_healthy:
                return False
        return True


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
    
    # ═══════════════════════════════════════════════════════════════
    # Initialize and connect OrderbookProvider for auto-invalidation
    # ═══════════════════════════════════════════════════════════════
    try:
        from src.data.orderbook_provider import init_orderbook_provider
        provider = init_orderbook_provider(lighter_adapter, x10_adapter, manager)
        manager.set_orderbook_provider(provider)
        logger.info("✅ OrderbookProvider initialized and connected to WebSocketManager")
    except Exception as e:
        logger.warning(f"⚠️ Could not initialize OrderbookProvider: {e}")
    
    await manager.start(ping_interval=ping_interval, ping_timeout=ping_timeout)
    
    if symbols:
        await manager.subscribe_market_data(symbols)
    
    return manager
