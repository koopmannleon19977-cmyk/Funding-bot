# src/adapters/lighter_stream_client.py
"""
Lighter Stream Client for real-time WebSocket updates.

Implements structured stream management for Lighter Protocol WebSocket API.
Provides real-time updates for:
- Orderbooks
- Public trades
- Funding rates (if available)
- Account updates (if available)
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional, Callable, Any, List
from dataclasses import dataclass
from enum import Enum
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

logger = logging.getLogger(__name__)


class LighterStreamType(Enum):
    """Lighter stream subscription types"""
    ORDERBOOK = "order_book"
    TRADES = "trade"
    FUNDING = "funding"
    ACCOUNT = "account"


@dataclass
class LighterStreamConfig:
    """Configuration for a Lighter stream connection"""
    stream_url: str
    market_id: Optional[int] = None  # Market ID for symbol-specific streams
    reconnect_delay_initial: float = 2.0
    reconnect_delay_max: float = 120.0
    ping_interval: float = 20.0
    ping_timeout: float = 10.0
    json_ping_interval: Optional[float] = 10.0  # Lighter uses JSON ping/pong


class LighterStreamConnection:
    """
    Single WebSocket stream connection for Lighter.
    
    Handles connection, reconnection, and message parsing.
    Uses Lighter's channel-based subscription model.
    """
    
    def __init__(
        self,
        stream_type: LighterStreamType,
        config: LighterStreamConfig,
        message_handler: Callable[[Dict[str, Any]], None],
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ):
        self.stream_type = stream_type
        self.config = config
        self.message_handler = message_handler
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = config.reconnect_delay_initial
        self._reconnect_attempts = 0
        self._last_message_time = 0.0
        self._last_pong_time = 0.0
        self._last_ping_time = 0.0
        self._metrics = {
            'messages_received': 0,
            'reconnect_count': 0,
            'last_error': None,
            'pings_sent': 0,
            'pongs_received': 0
        }
        
    @property
    def is_connected(self) -> bool:
        """Check if connection is active"""
        return self._ws is not None and not self._ws.closed
    
    @property
    def metrics(self) -> Dict[str, Any]:
        """Get connection metrics"""
        return {
            **self._metrics,
            'uptime_seconds': time.time() - self._last_message_time if self._last_message_time > 0 else 0.0,
            'is_connected': self.is_connected,
            'time_since_last_pong': time.time() - self._last_pong_time if self._last_pong_time > 0 else 0.0
        }
    
    def _get_channel_name(self) -> str:
        """Get channel name for subscription"""
        if self.stream_type == LighterStreamType.ORDERBOOK:
            if self.config.market_id is None:
                # Lighter WS requires a market_id for order_book subscriptions.
                raise ValueError("Lighter order_book stream requires market_id")
            return f"order_book/{self.config.market_id}"
        
        elif self.stream_type == LighterStreamType.TRADES:
            if self.config.market_id is None:
                # Lighter WS requires a market_id for trade subscriptions.
                raise ValueError("Lighter trade stream requires market_id")
            return f"trade/{self.config.market_id}"
        
        elif self.stream_type == LighterStreamType.FUNDING:
            # Lighter WS does not expose a dedicated "funding/*" channel (Invalid Channel).
            # Funding is delivered via market_stats.
            if self.config.market_id is not None:
                return f"market_stats/{self.config.market_id}"
            return "market_stats/all"
        
        elif self.stream_type == LighterStreamType.ACCOUNT:
            return "account"  # Account updates don't need market_id
        
        raise ValueError(f"Unknown stream type: {self.stream_type}")
    
    async def connect(self) -> None:
        """Connect to WebSocket stream"""
        url = self.config.stream_url
        
        logger.info(f"🔌 [Lighter Stream] Connecting to {self.stream_type.value}: {url}")
        
        try:
            self._ws = await websockets.connect(
                url,
                ping_interval=self.config.ping_interval,
                ping_timeout=self.config.ping_timeout
            )
            
            self._last_message_time = time.time()
            self._last_pong_time = time.time()
            self._reconnect_delay = self.config.reconnect_delay_initial
            self._reconnect_attempts = 0
            
            # Subscribe to channel
            channel = self._get_channel_name()
            subscribe_msg = {
                "type": "subscribe",
                "channel": channel
            }
            await self._ws.send(json.dumps(subscribe_msg))
            
            logger.info(f"✅ [Lighter Stream] Connected and subscribed to {channel}")
            
            if self.on_connect:
                try:
                    await self.on_connect()
                except Exception as e:
                    logger.warning(f"[Lighter Stream] on_connect callback error: {e}")
                    
        except Exception as e:
            logger.error(f"❌ [Lighter Stream] Connection failed for {self.stream_type.value}: {e}")
            self._metrics['last_error'] = str(e)
            raise
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket"""
        if self._ws and not self._ws.closed:
            # Unsubscribe before closing
            try:
                channel = self._get_channel_name()
                unsubscribe_msg = {
                    "type": "unsubscribe",
                    "channel": channel
                }
                await self._ws.send(json.dumps(unsubscribe_msg))
            except Exception:
                pass
            
            await self._ws.close()
            self._ws = None
            
            if self.on_disconnect:
                try:
                    await self.on_disconnect()
                except Exception as e:
                    logger.warning(f"[Lighter Stream] on_disconnect callback error: {e}")
    
    async def _send_json_ping(self) -> None:
        """Send JSON ping message (Lighter-specific)"""
        if not self._ws or self._ws.closed:
            return
        
        try:
            ping_msg = {"type": "ping"}
            await self._ws.send(json.dumps(ping_msg))
            self._last_ping_time = time.time()
            self._metrics['pings_sent'] += 1
        except Exception as e:
            logger.debug(f"[Lighter Stream] JSON ping error: {e}")
    
    async def _receive_loop(self) -> None:
        """Main message receiving loop"""
        json_ping_task = None
        
        # Start JSON ping task if configured
        if self.config.json_ping_interval:
            async def json_ping_loop():
                while self._running and self.is_connected:
                    await asyncio.sleep(self.config.json_ping_interval)
                    if self._running and self.is_connected:
                        await self._send_json_ping()
            
            json_ping_task = asyncio.create_task(json_ping_loop())
        
        try:
            while self._running:
                try:
                    if not self._ws or self._ws.closed:
                        break
                    
                    message = await self._ws.recv()
                    self._last_message_time = time.time()
                    self._metrics['messages_received'] += 1
                    
                    try:
                        data = json.loads(message)
                        
                        # Handle pong messages
                        if data.get("type") == "pong":
                            self._last_pong_time = time.time()
                            self._metrics['pongs_received'] += 1
                            continue
                        
                        # Handle other messages
                        await self._handle_message(data)
                    except json.JSONDecodeError as e:
                        logger.warning(f"[Lighter Stream] Invalid JSON from {self.stream_type.value}: {e}")
                    except Exception as e:
                        logger.error(f"[Lighter Stream] Error handling message from {self.stream_type.value}: {e}")
                        
                except ConnectionClosed:
                    logger.warning(f"⚠️ [Lighter Stream] Connection closed for {self.stream_type.value}")
                    break
                except Exception as e:
                    logger.error(f"❌ [Lighter Stream] Receive error for {self.stream_type.value}: {e}")
                    self._metrics['last_error'] = str(e)
                    break
        finally:
            if json_ping_task:
                json_ping_task.cancel()
                try:
                    await json_ping_task
                except asyncio.CancelledError:
                    pass
    
    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message"""
        try:
            # Call message handler
            if self.message_handler:
                await self.message_handler(data)
        except Exception as e:
            logger.error(f"[Lighter Stream] Message handler error for {self.stream_type.value}: {e}")
    
    async def run(self) -> None:
        """Run connection with auto-reconnect"""
        self._running = True
        
        while self._running:
            try:
                await self.connect()
                await self._receive_loop()
                
            except ConnectionClosed:
                # Normal closure, try to reconnect
                pass
            except Exception as e:
                logger.error(f"[Lighter Stream] Connection error for {self.stream_type.value}: {e}")
                self._metrics['last_error'] = str(e)
            
            if not self._running:
                break
            
            # Reconnect logic
            self._metrics['reconnect_count'] += 1
            delay = min(self._reconnect_delay, self.config.reconnect_delay_max)
            
            logger.info(f"🔄 [Lighter Stream] Reconnecting {self.stream_type.value} in {delay:.1f}s (attempt {self._metrics['reconnect_count']})")
            await asyncio.sleep(delay)
            
            self._reconnect_delay *= 1.5  # Exponential backoff
    
    async def stop(self) -> None:
        """Stop the connection"""
        self._running = False
        await self.disconnect()


class LighterStreamClient:
    """
    Main Lighter Stream Client.
    
    Manages multiple stream connections for different data types.
    """
    
    def __init__(self, stream_url: str):
        self.stream_url = stream_url
        self._connections: Dict[str, LighterStreamConnection] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False
    
    async def subscribe_to_orderbooks(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_id: Optional[int] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to orderbook updates"""
        config = LighterStreamConfig(
            stream_url=self.stream_url,
            market_id=market_id
        )
        
        connection = LighterStreamConnection(
            LighterStreamType.ORDERBOOK,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key to support multiple orderbook streams
        key = f"{LighterStreamType.ORDERBOOK.value}:{market_id or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def subscribe_to_trades(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_id: Optional[int] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to public trade updates"""
        config = LighterStreamConfig(
            stream_url=self.stream_url,
            market_id=market_id
        )
        
        connection = LighterStreamConnection(
            LighterStreamType.TRADES,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key to support multiple trade streams
        key = f"{LighterStreamType.TRADES.value}:{market_id or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def subscribe_to_funding_rates(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_id: Optional[int] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to funding rate updates (if available)"""
        config = LighterStreamConfig(
            stream_url=self.stream_url,
            market_id=market_id
        )
        
        connection = LighterStreamConnection(
            LighterStreamType.FUNDING,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key
        key = f"{LighterStreamType.FUNDING.value}:{market_id or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def start(self) -> None:
        """Start all stream connections"""
        self._running = True
        
        for stream_key, connection in self._connections.items():
            task = asyncio.create_task(connection.run())
            self._tasks[stream_key] = task
        
        logger.info(f"🚀 [Lighter Stream Client] Started {len(self._connections)} stream(s)")
    
    async def stop(self) -> None:
        """Stop all stream connections"""
        self._running = False
        
        # Stop all connections
        for connection in self._connections.values():
            await connection.stop()
        
        # Cancel all tasks
        for task in self._tasks.values():
            task.cancel()
        
        # Wait for tasks to complete
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        
        self._connections.clear()
        self._tasks.clear()
        
        logger.info("🛑 [Lighter Stream Client] Stopped all streams")
    
    def get_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get metrics for all connections"""
        return {
            key: connection.metrics
            for key, connection in self._connections.items()
        }
    
    def is_connected(self, stream_key: Optional[str] = None) -> bool:
        """Check if connection(s) are active"""
        if stream_key:
            if stream_key in self._connections:
                return self._connections[stream_key].is_connected
            return False
        
        return any(conn.is_connected for conn in self._connections.values())

