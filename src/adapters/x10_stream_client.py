# src/adapters/x10_stream_client.py
"""
X10 Stream Client for real-time WebSocket updates.

Implements the Extended-TS-SDK PerpetualStreamClient functionality in Python.
Provides real-time updates for:
- Account updates (positions, orders, balance)
- Orderbooks
- Funding rates
- Public trades
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional, Callable, Any, List, Set
from dataclasses import dataclass
from enum import Enum
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

logger = logging.getLogger(__name__)


class StreamType(Enum):
    """Stream subscription types"""
    ORDERBOOKS = "orderbooks"
    PUBLIC_TRADES = "publicTrades"
    FUNDING = "funding"
    ACCOUNT = "account"
    CANDLES = "candles"


@dataclass
class StreamConfig:
    """Configuration for a stream connection"""
    stream_url: str
    api_key: Optional[str] = None
    market_name: Optional[str] = None
    depth: Optional[int] = None  # For orderbooks
    reconnect_delay_initial: float = 2.0
    reconnect_delay_max: float = 120.0
    ping_interval: float = 20.0
    ping_timeout: float = 10.0


class X10StreamConnection:
    """
    Single WebSocket stream connection for X10.
    
    Handles connection, reconnection, and message parsing.
    """
    
    def __init__(
        self,
        stream_type: StreamType,
        config: StreamConfig,
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
        self._metrics = {
            'messages_received': 0,
            'reconnect_count': 0,
            'last_error': None
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
            'is_connected': self.is_connected
        }
    
    def _build_url(self) -> str:
        """Build WebSocket URL based on stream type"""
        base_url = self.config.stream_url
        
        if self.stream_type == StreamType.ORDERBOOKS:
            if self.config.market_name:
                path = f"/orderbooks/{self.config.market_name}"
            else:
                path = "/orderbooks"
            if self.config.depth:
                return f"{base_url}{path}?depth={self.config.depth}"
            return f"{base_url}{path}"
        
        elif self.stream_type == StreamType.PUBLIC_TRADES:
            if self.config.market_name:
                return f"{base_url}/publicTrades/{self.config.market_name}"
            return f"{base_url}/publicTrades"
        
        elif self.stream_type == StreamType.FUNDING:
            if self.config.market_name:
                return f"{base_url}/funding/{self.config.market_name}"
            return f"{base_url}/funding"
        
        elif self.stream_type == StreamType.ACCOUNT:
            return f"{base_url}/account"
        
        elif self.stream_type == StreamType.CANDLES:
            # Requires market_name, candle_type, interval
            raise NotImplementedError("Candles stream requires additional parameters")
        
        raise ValueError(f"Unknown stream type: {self.stream_type}")
    
    def _build_headers(self) -> Dict[str, str]:
        """Build WebSocket headers"""
        headers = {
            'User-Agent': 'X10-Funding-Bot/1.0'
        }
        
        if self.config.api_key:
            headers['X-API-Key'] = self.config.api_key
        
        return headers
    
    async def connect(self) -> None:
        """Connect to WebSocket stream"""
        url = self._build_url()
        headers = self._build_headers()
        
        logger.info(f"🔌 [X10 Stream] Connecting to {self.stream_type.value}: {url}")
        
        try:
            self._ws = await websockets.connect(
                url,
                extra_headers=headers,
                ping_interval=self.config.ping_interval,
                ping_timeout=self.config.ping_timeout
            )
            
            self._last_message_time = time.time()
            self._reconnect_delay = self.config.reconnect_delay_initial
            self._reconnect_attempts = 0
            
            logger.info(f"✅ [X10 Stream] Connected to {self.stream_type.value}")
            
            if self.on_connect:
                try:
                    await self.on_connect()
                except Exception as e:
                    logger.warning(f"[X10 Stream] on_connect callback error: {e}")
                    
        except Exception as e:
            logger.error(f"❌ [X10 Stream] Connection failed for {self.stream_type.value}: {e}")
            self._metrics['last_error'] = str(e)
            raise
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket"""
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
            
            if self.on_disconnect:
                try:
                    await self.on_disconnect()
                except Exception as e:
                    logger.warning(f"[X10 Stream] on_disconnect callback error: {e}")
    
    async def _receive_loop(self) -> None:
        """Main message receiving loop"""
        while self._running:
            try:
                if not self._ws or self._ws.closed:
                    break
                
                message = await self._ws.recv()
                self._last_message_time = time.time()
                self._metrics['messages_received'] += 1
                
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"[X10 Stream] Invalid JSON from {self.stream_type.value}: {e}")
                except Exception as e:
                    logger.error(f"[X10 Stream] Error handling message from {self.stream_type.value}: {e}")
                    
            except ConnectionClosed:
                logger.warning(f"⚠️ [X10 Stream] Connection closed for {self.stream_type.value}")
                break
            except Exception as e:
                logger.error(f"❌ [X10 Stream] Receive error for {self.stream_type.value}: {e}")
                self._metrics['last_error'] = str(e)
                break
    
    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message"""
        try:
            # Call message handler
            if self.message_handler:
                await self.message_handler(data)
        except Exception as e:
            logger.error(f"[X10 Stream] Message handler error for {self.stream_type.value}: {e}")
    
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
                logger.error(f"[X10 Stream] Connection error for {self.stream_type.value}: {e}")
                self._metrics['last_error'] = str(e)
            
            if not self._running:
                break
            
            # Reconnect logic
            self._metrics['reconnect_count'] += 1
            delay = min(self._reconnect_delay, self.config.reconnect_delay_max)
            
            logger.info(f"🔄 [X10 Stream] Reconnecting {self.stream_type.value} in {delay:.1f}s (attempt {self._metrics['reconnect_count']})")
            await asyncio.sleep(delay)
            
            self._reconnect_delay *= 1.5  # Exponential backoff
    
    async def stop(self) -> None:
        """Stop the connection"""
        self._running = False
        await self.disconnect()


class X10StreamClient:
    """
    Main X10 Stream Client.
    
    Manages multiple stream connections for different data types.
    """
    
    def __init__(self, stream_url: str, api_key: Optional[str] = None):
        self.stream_url = stream_url
        self.api_key = api_key
        self._connections: Dict[str, X10StreamConnection] = {}  # Use composite keys for multiple streams
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False
    
    async def subscribe_to_account_updates(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to account updates (positions, orders, balance)"""
        if not self.api_key:
            raise ValueError("API key required for account updates")
        
        config = StreamConfig(
            stream_url=self.stream_url,
            api_key=self.api_key
        )
        
        connection = X10StreamConnection(
            StreamType.ACCOUNT,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        key = f"{StreamType.ACCOUNT.value}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def subscribe_to_orderbooks(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_name: Optional[str] = None,
        depth: Optional[int] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to orderbook updates"""
        config = StreamConfig(
            stream_url=self.stream_url,
            market_name=market_name,
            depth=depth
        )
        
        connection = X10StreamConnection(
            StreamType.ORDERBOOKS,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key to support multiple orderbook streams
        key = f"{StreamType.ORDERBOOKS.value}:{market_name or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def subscribe_to_funding_rates(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_name: Optional[str] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to funding rate updates"""
        config = StreamConfig(
            stream_url=self.stream_url,
            market_name=market_name
        )
        
        connection = X10StreamConnection(
            StreamType.FUNDING,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key to support multiple funding streams
        key = f"{StreamType.FUNDING.value}:{market_name or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def subscribe_to_public_trades(
        self,
        message_handler: Callable[[Dict[str, Any]], None],
        market_name: Optional[str] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None
    ) -> None:
        """Subscribe to public trade updates"""
        config = StreamConfig(
            stream_url=self.stream_url,
            market_name=market_name
        )
        
        connection = X10StreamConnection(
            StreamType.PUBLIC_TRADES,
            config,
            message_handler,
            on_connect,
            on_disconnect
        )
        
        # Use composite key to support multiple trade streams
        key = f"{StreamType.PUBLIC_TRADES.value}:{market_name or 'all'}"
        self._connections[key] = connection
        
        if self._running:
            task = asyncio.create_task(connection.run())
            self._tasks[key] = task
    
    async def start(self) -> None:
        """Start all stream connections"""
        self._running = True
        
        for stream_type, connection in self._connections.items():
            task = asyncio.create_task(connection.run())
            self._tasks[stream_type] = task
        
        logger.info(f"🚀 [X10 Stream Client] Started {len(self._connections)} stream(s)")
    
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
        
        logger.info("🛑 [X10 Stream Client] Stopped all streams")
    
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

