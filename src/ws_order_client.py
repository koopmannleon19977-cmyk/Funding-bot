"""
WebSocket Order Client for Lighter Exchange

Sends orders via WebSocket instead of REST API for lower latency.
Pattern from: lighter-ts-main/src/api/ws-order-client.ts

B1 Implementation - Phase 3 Big Rewrites

WebSocket Endpoint: wss://mainnet.zklighter.elliot.ai/stream
  (Uses same /stream endpoint as market data - /jsonapi is not available)
  
Message Format:
    Send: {"type": "jsonapi/sendtx", "data": {"id": "req_123", "tx_type": 14, "tx_info": {...}}}
    Recv: {"id": "req_123", "hash": "...", "status": 3, ...} or {"error": {...}}

Features:
- Automatic reconnection with exponential backoff
- Heartbeat (PING/PONG)
- Request timeout handling
- REST fallback on WS failure
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from enum import IntEnum

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)


class TransactionType(IntEnum):
    """Lighter transaction types"""
    CREATE_ORDER = 14
    CANCEL_ORDER = 15
    CANCEL_ALL_ORDERS = 16


@dataclass
class WsOrderConfig:
    """WebSocket Order Client configuration"""
    # Uses /stream endpoint (same as market data) - /jsonapi returns 404
    url: str = "wss://mainnet.zklighter.elliot.ai/stream"
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0
    request_timeout: float = 10.0


@dataclass
class WsTransaction:
    """Response from WebSocket order submission"""
    hash: str
    tx_type: int
    status: int
    nonce: int
    account_index: int
    block_height: int = 0
    queued_at: int = 0
    executed_at: int = 0
    error: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WsTransaction':
        return cls(
            hash=data.get('hash', ''),
            tx_type=data.get('type', 0),
            status=data.get('status', 0),
            nonce=data.get('nonce', 0),
            account_index=data.get('account_index', 0),
            block_height=data.get('block_height', 0),
            queued_at=data.get('queued_at', 0),
            executed_at=data.get('executed_at', 0),
            error=data.get('error')
        )


@dataclass
class PendingRequest:
    """Pending WebSocket request awaiting response"""
    future: asyncio.Future
    timestamp: float
    timeout_task: Optional[asyncio.Task] = None


class WebSocketOrderClient:
    """
    WebSocket client for submitting orders to Lighter exchange.
    
    Provides lower latency than REST API (~50-100ms improvement).
    Falls back to REST on connection failure.
    """
    
    def __init__(self, config: Optional[WsOrderConfig] = None):
        self.config = config or WsOrderConfig()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._is_connected = False
        self._reconnect_attempts = 0
        self._message_id = 0
        self._pending_requests: Dict[str, PendingRequest] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._on_health_change: Optional[Callable[[bool], None]] = None
        self._lock = asyncio.Lock()
        self._running = False
        
    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._ws is not None
        
    def set_health_callback(self, callback: Callable[[bool], None]):
        """Set callback for connection health changes"""
        self._on_health_change = callback
        
    def _set_health(self, healthy: bool):
        """Update health status and notify callback"""
        old_health = self._is_connected
        self._is_connected = healthy
        if old_health != healthy and self._on_health_change:
            try:
                self._on_health_change(healthy)
            except Exception as e:
                logger.warning(f"[WS-ORDER] Health callback error: {e}")
    
    async def _cleanup_tasks(self):
        """Cancel existing background tasks to prevent duplicate recv() calls"""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None
        
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None
                
    async def connect(self) -> bool:
        """
        Connect to WebSocket endpoint.
        
        Returns True if connection successful, False otherwise.
        """
        if self._is_connected:
            return True
            
        # Cancel any existing tasks first to avoid duplicate recv() calls
        await self._cleanup_tasks()
            
        try:
            logger.info(f"[WS-ORDER] Connecting to {self.config.url}...")
            
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.config.url,
                    ping_interval=None,  # We handle our own heartbeat
                    ping_timeout=None,
                    close_timeout=5
                ),
                timeout=self.config.request_timeout
            )
            
            self._set_health(True)
            self._reconnect_attempts = 0
            self._running = True
            
            # Start background tasks
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            logger.info(f"✅ [WS-ORDER] Connected to {self.config.url}")
            return True
            
        except asyncio.TimeoutError:
            logger.warning(f"[WS-ORDER] Connection timeout to {self.config.url}")
            return False
        except Exception as e:
            logger.error(f"[WS-ORDER] Connection failed: {e}")
            return False
            
    async def disconnect(self):
        """Disconnect from WebSocket"""
        self._running = False
        
        # Cancel background tasks
        for task in [self._heartbeat_task, self._receive_task, self._reconnect_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                    
        # Reject all pending requests
        for req_id, pending in list(self._pending_requests.items()):
            if pending.timeout_task:
                pending.timeout_task.cancel()
            if not pending.future.done():
                pending.future.set_exception(Exception("WebSocket disconnected"))
        self._pending_requests.clear()
        
        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            
        self._set_health(False)
        logger.info("[WS-ORDER] Disconnected")
        
    async def _receive_loop(self):
        """Background task to receive and process messages"""
        while self._running and self._ws:
            try:
                message = await self._ws.recv()
                await self._handle_message(message)
            except ConnectionClosed as e:
                logger.warning(f"[WS-ORDER] Connection closed: {e.code} {e.reason}")
                self._set_health(False)
                # DON'T schedule reconnect here - let the task end cleanly first
                # The reconnect_task will be scheduled separately
                if self._running and not self._reconnect_task:
                    self._reconnect_task = asyncio.create_task(self._do_reconnect())
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Critical error (e.g., duplicate recv call) - break to avoid spam
                logger.error(f"[WS-ORDER] Receive error: {e}")
                self._set_health(False)
                # DON'T schedule reconnect here - let the task end cleanly first
                if self._running and not self._reconnect_task:
                    self._reconnect_task = asyncio.create_task(self._do_reconnect())
                break
                
    async def _handle_message(self, raw_message: str):
        """Process incoming WebSocket message"""
        try:
            message = json.loads(raw_message)
            
            # Handle server PING - respond with PONG (per Lighter Python SDK)
            if message.get('type') in ('ping', 'PING'):
                pong_msg = json.dumps({"type": "pong"})
                if self._ws:
                    await self._ws.send(pong_msg)
                return
                
            # Handle PONG response (ignore)
            if message.get('type') in ('PONG', 'pong'):
                return
            
            # Handle "connected" message (ignore)
            if message.get('type') == 'connected':
                logger.debug("[WS-ORDER] Received connected acknowledgment")
                return
                
            # Handle error response
            if 'error' in message:
                error_msg = message.get('error')
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get('message', str(error_msg))
                    
                req_id = message.get('id')
                if req_id and req_id in self._pending_requests:
                    pending = self._pending_requests.pop(req_id)
                    if pending.timeout_task:
                        pending.timeout_task.cancel()
                    if not pending.future.done():
                        pending.future.set_exception(Exception(error_msg))
                    return
                    
                # No matching request, log error
                logger.error(f"[WS-ORDER] Server error: {error_msg}")
                return
                
            # Handle successful response
            req_id = message.get('id')
            if req_id and req_id in self._pending_requests:
                pending = self._pending_requests.pop(req_id)
                if pending.timeout_task:
                    pending.timeout_task.cancel()
                if not pending.future.done():
                    pending.future.set_result(message)
                return
                
            # Handle transaction result without ID (match oldest pending)
            if 'hash' in message and self._pending_requests:
                oldest_id = next(iter(self._pending_requests))
                pending = self._pending_requests.pop(oldest_id)
                if pending.timeout_task:
                    pending.timeout_task.cancel()
                if not pending.future.done():
                    pending.future.set_result(message)
                return
                
            # Unhandled message
            logger.debug(f"[WS-ORDER] Unhandled message: {message}")
            
        except json.JSONDecodeError as e:
            logger.warning(f"[WS-ORDER] Invalid JSON: {e}")
            
    async def _heartbeat_loop(self):
        """
        Handle server pings - Lighter servers send 'ping' messages that we respond to with 'pong'.
        We also send occasional pongs proactively to keep the connection alive.
        
        Per official Lighter Python SDK (ws_client.py):
        - Server sends {"type": "ping"}
        - Client responds with {"type": "pong"}
        """
        while self._running and self._ws:
            try:
                # Wait longer between proactive keepalives (server will ping us)
                await asyncio.sleep(self.config.heartbeat_interval)
                if self._ws and self._is_connected:
                    # Send a pong as keepalive (server accepts this)
                    pong_msg = json.dumps({"type": "pong"})
                    await self._ws.send(pong_msg)
                    logger.debug("[WS-ORDER] Sent keepalive pong")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[WS-ORDER] Heartbeat error: {e}")
    
    async def _do_reconnect(self):
        """
        Reconnect with proper task cleanup to prevent duplicate recv() calls.
        This is called as a separate task from _receive_loop.
        """
        try:
            # First, cleanup old tasks completely (this waits for them to finish)
            await self._cleanup_tasks()
            
            # Close old WebSocket if still open
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
            
            # Now do the reconnect with backoff
            await self._schedule_reconnect()
        finally:
            self._reconnect_task = None
                
    async def _schedule_reconnect(self):
        """Schedule reconnection with backoff"""
        if not self._running:
            return
            
        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("[WS-ORDER] Max reconnect attempts reached")
            return
            
        self._reconnect_attempts += 1
        delay = min(self.config.reconnect_interval * (2 ** (self._reconnect_attempts - 1)), 60)
        logger.info(f"[WS-ORDER] Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})")
        
        await asyncio.sleep(delay)
        
        if self._running:
            await self.connect()
            
    def _generate_request_id(self) -> str:
        """Generate unique request ID"""
        self._message_id += 1
        return f"tx_{int(time.time() * 1000)}_{self._message_id}"
        
    async def _request_timeout(self, req_id: str):
        """Handle request timeout"""
        await asyncio.sleep(self.config.request_timeout)
        if req_id in self._pending_requests:
            pending = self._pending_requests.pop(req_id)
            if not pending.future.done():
                pending.future.set_exception(asyncio.TimeoutError(f"Request timeout: {req_id}"))
                
    async def send_transaction(
        self,
        tx_type: int,
        tx_info: str  # JSON string from WASM signer
    ) -> WsTransaction:
        """
        Send a single transaction via WebSocket.
        
        Args:
            tx_type: Transaction type (14=CREATE_ORDER, 15=CANCEL_ORDER, etc.)
            tx_info: Signed transaction info as JSON string
            
        Returns:
            WsTransaction with result
            
        Raises:
            Exception on failure
        """
        if not self._is_connected or not self._ws:
            raise Exception("WebSocket not connected")
            
        req_id = self._generate_request_id()
        
        # Parse tx_info to object (server expects object, not string)
        try:
            tx_info_obj = json.loads(tx_info) if isinstance(tx_info, str) else tx_info
        except json.JSONDecodeError:
            tx_info_obj = tx_info
            
        # Build message per Lighter WS API format
        message = {
            "type": "jsonapi/sendtx",
            "data": {
                "id": req_id,
                "tx_type": tx_type,
                "tx_info": tx_info_obj
            }
        }
        
        # Create pending request
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        timeout_task = asyncio.create_task(self._request_timeout(req_id))
        
        self._pending_requests[req_id] = PendingRequest(
            future=future,
            timestamp=time.time(),
            timeout_task=timeout_task
        )
        
        try:
            # Send request
            start_time = time.time()
            await self._ws.send(json.dumps(message))
            
            # Wait for response
            result = await future
            latency_ms = (time.time() - start_time) * 1000
            
            logger.debug(f"[WS-ORDER] TX sent in {latency_ms:.1f}ms: {result.get('hash', 'N/A')}")
            
            return WsTransaction.from_dict(result)
            
        except asyncio.TimeoutError:
            raise Exception(f"Request timeout after {self.config.request_timeout}s")
        except Exception as e:
            raise Exception(f"Failed to send transaction: {e}")
            
    async def send_batch_transactions(
        self,
        tx_types: List[int],
        tx_infos: List[str]
    ) -> List[WsTransaction]:
        """
        Send batch transactions via WebSocket.
        
        Args:
            tx_types: List of transaction types
            tx_infos: List of signed transaction info JSON strings
            
        Returns:
            List of WsTransaction results
        """
        if not self._is_connected or not self._ws:
            raise Exception("WebSocket not connected")
            
        if len(tx_types) != len(tx_infos):
            raise ValueError("tx_types and tx_infos must have same length")
            
        if len(tx_types) > 50:
            raise ValueError("Batch size cannot exceed 50 transactions")

        # Parse tx_infos to objects
        tx_info_objs = []
        for ti in tx_infos:
            try:
                tx_info_objs.append(json.loads(ti) if isinstance(ti, str) else ti)
            except json.JSONDecodeError:
                tx_info_objs.append(ti)

        async def _send_batch(message: dict, req_id: str, fmt: str) -> List[WsTransaction]:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            timeout_task = asyncio.create_task(self._request_timeout(req_id))
            self._pending_requests[req_id] = PendingRequest(
                future=future,
                timestamp=time.time(),
                timeout_task=timeout_task,
            )
            start_time = time.time()
            try:
                await self._ws.send(json.dumps(message))
                result = await future
                latency_ms = (time.time() - start_time) * 1000
                logger.debug(f"[WS-ORDER] Batch of {len(tx_types)} sent in {latency_ms:.1f}ms (format={fmt})")
                if isinstance(result, list):
                    return [WsTransaction.from_dict(r) for r in result]
                return [WsTransaction.from_dict(result)]
            except Exception:
                pending = self._pending_requests.pop(req_id, None)
                if pending and pending.timeout_task:
                    pending.timeout_task.cancel()
                raise

        last_error: Optional[Exception] = None
        for fmt in ("array", "string"):
            req_id = self._generate_request_id()
            message = {
                "type": "jsonapi/sendtxbatch",
                "data": {
                    "id": req_id,
                    "tx_types": tx_types if fmt == "array" else json.dumps(tx_types),
                    "tx_infos": tx_info_objs if fmt == "array" else json.dumps(tx_info_objs),
                },
            }
            try:
                return await _send_batch(message, req_id, fmt)
            except asyncio.TimeoutError as e:
                last_error = e
            except Exception as e:
                last_error = e

        if isinstance(last_error, asyncio.TimeoutError):
            raise Exception(f"Batch request timeout after {self.config.request_timeout}s")
        raise Exception(f"Failed to send batch: {last_error}")
            
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics"""
        return {
            "is_connected": self._is_connected,
            "pending_requests": len(self._pending_requests),
            "reconnect_attempts": self._reconnect_attempts,
            "url": self.config.url
        }


# Convenience function for creating client
def create_ws_order_client(url: Optional[str] = None) -> WebSocketOrderClient:
    """Create WebSocket order client with optional custom URL"""
    config = WsOrderConfig()
    if url:
        config.url = url
    return WebSocketOrderClient(config)
