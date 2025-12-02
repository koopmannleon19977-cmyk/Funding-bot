# src/websocket_manager.py - PUNKT 9: WEBSOCKETS REFACTOR MIT AUTO-RECONNECT

import asyncio
import json
import time
import logging
from typing import Dict, Optional, Callable, Any, Set, List
from dataclasses import dataclass, field
from enum import Enum
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

logger = logging.getLogger(__name__)
import config


def safe_float(val, default=0.0):
    """Safely convert a value to float, returning default on failure."""
    if val is None or val == "" or val == "None":
        return default
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


@dataclass
class WSConfig:
    """WebSocket connection configuration"""
    url: str
    name: str
    ping_interval: Optional[float] = 20.0  # None = no client pings, library auto-responds to server pings
    ping_timeout: Optional[float] = 20.0   # None = no timeout for client pings
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
        self._process_task: Optional[asyncio.Task] = None  # NEW: Message processing task
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
        for task in [self._receive_task, self._heartbeat_task, self._connect_task, self._process_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        # Close connection
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
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
                    
                    # Resubscribe to channels
                    await self._resubscribe_all()
                    
                    # Wait for tasks to complete (connection lost)
                    done, pending = await asyncio.wait(
                        [self._receive_task, self._heartbeat_task, self._process_task],
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
            }
            
            # Ping-Konfiguration:
            # - Wenn ping_interval explizit gesetzt ist (z.B. 20.0): Client sendet Pings
            # - Wenn ping_interval None ist: EXPLIZIT None übergeben um Client-Pings zu DEAKTIVIEREN
            # Ohne explizites None verwendet die Library ihre Defaults (ping_interval=20)!
            if self.config.ping_interval is not None:
                connect_kwargs["ping_interval"] = self.config.ping_interval
                connect_kwargs["ping_timeout"] = self.config.ping_timeout or 20.0
            else:
                # CRITICAL: Explizit None übergeben um Client-Pings zu deaktivieren
                # Die Library antwortet trotzdem automatisch auf Server-Pings!
                connect_kwargs["ping_interval"] = None
                connect_kwargs["ping_timeout"] = None
            
            # Add headers if configured
            if self.config.headers:
                connect_kwargs["additional_headers"] = self.config.headers
            
            logger.info(f"🔌 [{self.config.name}] Connecting with kwargs: {connect_kwargs}")
            
            self._ws = await asyncio.wait_for(
                websockets.connect(self.config.url, **connect_kwargs),
                timeout=30.0
            )
            
            self._state = WSState.CONNECTED
            self._metrics.last_connect_time = time.time()
            
            logger.info(f"✅ [{self.config.name}] Connected to {self.config.url}")
            logger.debug(f"✅ [{self.config.name}] WebSocket library will auto-respond to server pings")
            
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
        
        CRITICAL FIX for X10 "1011 Ping timeout":
        This loop MUST stay free to allow the websockets library to respond to
        server pings with pongs. Message processing is moved to _process_loop().
        """
        try:
            async for message in self._ws:
                self._metrics.messages_received += 1
                self._metrics.last_message_time = time.time()
                
                # Queue message for async processing - DON'T PROCESS HERE!
                # This keeps the receive loop free for ping/pong handling
                try:
                    self._message_queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning(f"[{self.config.name}] Message queue full, dropping message")
                
                # CRITICAL: Yield to event loop after EVERY message
                # This allows the websockets library to process ping frames
                await asyncio.sleep(0)
        
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
                # This helps drain the queue quickly during snapshot bursts
                processed = 1
                while processed < batch_size:
                    try:
                        message = self._message_queue.get_nowait()
                        await self._process_single_message(message)
                        processed += 1
                    except asyncio.QueueEmpty:
                        break
                
                # Log queue depth periodically if it's backing up
                qsize = self._message_queue.qsize()
                if qsize > 1000:
                    logger.warning(f"[{self.config.name}] Message queue depth: {qsize}")
                
                # Yield to event loop after batch
                await asyncio.sleep(0)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.config.name}] Process loop error: {e}")
                await asyncio.sleep(0.1)
    
    async def _process_single_message(self, message: str):
        """Process a single message - extracted for batch processing."""
        try:
            data = json.loads(message)
            await self.message_handler(self.config.name, data)
        except json.JSONDecodeError:
            logger.debug(f"[{self.config.name}] Invalid JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"[{self.config.name}] Handler error: {e}")
    
    async def _heartbeat_loop(self):
        """Monitor connection health"""
        stale_threshold = 180.0  # 3 minutes without messages = stale
        check_interval = 30.0    # Health-check interval when no client pings
        
        # Log connection start time for debugging
        connect_time = time.time()
        logger.debug(f"💓 [{self.config.name}] Heartbeat started (ping_interval={self.config.ping_interval}, ping_timeout={self.config.ping_timeout})")
        
        while self._running and self.is_connected:
            try:
                if self.config.ping_interval is not None:
                    # Mode 1: Client-initiierte Pings (für Lighter etc.)
                    interval = self.config.ping_interval
                    await asyncio.sleep(interval)
                    
                    if self._ws:
                        try:
                            pong_waiter = await self._ws.ping()
                            
                            if self.config.ping_timeout is not None:
                                await asyncio.wait_for(pong_waiter, timeout=self.config.ping_timeout)
                                logger.debug(f"💓 [{self.config.name}] Ping/Pong OK")
                            else:
                                logger.debug(f"💓 [{self.config.name}] Ping sent (no pong expected)")
                        except asyncio.TimeoutError:
                            logger.warning(f"[{self.config.name}] Ping timeout, reconnecting")
                            break
                        except Exception as e:
                            if "1011" in str(e) or "closed" in str(e).lower():
                                logger.debug(f"[{self.config.name}] Ping error (connection issue): {e}")
                            else:
                                logger.debug(f"[{self.config.name}] Ping error: {e}")
                            break
                else:
                    # Mode 2: KEINE Client-Pings (für X10)
                    # Die websockets-Library antwortet AUTOMATISCH auf Server-Pings!
                    # Wir machen nur Health-Monitoring ohne eigene Pings zu senden.
                    await asyncio.sleep(check_interval)
                
                # Connection health check
                uptime = time.time() - connect_time
                if self._metrics.last_message_time > 0:
                    silence = time.time() - self._metrics.last_message_time
                    if silence > stale_threshold:
                        logger.warning(f"[{self.config.name}] No messages for {silence:.0f}s, reconnecting")
                        break
                    else:
                        logger.debug(f"💓 [{self.config.name}] Connection alive (uptime={uptime:.0f}s, last_msg={silence:.0f}s ago)")
                else:
                    logger.debug(f"💓 [{self.config.name}] Connection alive (uptime={uptime:.0f}s, waiting for first message)")
                        
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
        """Resubscribe to all channels after reconnect - INSTANT"""
        all_channels = list(self._subscriptions | self._pending_subscriptions)
        self._subscriptions.clear()
        
        if not all_channels:
            return
        
        # Feuere ALLE Resubscriptions gleichzeitig ab
        async def resubscribe_single(channel):
            try:
                msg = {"type": "subscribe", "channel": channel}
                await self._ws.send(json.dumps(msg))
                self._subscriptions.add(channel)
            except Exception as e:
                logger.error(f"[{self.config.name}] Resubscription error: {e}")
        
        await asyncio.gather(*[resubscribe_single(ch) for ch in all_channels])
        logger.info(f"[{self.config.name}] Bulk resubscribed to {len(all_channels)} channels")


class WebSocketManager:
    """
    Manages multiple WebSocket connections for X10 and Lighter.
    
    Features:
    - Auto-reconnect with exponential backoff
    - Health monitoring and metrics
    - Subscription management
    - Message routing to handlers
    """
    
    # Exchange WebSocket URLs
    LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
    X10_WS_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"


    
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
    
    async def start(self, ping_interval: Optional[float] = None, ping_timeout: Optional[float] = None):
        """Start all WebSocket connections
        
        Args:
            ping_interval: WebSocket ping interval in seconds (for X10 connection, default: None)
            ping_timeout: WebSocket ping timeout in seconds (for X10 connection, default: None)
        """
        if self._running:
            return
        
        self._running = True
        
        # Create Lighter connection
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
        
        # Create X10 connection
        x10_headers = {
            "X-Api-Key": getattr(config, "X10_API_KEY", ""),
            "User-Agent": "X10PythonTradingClient/0.4.5",
        }
        x10_config = WSConfig(
            url=self.X10_WS_URL,
            name="x10",
            # Use provided ping settings or default to None (no client-initiated pings)
            # If ping_interval and ping_timeout are provided, use them to prevent ping timeouts
            ping_interval=ping_interval,  # Client-initiated pings if specified
            ping_timeout=ping_timeout,   # Ping timeout if specified
            headers=x10_headers,
        )
        self._connections["x10"] = ManagedWebSocket(
            x10_config,
            self._handle_message
        )
        
        # Start all connections
        await asyncio.gather(*[
            conn.start() for conn in self._connections.values()
        ])
        
        logger.info("✅ WebSocketManager started")
    
    async def stop(self):
        """Stop all WebSocket connections"""
        self._running = False
        
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
    
    async def subscribe_x10(self, channels: List[str]):
        """Subscribe to X10 channels"""
        conn = self._connections. get("x10")
        if conn:
            for channel in channels:
                await conn.subscribe(channel)
    
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
        
        # X10 - INSTANT BULK SUBSCRIPTION (wie offizielles SDK)
        x10_conn = self._connections.get("x10")
        if x10_conn:
            # Sammle alle Subscription-Tasks
            subscription_tasks = []
            for symbol in symbols:
                market = symbol.replace("-", "/")
                subscription_tasks.append(x10_conn.subscribe(f"publicTrades/{market}"))
                subscription_tasks.append(x10_conn.subscribe(f"funding/{market}"))
            
            # Feuere ALLE Subscriptions gleichzeitig ab - KEINE DELAYS!
            await asyncio.gather(*subscription_tasks)
            logger.info(f"[x10] Bulk subscribed to {len(symbols)} markets instantly")
    
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
            elif source == "x10":
                await self._handle_x10_message(msg)
            
            # Call registered handlers
            handlers = self._message_handlers.get(source, [])
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
        """Process Lighter orderbook update"""
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
                
                if bids:
                    self.lighter_adapter._orderbook_cache[symbol] = {
                        'bids': bids,
                        'asks': asks,
                        'timestamp': time.time()
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
        """Handle X10 WebSocket messages"""
        msg_type = msg. get("type", "")
        
        # Mark price
        if msg_type == "MP":
            await self._handle_x10_mark_price(msg)
        
        # Funding
        elif "funding" in str(msg. get("channel", "")).lower():
            await self._handle_x10_funding(msg)
        
        # Orderbook
        elif "orderbook" in str(msg.get("channel", "")). lower():
            await self._handle_x10_orderbook(msg)
        
        # Public Trades
        elif "publicTrades" in str(msg.get("channel", "")):
            await self._handle_x10_trade(msg)
        
        # Open Interest
        elif msg. get("channel") == "open_interest":
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
        """Process X10 funding rate"""
        data = msg.get("data", {})
        market = data.get("m", "")
        rate = data.get("f")
        
        if market and rate:
            symbol = market.replace("/", "-")
            if self.x10_adapter:
                self. x10_adapter._funding_cache[symbol] = float(rate)
                self.x10_adapter._funding_cache_time[symbol] = time. time()
    
    async def _handle_x10_orderbook(self, msg: dict):
        """Process X10 orderbook"""
        data = msg.get("data", {})
        market = data.get("m", data.get("market", ""))
        
        if market:
            symbol = market.replace("/", "-")
            bids = data.get("b", data.get("bid", []))
            asks = data.get("a", data. get("ask", []))
            
            if self.x10_adapter and (bids or asks):
                self.x10_adapter._orderbook_cache[symbol] = {
                    'bids': bids,
                    'asks': asks,
                    'timestamp': time.time()
                }
    
    async def _handle_x10_open_interest(self, msg: dict):
        """Process X10 open interest"""
        market = msg.get("market", ""). replace("/", "-")
        oi = msg.get("open_interest")
        
        if market and oi:
            if self.oi_tracker:
                self.oi_tracker.update_from_websocket(market, "x10", float(oi))
            
            if self.predictor:
                self.predictor.update_oi_velocity(market, float(oi))

    async def _handle_x10_trade(self, msg: dict):
        """Process X10 public trades"""
        data = msg.get("data", [])
        # publicTrades liefert eine Liste von Trades
        if not isinstance(data, list):
            data = [data]
    
        for trade in data:
            market = msg.get("channel", "").replace("publicTrades/", "").replace("/", "-")
            price = trade.get("p")
            if market and price and self.x10_adapter:
                self.x10_adapter._price_cache[market] = float(price)
                self.x10_adapter._price_cache_time[market] = time.time()

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
                'state': conn.state. value,
                'connected': conn.is_connected,
                'metrics': {
                    'messages_received': conn.metrics.messages_received,
                    'messages_sent': conn.metrics.messages_sent,
                    'reconnect_count': conn.metrics.reconnect_count,
                    'uptime_seconds': conn.metrics.uptime_seconds,
                    'last_error': conn.metrics. last_error
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
