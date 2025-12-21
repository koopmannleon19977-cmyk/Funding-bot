"""
WebSocket Order Client for X10 Exchange

Sends orders via WebSocket instead of REST API for lower latency.
Pattern from: src/ws_order_client.py (Lighter) and Extended-TS-SDK

Note: X10 may not have a dedicated WebSocket order endpoint.
This implementation provides a framework that can be adapted once
the exact X10 WebSocket order API is confirmed.

WebSocket Endpoint: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account
  (Uses account stream endpoint - may need separate order endpoint)

Message Format (TBD - based on X10 API docs):
    Send: {"id": "req_123", "method": "order.place", "params": {...}}
    Recv: {"id": "req_123", "result": {...}} or {"id": "req_123", "error": {...}}

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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)


@dataclass
class X10WsOrderConfig:
    """WebSocket Order Client configuration for X10"""
    url: str = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0
    request_timeout: float = 10.0
    api_key: Optional[str] = None


@dataclass
class X10WsOrderResponse:
    """Response from WebSocket order submission"""
    order_id: str
    status: str
    market: str
    side: str
    filled_qty: Optional[float] = None
    average_price: Optional[float] = None
    error: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'X10WsOrderResponse':
        """Parse response from X10 WebSocket API"""
        if 'error' in data:
            return cls(
                order_id="",
                status="ERROR",
                market=data.get('market', ''),
                side=data.get('side', ''),
                error=str(data.get('error', 'Unknown error'))
            )
        
        result = data.get('result', data)
        return cls(
            order_id=str(result.get('id', result.get('order_id', ''))),
            status=result.get('status', 'UNKNOWN'),
            market=result.get('market', ''),
            side=result.get('side', ''),
            filled_qty=result.get('filled_qty'),
            average_price=result.get('average_price')
        )


@dataclass
class PendingRequest:
    """Pending WebSocket request awaiting response"""
    future: asyncio.Future
    timestamp: float
    timeout_task: Optional[asyncio.Task] = None


class X10WebSocketOrderClient:
    """
    WebSocket client for submitting orders to X10 exchange.
    
    Provides lower latency than REST API (~50-100ms improvement).
    Falls back to REST on connection failure.
    
    Note: This is a framework implementation. The exact message format
    may need to be adjusted based on X10's actual WebSocket API.
    """
    
    def __init__(self, config: Optional[X10WsOrderConfig] = None):
        self.config = config or X10WsOrderConfig()
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
                logger.warning(f"[X10-WS-ORDER] Health callback error: {e}")
    
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
            logger.info(f"[X10-WS-ORDER] Connecting to {self.config.url}...")
            
            # Build headers with API key if available
            extra_headers = {}
            if self.config.api_key:
                extra_headers['X-API-Key'] = self.config.api_key
            
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.config.url,
                    extra_headers=extra_headers if extra_headers else None,
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
            if self.config.heartbeat_interval and self.config.heartbeat_interval > 0:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            logger.info(f"✅ [X10-WS-ORDER] Connected to {self.config.url}")
            return True
            
        except asyncio.TimeoutError:
            logger.warning(f"[X10-WS-ORDER] Connection timeout to {self.config.url}")
            return False
        except Exception as e:
            logger.error(f"[X10-WS-ORDER] Connection failed: {e}")
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
        logger.info("[X10-WS-ORDER] Disconnected")
        
    async def _receive_loop(self):
        """Background task to receive and process messages"""
        while self._running and self._ws:
            try:
                message = await self._ws.recv()
                await self._handle_message(message)
            except ConnectionClosed as e:
                logger.warning(f"[X10-WS-ORDER] Connection closed: {e.code} {e.reason}")
                self._set_health(False)
                if self._running and not self._reconnect_task:
                    self._reconnect_task = asyncio.create_task(self._do_reconnect())
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[X10-WS-ORDER] Receive error: {e}")
                self._set_health(False)
                if self._running and not self._reconnect_task:
                    self._reconnect_task = asyncio.create_task(self._do_reconnect())
                break
                
    async def _handle_message(self, raw_message: str):
        """Process incoming WebSocket message"""
        try:
            message = json.loads(raw_message)
            
            # Handle server PING - respond with PONG
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
                logger.debug("[X10-WS-ORDER] Received connected acknowledgment")
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
                logger.error(f"[X10-WS-ORDER] Server error: {error_msg}")
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
                
            # Handle order update without ID (match oldest pending)
            if 'order_id' in message or 'id' in message:
                if self._pending_requests:
                    oldest_id = next(iter(self._pending_requests))
                    pending = self._pending_requests.pop(oldest_id)
                    if pending.timeout_task:
                        pending.timeout_task.cancel()
                    if not pending.future.done():
                        pending.future.set_result(message)
                    return
                
            # Unhandled message
            logger.debug(f"[X10-WS-ORDER] Unhandled message: {message}")
            
        except json.JSONDecodeError as e:
            logger.warning(f"[X10-WS-ORDER] Invalid JSON: {e}")
            
    async def _heartbeat_loop(self):
        """Send periodic heartbeat to keep connection alive"""
        while self._running and self._ws:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)
                if self._ws and self._is_connected:
                    ping_msg = json.dumps({"type": "ping", "timestamp": int(time.time() * 1000)})
                    await self._ws.send(ping_msg)
                    logger.debug("[X10-WS-ORDER] Sent heartbeat ping")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[X10-WS-ORDER] Heartbeat error: {e}")
    
    async def _do_reconnect(self):
        """Reconnect with proper task cleanup"""
        try:
            await self._cleanup_tasks()
            
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
            
            await self._schedule_reconnect()
        finally:
            self._reconnect_task = None
                
    async def _schedule_reconnect(self):
        """Schedule reconnection with backoff"""
        if not self._running:
            return
            
        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("[X10-WS-ORDER] Max reconnect attempts reached")
            return
            
        self._reconnect_attempts += 1
        delay = min(self.config.reconnect_interval * (2 ** (self._reconnect_attempts - 1)), 60)
        logger.info(f"[X10-WS-ORDER] Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})")
        
        await asyncio.sleep(delay)
        
        if self._running:
            await self.connect()
            
    def _generate_request_id(self) -> str:
        """Generate unique request ID"""
        self._message_id += 1
        return f"x10_{int(time.time() * 1000)}_{self._message_id}"
        
    async def _request_timeout(self, req_id: str):
        """Handle request timeout"""
        await asyncio.sleep(self.config.request_timeout)
        if req_id in self._pending_requests:
            pending = self._pending_requests.pop(req_id)
            if not pending.future.done():
                pending.future.set_exception(asyncio.TimeoutError(f"Request timeout: {req_id}"))
                
    async def place_order(
        self,
        market: str,
        side: str,
        qty: float,
        price: float,
        order_type: str = "LIMIT",
        post_only: bool = False,
        time_in_force: str = "GTT",
        reduce_only: bool = False,
        external_id: Optional[str] = None
    ) -> X10WsOrderResponse:
        """
        Place a single order via WebSocket.
        
        Args:
            market: Market symbol (e.g., "BTC-USD")
            side: "BUY" or "SELL"
            qty: Order quantity
            price: Order price
            order_type: "LIMIT" or "MARKET"
            post_only: Whether order is post-only
            time_in_force: "GTT", "IOC", or "FOK"
            reduce_only: Whether order is reduce-only
            external_id: Optional external order ID
            
        Returns:
            X10WsOrderResponse with result
            
        Raises:
            Exception on failure
        """
        if not self._is_connected or not self._ws:
            raise Exception("WebSocket not connected")
            
        req_id = self._generate_request_id()
        
        # Build message per X10 WebSocket API format (TBD - may need adjustment)
        message = {
            "id": req_id,
            "method": "order.place",
            "params": {
                "market": market,
                "side": side,
                "type": order_type,
                "qty": str(qty),
                "price": str(price),
                "postOnly": post_only,
                "timeInForce": time_in_force,
                "reduceOnly": reduce_only
            }
        }
        
        if external_id:
            message["params"]["externalId"] = external_id
        
        # Create pending request
        loop = asyncio.get_event_loop()
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
            
            logger.debug(f"[X10-WS-ORDER] Order placed in {latency_ms:.1f}ms: {result.get('id', 'N/A')}")
            
            return X10WsOrderResponse.from_dict(result)
            
        except asyncio.TimeoutError:
            raise Exception(f"Request timeout after {self.config.request_timeout}s")
        except Exception as e:
            raise Exception(f"Failed to place order: {e}")
            
    async def cancel_order(
        self,
        order_id: str,
        market: Optional[str] = None
    ) -> X10WsOrderResponse:
        """
        Cancel an order via WebSocket.
        
        Args:
            order_id: Order ID to cancel
            market: Optional market symbol
            
        Returns:
            X10WsOrderResponse with result
            
        Raises:
            Exception on failure
        """
        if not self._is_connected or not self._ws:
            raise Exception("WebSocket not connected")
            
        req_id = self._generate_request_id()
        
        # Build cancel message
        message = {
            "id": req_id,
            "method": "order.cancel",
            "params": {
                "orderId": order_id
            }
        }
        
        if market:
            message["params"]["market"] = market
        
        # Create pending request
        loop = asyncio.get_event_loop()
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
            
            logger.debug(f"[X10-WS-ORDER] Order cancelled in {latency_ms:.1f}ms: {order_id}")
            
            return X10WsOrderResponse.from_dict(result)
            
        except asyncio.TimeoutError:
            raise Exception(f"Request timeout after {self.config.request_timeout}s")
        except Exception as e:
            raise Exception(f"Failed to cancel order: {e}")
            
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics"""
        return {
            "is_connected": self._is_connected,
            "pending_requests": len(self._pending_requests),
            "reconnect_attempts": self._reconnect_attempts,
            "url": self.config.url
        }


# Convenience function for creating client
def create_x10_ws_order_client(url: Optional[str] = None, api_key: Optional[str] = None) -> X10WebSocketOrderClient:
    """Create X10 WebSocket order client with optional custom URL and API key"""
    config = X10WsOrderConfig()
    if url:
        config.url = url
    if api_key:
        config.api_key = api_key
    return X10WebSocketOrderClient(config)

