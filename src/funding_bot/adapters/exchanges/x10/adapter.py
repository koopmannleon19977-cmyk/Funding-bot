"""
X10 Exchange Adapter.

Implements ExchangePort using the official X10 Python SDK (starknet branch).
Handles all SDK type conversions to domain types.

SDK: https://github.com/x10xchange/python_sdk/tree/starknet
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import aiohttp

from funding_bot.config.settings import Settings
from funding_bot.domain.errors import (
    ExchangeError,
    InsufficientBalanceError,
    OrderRejectedError,
)
from funding_bot.domain.models import (
    Balance,
    Exchange,
    FundingRate,
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.utils import json_dumps, json_loads
from funding_bot.utils.decimals import (
    X10_FUNDING_RATE_CAP,
    clamp_funding_rate,
)
from funding_bot.utils.decimals import (
    safe_decimal as _safe_decimal,
)

logger = get_logger(__name__)


def _round_to_increment(value: Decimal, increment: Decimal, *, rounding: str) -> Decimal:
    """
    Round `value` to a multiple of `increment`.

    NOTE: Exchanges often enforce non-power-of-10 increments (e.g. qty step=100).
    `Decimal.quantize()` only matches exponent/decimal places, so we must round by division.
    """
    if increment <= 0:
        return value
    if value <= 0:
        return value

    q = value / increment
    n = q.to_integral_value(rounding=ROUND_CEILING if rounding == "ceil" else ROUND_FLOOR)
    return n * increment


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """
    Get attribute from object or dict safely.

    This handles the SDK returning either dicts or objects.
    Fixes the 'Position object has no attribute get' error.
    """
    if obj is None:
        return default

    # Try dict access first
    if isinstance(obj, dict):
        return obj.get(key, default)

    # Try attribute access
    if hasattr(obj, key):
        return getattr(obj, key, default)

    # Try lowercase/snake_case variants
    snake_key = key.replace("-", "_").lower()
    if hasattr(obj, snake_key):
        return getattr(obj, snake_key, default)

    return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Best-effort bool parsing for SDK payloads (dicts or objects)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _normalize_x10_api_v1_base_url(base_url: str) -> str:
    """
    Normalize a user-provided X10 base URL to the REST API v1 base URL.

    Examples:
    - https://api.starknet.extended.exchange            -> https://api.starknet.extended.exchange/api/v1
    - https://api.starknet.extended.exchange/api/v1    -> https://api.starknet.extended.exchange/api/v1
    - https://api.starknet.sepolia.extended.exchange/  -> https://api.starknet.sepolia.extended.exchange/api/v1
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def _normalize_x10_onboarding_url(base_url: str) -> str:
    """Normalize to the host-level onboarding URL (no `/api/v1`)."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return base[: -len("/api/v1")]
    return base


def _looks_like_x10_testnet(base_url: str) -> bool:
    """Best-effort detection to switch SDK endpoint config."""
    b = (base_url or "").strip().lower()
    return ("sepolia" in b) or ("testnet" in b)


class X10Adapter(ExchangePort):
    """
    X10 Exchange adapter.

    Uses the official SDK for StarkNet signing and trading.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connected = False

        # API Configuration
        self._api_key = settings.x10.api_key or os.getenv("X10_API_KEY", "")
        self._private_key = settings.x10.private_key or os.getenv("X10_PRIVATE_KEY", "")
        self._public_key = settings.x10.public_key or os.getenv("X10_PUBLIC_KEY", "")
        self._vault_id = settings.x10.vault_id or os.getenv("X10_VAULT_ID", "")

        # SDK clients (lazy init)
        self._trading_client: Any = None
        self._stream_client: Any = None

        # HTTP session for manual REST calls (reused for performance)
        self._http_session: aiohttp.ClientSession | None = None

        # Caches
        self._markets: dict[str, MarketInfo] = {}
        self._markets_loaded_at: datetime | None = None  # Track when markets were last loaded
        self._price_cache: dict[str, Decimal] = {}
        self._funding_cache: dict[str, FundingRate] = {}
        self._orderbook_cache: dict[str, dict[str, Decimal]] = {}
        self._orderbook_depth_cache: dict[str, dict[str, list[tuple[Decimal, Decimal]]]] = {}
        self._price_updated_at: dict[str, datetime] = {}
        self._orderbook_updated_at: dict[str, datetime] = {}
        self._orderbook_depth_updated_at: dict[str, datetime] = {}
        self._position_cache: dict[str, Position] = {}
        self._position_cache_updated_at: datetime | None = None
        self._symbol_map: dict[str, str] = {}  # Map base asset -> market name (e.g. "NEAR" -> "NEAR-USD")
        self._orderbook_snapshot_supports_limit: bool | None = None
        self._orderbook_snapshot_limit_logged: bool = False
        # Rate limiting state
        self._rate_limited_until: float = 0.0
        self._rate_limit_backoff: float = 3.0  # Start with 3 second backoff (reduced from 30s)
        self._max_backoff: float = 30.0  # Cap at 30 seconds
        self._last_successful_refresh: float = 0.0
        # Dynamic rate limit: Will be recalculated in _calculate_safe_request_delay()
        # Initial safe default: 0.1s (600/min) assuming ~10 markets
        self._request_delay: float = 0.1
        self._request_lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        self._sdk_call_timeout_seconds: float = float(
            getattr(self.settings.execution, "x10_sdk_call_timeout_seconds", 8.0) or 8.0
        )

        # Callbacks
        self._position_callbacks: list[Callable[[Position], Awaitable[None]]] = []
        self._order_callbacks: list[Callable[[Order], Awaitable[None]]] = []

        # Fees cache
        self._maker_fee: Decimal | None = None
        self._taker_fee: Decimal | None = None
        # Recent order metadata (helps when REST order lookup lags/404s).
        self._submitted_order_by_id: dict[str, Order] = {}
        self._client_order_id_by_order_id: dict[str, str] = {}
        self._order_cache_max_entries: int = 500

        # WebSocket
        self._stream_client_cls: Any = None
        self._ws_task: asyncio.Task | None = None  # account updates
        self._ws_ready = asyncio.Event()
        self._ws_ready_logged = False
        self._ob_ws_task: asyncio.Task | None = None  # public orderbooks
        self._ob_ws_allowlist: set[str] | None = None  # market symbols (e.g. "ETH-USD")
        self._ob_ws_markets: set[str] | None = None  # active subscriptions
        # L1 orderbook stream uses depth=1 for low-latency best bid/ask updates.
        # Full depth is fetched on-demand via REST.
        self._ob_ws_depth: int | None = 1
        self._endpoint_config: Any = None

        # Per-market WebSocket infrastructure (on-demand subscriptions like Lighter pattern)
        self._per_market_ws_tasks: dict[str, asyncio.Task] = {}  # market -> ws task
        self._per_market_ws_ready: dict[str, asyncio.Event] = {}  # market -> ready event
        self._per_market_ws_lock: asyncio.Lock = asyncio.Lock()

        # Pruning / Lifecycle
        # Track last usage time (access or create) to auto-close unused sockets
        self._ob_ws_last_used: dict[str, float] = {}  # market -> monotonic time
        self._ob_ws_ttl_seconds: float = float(getattr(self.settings.websocket, "x10_ws_ttl_seconds", 60.0) or 60.0)

        # Session Heartbeat Task (Keep-Alive)
        self._heartbeat_task: asyncio.Task | None = None

        # 🚀 PERFORMANCE: WebSocket Fill Cache (instant fill detection)
        # Maps order_id -> (Order, timestamp) for immediate get_order() lookups.
        # Populated by WS account updates, avoiding slow REST API searches.
        # Cleaned up when order is terminal (filled/cancelled) or TTL expires.
        self._ws_fill_cache: dict[str, tuple[Order, float]] = {}
        self._ws_fill_cache_lock = asyncio.Lock()  # Thread-safe cache ops
        self._ws_fill_cache_ttl_seconds: float = 300.0  # 5 minutes TTL for stale entries

    @property
    def exchange(self) -> Exchange:
        return Exchange.X10

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def initialize(self) -> None:
        """Initialize the adapter."""
        if self._connected:
            return

        logger.info("Initializing X10 adapter...")

        try:
            # Initialize SDK
            await self._init_sdk()

            # Load markets
            await self.load_markets()

            # Calculate safe rate limit based on loaded markets
            self._calculate_safe_request_delay()

            # Warm up critical trading session to avoid first-request latency
            await self._warmup_trading_session()

            # Start Heartbeat Loop
            self._start_heartbeat_loop()

            self._connected = True
            logger.info(f"X10 adapter initialized with {len(self._markets)} markets")

        except Exception as e:
            logger.exception(f"Failed to initialize X10 adapter: {e}")
            await self.close()
            raise

    async def _warmup_trading_session(self) -> None:
        """
        Send a dummy request to OrderManagementModule to establish HTTPS connection.
        This prevents the first order from incurring 1.5s+ TLS handshake latency.
        """
        if not self._trading_client:
            return

        logger.info("Warming up X10 OrderManagement session (TLS handshake)...")
        try:
            # Attempt to cancel a non-existent order (0).
            # This forces the underlying aiohttp session to connect/handshake.
            # We don't use _sdk_call_with_retry to avoid cluttering logs with retries.
            await self._throttle_request(operation_name="warmup", priority=True)
            await self._trading_client.orders.cancel_order(0)
        except Exception:
            # We expect an error (Order not found), but the connection is now alive.
            pass

    async def _init_sdk(self) -> None:
        """Initialize X10 SDK components."""
        try:
            from x10.perpetual.accounts import StarkPerpetualAccount
            from x10.perpetual.configuration import MAINNET_CONFIG

            try:
                from x10.perpetual.configuration import TESTNET_CONFIG  # type: ignore
            except Exception:  # pragma: no cover - depends on installed SDK version
                TESTNET_CONFIG = None  # type: ignore[assignment]
            from x10.perpetual.stream_client.stream_client import PerpetualStreamClient
            from x10.perpetual.trading_client import PerpetualTradingClient

            # Select endpoint config based on configured base_url.
            configured_base_url = (self.settings.x10.base_url or "").strip()
            wants_testnet = _looks_like_x10_testnet(configured_base_url)
            if wants_testnet and TESTNET_CONFIG is None:
                logger.warning("X10 TESTNET requested but SDK has no TESTNET_CONFIG; falling back to MAINNET_CONFIG")
            base_cfg = TESTNET_CONFIG if (wants_testnet and TESTNET_CONFIG is not None) else MAINNET_CONFIG

            api_base_url = _normalize_x10_api_v1_base_url(configured_base_url)
            onboarding_url = _normalize_x10_onboarding_url(configured_base_url)
            if api_base_url:
                base_cfg = replace(base_cfg, api_base_url=api_base_url)
            if onboarding_url:
                base_cfg = replace(base_cfg, onboarding_url=onboarding_url)

            self._endpoint_config = base_cfg
            self._stream_client_cls = PerpetualStreamClient
            try:
                env = "TESTNET" if wants_testnet else "MAINNET"
                logger.info(
                    "X10 endpoint config selected: env=%s api_base_url=%s stream_url=%s",
                    env,
                    getattr(self._endpoint_config, "api_base_url", "<unknown>"),
                    getattr(self._endpoint_config, "stream_url", "<unknown>"),
                )
            except Exception:
                pass

            # Create StarkNet account
            if self._vault_id:
                vault_id = int(str(self._vault_id).strip())
                self._stark_account = StarkPerpetualAccount(
                    vault=vault_id,
                    private_key=self._private_key,
                    public_key=self._public_key,
                    api_key=self._api_key,
                )
                logger.debug("X10 StarkNet account created")

                # Create trading client with account
                self._trading_client = PerpetualTradingClient(
                    endpoint_config=self._endpoint_config,
                    stark_account=self._stark_account,
                )
                logger.debug("X10 SDK initialized")
            else:
                logger.warning("X10 Vault ID not configured")
                self._trading_client = None

        except ImportError:
            logger.warning("X10 SDK not installed. Install with: pip install x10-python-trading-starknet")
            raise ExchangeError("X10 SDK not installed", exchange="X10") from None
        except Exception as e:
            logger.exception(f"X10 SDK init failed: {e}")
            raise

    async def close(self) -> None:
        """Close connections with timeouts to prevent hanging."""
        self._connected = False
        close_timeout = 5.0  # Max seconds to wait for each task

        async def _cancel_task_with_timeout(task: asyncio.Task | None, name: str) -> None:
            """Cancel a task with a timeout."""
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=close_timeout)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.debug(f"Error closing {name}: {e}")

        # Cancel main WS tasks
        await _cancel_task_with_timeout(self._ws_task, "ws_task")
        self._ws_task = None
        self._ws_ready.clear()
        self._ws_ready_logged = False

        await _cancel_task_with_timeout(self._ob_ws_task, "ob_ws_task")
        self._ob_ws_task = None

        if self._stream_client:
            # SDK `PerpetualStreamClient` has no `close()`. Active connections are owned by the
            # tasks above and are closed by exiting the async context manager on task cancellation.
            self._stream_client = None

        # Close trading client with timeout
        if self._trading_client:
            try:
                await asyncio.wait_for(self._trading_client.close(), timeout=close_timeout)
            except (TimeoutError, Exception) as e:
                logger.debug(f"Trading client close timeout/error: {e}")
            self._trading_client = None

        if self._http_session:
            with contextlib.suppress(Exception):
                await self._http_session.close()
            self._http_session = None

        await _cancel_task_with_timeout(self._heartbeat_task, "heartbeat_task")
        self._heartbeat_task = None

        # Cleanup per-market WS tasks (in parallel with timeout)
        per_market_tasks = [
            (market, task) for market, task in list(self._per_market_ws_tasks.items()) if task and not task.done()
        ]
        if per_market_tasks:
            for _market, task in per_market_tasks:
                task.cancel()
            # Wait for all cancellations with a single timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[task for _, task in per_market_tasks], return_exceptions=True),
                    timeout=close_timeout,
                )
            except TimeoutError:
                logger.debug(f"Timeout waiting for {len(per_market_tasks)} per-market WS tasks")

        self._per_market_ws_tasks.clear()
        self._per_market_ws_ready.clear()
        self._ob_ws_last_used.clear()

        logger.info("X10 adapter closed")

    def _start_heartbeat_loop(self) -> None:
        """Start the background heartbeat task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="X10_Heartbeat")

    async def _heartbeat_loop(self) -> None:
        """
        Periodically send a valid GET request to keep the HTTPS session alive.
        Target: OrderManagementModule session (Critical path)
        Endpoint: /user/client/info (Lightweight, Authenticated)
        """
        # Local imports to avoid circular/top-level issues if SDK is optional
        try:
            from x10.perpetual.clients import ClientModel
            from x10.utils.http import send_get_request
        except ImportError:
            logger.warning("X10 SDK imports failed in heartbeat loop")
            return

        logger.info("X10 heartbeat loop started (Session Hijack Mode)")
        while self._connected:
            try:
                await asyncio.sleep(25)
                if not self._connected or not self._trading_client:
                    break

                # Hijack the session from OrderManagementModule
                # This is the session we need to keep warm for place_order
                orders_module = self._trading_client.orders
                session = await orders_module.get_session()
                api_key = orders_module._get_api_key()

                # Construct URL for a safe GET endpoint
                # /user/client/info is used by AccountModule to get tier/fees
                # It is lightweight, authenticated, and returns 200 OK.
                url = orders_module._get_url("/user/client/info")

                # Check rate limit (though this is low frequency)
                await self._throttle_request(operation_name="heartbeat_hijack", priority=True)

                # Send Authenticated GET using the Orders Session
                # This validates the connection, keeps SDK's session pool/SSL config active,
                # and ensures the server sees a valid request to keep Keep-Alive open.
                await send_get_request(session, url, ClientModel, api_key=api_key)

            except asyncio.CancelledError:
                logger.debug("X10 heartbeat loop cancelled")
                break
            except Exception as e:
                logger.debug(f"X10 heartbeat error (non-fatal): {e}")
                await asyncio.sleep(5)  # Short backoff

    # =========================================================================
    # Market Data
    # =========================================================================

    async def refresh_all_market_data(self) -> None:
        """
        Batch refresh all market data from a single get_markets() call.
        This is the key optimization to avoid rate limiting.
        """
        if not self._trading_client:
            return

        # Check if we're in rate-limit backoff
        import time

        now = time.time()
        if now < self._rate_limited_until:
            remaining = int(self._rate_limited_until - now)
            logger.debug(f"X10 rate-limited, waiting {remaining}s before retry")
            return

        try:
            response = await self._sdk_call_with_retry(
                lambda: self._trading_client.markets_info.get_markets(),
                operation_name="get_markets",
            )
            markets = getattr(response, "data", response) or []

            for m in markets:
                symbol = _get_attr(m, "name", "")
                if not symbol or not _get_attr(m, "active", True):
                    continue

                market_stats = _get_attr(m, "market_stats", {})
                trading_config = _get_attr(m, "trading_config", {})

                # Cache market info
                if symbol not in self._markets:
                    base_asset = _get_attr(m, "asset_name", symbol.split("-")[0] if "-" in symbol else symbol)
                    quote_asset = _get_attr(m, "collateral_asset_name", "USD")
                    asset_precision = int(_get_attr(m, "asset_precision", 0))
                    collateral_precision = int(_get_attr(m, "collateral_asset_precision", 6))

                    self._markets[symbol] = MarketInfo(
                        symbol=symbol,
                        exchange=Exchange.X10,
                        base_asset=base_asset,
                        quote_asset=quote_asset,
                        price_precision=collateral_precision,
                        qty_precision=asset_precision,
                        tick_size=_safe_decimal(_get_attr(trading_config, "min_price_change"), Decimal("0.00001")),
                        step_size=_safe_decimal(_get_attr(trading_config, "min_order_size_change"), Decimal("1")),
                        min_qty=_safe_decimal(_get_attr(trading_config, "min_order_size"), Decimal("0")),
                        max_qty=_safe_decimal(_get_attr(trading_config, "max_limit_order_value"), Decimal("1000000")),
                        min_notional=Decimal("10"),
                        maker_fee=self.settings.trading.maker_fee_x10,
                        taker_fee=self.settings.trading.taker_fee_x10,
                        # Dynamic Leverage
                        x10_max_leverage=_safe_decimal(_get_attr(trading_config, "max_leverage"), Decimal("5")),
                    )

                    # Update symbol map
                    self._symbol_map[base_asset] = symbol
                    self._symbol_map[symbol] = symbol  # Self-map for safety

                # Cache price
                price = _safe_decimal(
                    _get_attr(market_stats, "mark_price")
                    or _get_attr(market_stats, "last_price")
                    or _get_attr(market_stats, "index_price")
                )
                if price > 0:
                    self._price_cache[symbol] = price
                    self._price_updated_at[symbol] = datetime.now(UTC)

                # Cache funding rate
                # X10 API returns hourly rate as DECIMAL (e.g., -0.000360 = -31.5% annualized).
                # NO division by 100 - API already returns decimal format.
                # Ref: https://api.docs.extended.exchange/#funding-rates
                raw_rate = _safe_decimal(_get_attr(market_stats, "funding_rate"))
                funding_rate = raw_rate  # Already in decimal format

                # Validate against documented cap (±3%/hour max for all asset groups)
                funding_rate = clamp_funding_rate(funding_rate, X10_FUNDING_RATE_CAP, symbol, "X10")

                next_funding = _get_attr(market_stats, "next_funding_rate")
                next_time = None
                if next_funding:
                    with contextlib.suppress(ValueError, TypeError):
                        next_time = datetime.fromtimestamp(int(next_funding) / 1000, tz=UTC)

                self._funding_cache[symbol] = FundingRate(
                    symbol=symbol,
                    exchange=Exchange.X10,
                    rate=funding_rate,
                    next_funding_time=next_time,
                )

                # Cache bid/ask from market stats
                bid_price = _safe_decimal(_get_attr(market_stats, "bid_price"))
                ask_price = _safe_decimal(_get_attr(market_stats, "ask_price"))
                if bid_price > 0 or ask_price > 0:
                    existing = self._orderbook_cache.get(symbol, {})
                    # Important: market stats don't include L1 qty. Do not overwrite a previously depth-valid cache.
                    bid_qty = _safe_decimal(existing.get("bid_qty"), Decimal("0"))
                    ask_qty = _safe_decimal(existing.get("ask_qty"), Decimal("0"))
                    self._orderbook_cache[symbol] = {
                        "best_bid": bid_price
                        if bid_price > 0
                        else _safe_decimal(existing.get("best_bid"), Decimal("0")),
                        "best_ask": ask_price
                        if ask_price > 0
                        else _safe_decimal(existing.get("best_ask"), Decimal("0")),
                        "bid_qty": bid_qty,
                        "ask_qty": ask_qty,
                    }

            # Success - reset backoff and update market cache timestamp
            self._markets_loaded_at = datetime.now(UTC)  # Update market cache timestamp
            self._rate_limit_backoff = 3.0  # Reset to initial backoff on success
            self._last_successful_refresh = now
            logger.debug(f"X10 batch refresh: {len(self._markets)} markets, {len(self._price_cache)} prices")

        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = "429" in error_str or "rate limit" in error_str or "too many" in error_str
            is_transient_5xx = any(c in error_str for c in ["500", "502", "503", "504"])
            is_html_proxy = "<!doctype" in error_str or "<html" in error_str

            if is_rate_limit:
                # Rate limited - apply exponential backoff
                self._rate_limited_until = now + self._rate_limit_backoff
                logger.warning(f"X10 rate-limited, backing off for {self._rate_limit_backoff:.0f}s")
                self._rate_limit_backoff = min(self._rate_limit_backoff * 2, self._max_backoff)
            elif is_transient_5xx or is_html_proxy:
                # Transient server error - don't update backoff, just skip this cycle
                logger.warning(f"X10 transient error on market refresh: {str(e)[:100]}")
            else:
                logger.warning(f"X10 batch refresh failed: {e}")

    async def load_markets(self) -> dict[str, MarketInfo]:
        """Load all available markets.

        🚀 PERFORMANCE PREMIUM-OPTIMIZED: Markets are cached and only refreshed
        after market_cache_ttl_seconds (default: 1 hour). Exchange metadata
        (tick sizes, min qty, etc.) rarely changes, so aggressive caching is safe.
        """
        if not self._trading_client:
            return {}

        # Check if cache is still valid
        if self._markets and not self._is_market_cache_stale():
            return self._markets

        # Use batch refresh which populates all caches
        await self.refresh_all_market_data()
        return self._markets

    async def get_market_info(self, symbol: str) -> MarketInfo | None:
        """Get market metadata for a symbol."""
        if not self._markets or self._is_market_cache_stale():
            await self.load_markets()
        return self._markets.get(symbol)

    def _is_market_cache_stale(self) -> bool:
        """Check if the market cache is stale based on TTL setting."""
        if self._markets_loaded_at is None:
            return True  # No cache yet

        # Get cache TTL from settings (default: 1 hour)
        cache_ttl = float(
            getattr(
                getattr(getattr(self.settings, "market_data", None), "market_cache_ttl_seconds", None),
                "market_cache_ttl_seconds",
                3600.0,
            )
            or 3600.0
        )

        # Check if cache has expired
        age = (datetime.now(UTC) - self._markets_loaded_at).total_seconds()
        return age > cache_ttl

    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
        """
        Get best bid/ask.
        Returns cached data if available and fresh (< 2s), otherwise fetches from REST.

        Phase 3.1 Optimization: Auto-start WebSocket for 10ms updates (depth=1).
        """
        if not self._trading_client:
            if symbol in self._orderbook_cache:
                return self._orderbook_cache[symbol]
            raise ExchangeError("Trading client not initialized", exchange="X10")

        # Phase 3.1: Auto-start per-market WebSocket for this symbol (non-blocking).
        # This ensures subsequent calls benefit from 10ms WS updates instead of REST.
        market_name = self._resolve_market_name(symbol)
        ws_task = self._per_market_ws_tasks.get(market_name)
        if ws_task is None or ws_task.done():
            # Start WebSocket in background (don't wait - first call uses REST)
            asyncio.create_task(self.ensure_orderbook_ws(market_name, timeout=0))

        # Check cache freshness (Turbo Mode)
        # FIX: Only use WS cache if it has BOTH prices AND quantities.
        # WS cache from `refresh_all_market_data()` only has prices (no qty), causing
        # depth gates to reject with `cap_max_notional_usd=0`.
        cached = self._orderbook_cache.get(market_name) or self._orderbook_cache.get(symbol)
        last_update = self._orderbook_updated_at.get(market_name) or self._orderbook_updated_at.get(symbol)
        if cached and last_update:
            age = (datetime.now(UTC) - last_update).total_seconds()
            # If cache is very fresh AND depth-valid (has qty), return immediately.
            cache_has_depth = cached.get("bid_qty", Decimal("0")) > 0 and cached.get("ask_qty", Decimal("0")) > 0
            if age < 2.0 and cache_has_depth:
                return cached
            # Otherwise, fall through to REST to get fresh L1 with quantities.

        try:
            # X10 SDK: markets_info.get_orderbook_snapshot expects keyword argument
            response = await self._get_orderbook_snapshot(
                market_name=market_name,
                limit=1,
                operation_name="get_orderbook_snapshot_l1",
            )
            book = getattr(response, "data", response)

            # X10 SDK uses 'bid' and 'ask' (singular), not 'bids'/'asks'
            # OrderbookUpdateModel has: bid: List[OrderbookQuantityModel], ask: List[OrderbookQuantityModel]
            # Each OrderbookQuantityModel has: price, qty
            bids = _get_attr(book, "bid", []) or _get_attr(book, "bids", [])
            asks = _get_attr(book, "ask", []) or _get_attr(book, "asks", [])

            # Parse bids/asks - format can be list of tuples/lists [price, qty] or objects
            best_bid = Decimal("0")
            best_ask = Decimal("0")
            bid_qty = Decimal("0")
            ask_qty = Decimal("0")

            if bids:
                if isinstance(bids[0], (list, tuple)):
                    best_bid = _safe_decimal(bids[0][0])
                    bid_qty = _safe_decimal(bids[0][1]) if len(bids[0]) > 1 else Decimal("0")
                else:
                    # OrderbookQuantityModel has .price and .qty
                    best_bid = _safe_decimal(_get_attr(bids[0], "price"))
                    bid_qty = _safe_decimal(_get_attr(bids[0], "qty") or _get_attr(bids[0], "size"))

            if asks:
                if isinstance(asks[0], (list, tuple)):
                    best_ask = _safe_decimal(asks[0][0])
                    ask_qty = _safe_decimal(asks[0][1]) if len(asks[0]) > 1 else Decimal("0")
                else:
                    # OrderbookQuantityModel has .price and .qty
                    best_ask = _safe_decimal(_get_attr(asks[0], "price"))
                    ask_qty = _safe_decimal(_get_attr(asks[0], "qty") or _get_attr(asks[0], "size"))

            result = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_qty": bid_qty,
                "ask_qty": ask_qty,
            }

            # Only cache if the snapshot is depth-valid (prevents poisoning the cache with zeros/partials).
            if best_bid > 0 and best_ask > 0 and best_bid < best_ask and bid_qty > 0 and ask_qty > 0:
                now = datetime.now(UTC)
                for key in {market_name, symbol}:
                    if not key:
                        continue
                    self._orderbook_cache[key] = result
                    self._orderbook_updated_at[key] = now
                    self._price_cache[key] = (best_bid + best_ask) / 2
                    self._price_updated_at[key] = now
                return result

            # If we already have any cached book (even quote-only from market stats), keep it instead of degrading.
            cached = self._orderbook_cache.get(market_name) or self._orderbook_cache.get(symbol)
            if isinstance(cached, dict) and cached:
                return cached

            # Return the partial result (do not cache it).
            return result

        except Exception as e:
            # Never synthesize "L1 depth" with qty=0 here: it causes false depth rejects upstream.
            if symbol in self._orderbook_cache:
                return self._orderbook_cache[symbol]
            raise ExchangeError(
                f"Failed to get orderbook for {symbol}: {e}",
                exchange="X10",
                symbol=symbol,
            ) from e

    async def get_orderbook_depth(self, symbol: str, depth: int = 20) -> dict[str, list[tuple[Decimal, Decimal]]]:
        """
        Get top-N orderbook levels (bids/asks) as (price, qty) tuples.

        Optimized to use WebSocket cache if available and fresh (<2s), otherwise falls back to REST.
        For depth checks that only need freshness (staleness gates), L1 from per-market WS is acceptable.
        """
        if not self._trading_client:
            raise ExchangeError("Trading client not initialized", exchange="X10", symbol=symbol)

        market_name = self._resolve_market_name(symbol)

        # Check cache freshness (Turbo Mode) - prioritize per-market WS cache
        # FIX: Only use cache if it has ENOUGH depth levels for the request.
        # WebSocket with depth=1 only caches L1 - don't use it for multi-level depth requests!
        cached = self._orderbook_depth_cache.get(market_name) or self._orderbook_depth_cache.get(symbol)
        last_update = self._orderbook_depth_updated_at.get(market_name) or self._orderbook_depth_updated_at.get(symbol)

        if cached and last_update:
            age = (datetime.now(UTC) - last_update).total_seconds()
            if age < 2.0:
                bids = cached.get("bids", [])
                asks = cached.get("asks", [])
                cached_depth = min(len(bids), len(asks))

                # FIX: Only use cache if it has enough depth levels
                # If depth=1 (L1 only), cache is valid for L1 requests
                # If depth>1, cache must have at least that many levels
                requested_depth = depth or 1
                if cached_depth >= requested_depth and cached_depth > 0:
                    logger.debug(
                        f"[{market_name}] Using cached depth (age={age:.3f}s, "
                        f"levels={cached_depth}, requested={requested_depth})"
                    )
                    bids_to_return = bids if depth is None or depth >= len(bids) else bids[:depth]
                    asks_to_return = asks if depth is None or depth >= len(asks) else asks[:depth]
                    return {
                        "bids": bids_to_return,
                        "asks": asks_to_return,
                        "updated_at": last_update,
                    }
                elif cached_depth > 0:
                    # Cache exists but doesn't have enough levels - skip to REST
                    logger.debug(
                        f"[{market_name}] Cache has {cached_depth} levels but need {requested_depth}, "
                        f"falling back to REST"
                    )

        # Log if we had cache but it was stale
        if cached:
            age = (datetime.now(UTC) - (last_update or datetime.min.replace(tzinfo=UTC))).total_seconds()
            if age < 10.0:  # excessive logging check
                logger.debug(f"[{market_name}] Cached depth stale (age={age:.1f}s), falling back to REST")

        try:
            limit = int(depth or 1)
            limit = max(1, min(limit, 200))

            # FIX: Pass limit parameter to X10 SDK to get requested depth levels
            # Without limit, X10 may return empty or incomplete data for low-volume symbols
            response = await self._get_orderbook_snapshot(
                market_name=market_name,
                limit=limit,
                operation_name="get_orderbook_snapshot_depth",
            )
            book = getattr(response, "data", response)

            bids_raw = _get_attr(book, "bid", []) or _get_attr(book, "bids", [])
            asks_raw = _get_attr(book, "ask", []) or _get_attr(book, "asks", [])

            bids: list[tuple[Decimal, Decimal]] = []
            asks: list[tuple[Decimal, Decimal]] = []

            for row in list(bids_raw)[:limit]:
                if isinstance(row, (list, tuple)):
                    price = _safe_decimal(row[0])
                    qty = _safe_decimal(row[1]) if len(row) > 1 else Decimal("0")
                else:
                    price = _safe_decimal(_get_attr(row, "price"))
                    qty = _safe_decimal(_get_attr(row, "qty") or _get_attr(row, "size"))
                if price > 0 and qty > 0:
                    bids.append((price, qty))

            for row in list(asks_raw)[:limit]:
                if isinstance(row, (list, tuple)):
                    price = _safe_decimal(row[0])
                    qty = _safe_decimal(row[1]) if len(row) > 1 else Decimal("0")
                else:
                    price = _safe_decimal(_get_attr(row, "price"))
                    qty = _safe_decimal(_get_attr(row, "qty") or _get_attr(row, "size"))
                if price > 0 and qty > 0:
                    asks.append((price, qty))

            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])

            # Log depth levels returned for monitoring
            if len(bids) < limit or len(asks) < limit:
                logger.debug(
                    f"[{market_name}] X10 returned partial depth: "
                    f"{len(bids)} bids (requested {limit}), "
                    f"{len(asks)} asks (requested {limit})"
                )

            # REST fetch is considered fresh at time of fetch
            now = datetime.now(UTC)
            payload = {"bids": bids, "asks": asks, "updated_at": now}
            for key in {market_name, symbol}:
                if not key:
                    continue
                self._orderbook_depth_cache[key] = {"bids": bids, "asks": asks}
                self._orderbook_depth_updated_at[key] = now
            return payload

        except Exception as e:
            raise ExchangeError(
                f"Failed to get orderbook depth for {symbol}: {e}",
                exchange="X10",
                symbol=symbol,
            ) from e

    def _calculate_safe_request_delay(self) -> None:
        """
        Calculate and set safe request delay to prevent 429 errors.

        Formula:
        - Limit: 1000 requests/minute
        - Safety Buffer: 10% (Target: 900 req/min)
        - Overhead: 1 global refresh/sec = 60 req/min
        - Available for Polling: 900 - 60 = 840 req/min
        - Active Markets: N
        - Max Polls/Min/Market = 840 / N
        - Interval = 60 / Max_Polls
        """
        try:
            num_markets = len(self._markets)
            if num_markets == 0:
                self._request_delay = 0.1
                return

            limit_per_min = 1000.0
            safety_factor = 0.9
            target_rate = limit_per_min * safety_factor  # 900

            # Reserved for refresh_all_market_data (approx 1 per sec)
            overhead_per_min = 60.0

            available_rate = target_rate - overhead_per_min
            if available_rate <= 0:
                available_rate = 100.0  # Emergency minimal budget

            # Theoretical max rate per market if we were to poll all of them uniformly
            # We want the delay between ANY request.
            # So delay = 60 / target_rate
            # Wait, currently `_request_delay` is "time between any API call".
            # So we simply set it to ensure we don't exceed `target_rate` globally.

            # Delay = 60 seconds / 900 requests = 0.066s
            # This is the GLOBAL throttle.
            # If we have 20 markets and poll each every 1s, we need 1200 req/min.
            # 0.066s delay allows 900 req/min.
            # So polling 20 markets will naturally slow down to 1.33s (20 * 0.066).
            # This is exactly what we want: The throttle enforces the limit, slows down the loop.

            safe_delay = 60.0 / target_rate
            self._request_delay = max(safe_delay, 0.067)  # Floor at ~15 req/s

            logger.info(
                f"X10 Dynamic Rate Limit: {num_markets} markets. "
                f"Target Rate: {target_rate:.0f} req/min. "
                f"Set Interval: {self._request_delay:.4f}s"
            )

        except Exception as e:
            logger.error(f"Failed to calc rate limit: {e}")
            self._request_delay = 0.1

    # =========================================================================
    # SDK Retry Wrapper
    # =========================================================================

    async def _throttle_request(self, *, operation_name: str, priority: bool = False) -> None:
        """
        Global throttle to stay under X10 REST rate limits.

        Args:
            operation_name: Name of the operation for logging.
            priority: If True, bypass the global request lock and polite pacing (for critical orders).
        """
        # Always respect the hard 429 backoff ("penalty box")
        if time.time() < self._rate_limited_until:
            wait_time = self._rate_limited_until - time.time()
            logger.debug(f"X10 rate-limited (hard backoff), waiting {wait_time:.1f}s before {operation_name}")
            await asyncio.sleep(wait_time)

        # Priority Lock Bypass:
        # Critical orders (place/cancel) skip the serial queue and polite delay.
        # This prevents market data refreshes from blocking trading actions.
        if priority:
            # We update the timestamp so background tasks usually back off after our burst.
            # This assignment is atomic enough for our purposes.
            self._last_request_time = time.monotonic()
            return

        # Normal Pacing (Background Tasks)
        async with self._request_lock:
            # Re-check hard limit inside lock just in case? (Optional, but safe)
            if time.time() < self._rate_limited_until:
                wait_time = self._rate_limited_until - time.time()
                await asyncio.sleep(wait_time)

            if self._request_delay > 0:
                elapsed = time.monotonic() - self._last_request_time
                if elapsed < self._request_delay:
                    await asyncio.sleep(self._request_delay - elapsed)
                self._last_request_time = time.monotonic()

    async def _sdk_call_with_retry(
        self,
        coro_func: Callable[[], Awaitable[Any]],
        max_retries: int = 3,
        operation_name: str = "SDK call",
        priority: bool = False,
    ) -> Any:
        """
        Wrap X10 SDK calls with retry logic for rate limits and transient errors.

        Handles:
        - 429 Rate Limit → exponential backoff
        - 500/502/503/504 → retry with backoff
        - HTML responses (proxy errors) → retry
        - ValueError from SDK (often wraps HTTP errors)
        """
        import time

        from x10.utils.http import RateLimitException

        initial_backoff = 3.0  # seconds

        for attempt in range(max_retries):
            try:
                await self._throttle_request(operation_name=operation_name, priority=priority)

                if self._sdk_call_timeout_seconds and self._sdk_call_timeout_seconds > 0:
                    return await asyncio.wait_for(
                        coro_func(),
                        timeout=float(self._sdk_call_timeout_seconds),
                    )
                return await coro_func()

            except RateLimitException as e:
                backoff = initial_backoff * (2**attempt)
                self._rate_limited_until = time.time() + backoff
                logger.warning(
                    f"X10 rate-limited ({operation_name}), backing off {backoff:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    continue
                raise ExchangeError(f"Rate limit exceeded: {e}", exchange="X10") from e

            except ValueError as e:
                err_str = str(e).lower()
                # Detect transient 5xx or HTML proxy errors
                is_5xx = any(c in err_str for c in ["500", "502", "503", "504"])
                is_html = "<!doctype" in err_str or "<html" in err_str

                if is_5xx or is_html:
                    backoff = initial_backoff * (2**attempt)
                    logger.warning(
                        f"X10 transient error ({operation_name}): {str(e)[:100]}, "
                        f"retrying in {backoff:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        continue
                raise ExchangeError(f"SDK error: {e}", exchange="X10") from e

            except TimeoutError as te:
                backoff = initial_backoff * (2**attempt)
                logger.warning(
                    f"X10 timeout ({operation_name}), retrying in {backoff:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    continue
                raise ExchangeError(f"SDK timeout: {operation_name}", exchange="X10") from te

            except Exception:
                # Don't retry unknown exceptions
                raise

        raise ExchangeError(f"Max retries exceeded for {operation_name}", exchange="X10")

    # =========================================================================
    # Account
    # =========================================================================

    async def get_available_balance(self) -> Balance:
        """Get available margin/balance."""
        if not self._trading_client:
            raise ExchangeError("Trading client not initialized", exchange="X10")

        try:
            # Use retry wrapper for robustness against transient errors
            response = await self._sdk_call_with_retry(
                lambda: self._trading_client.account.get_balance(),
                operation_name="get_balance",
            )
            data = getattr(response, "data", response)

            # data typically has available_for_trade, equity, etc.
            available = _safe_decimal(
                _get_attr(data, "available_for_trade")
                or _get_attr(data, "available_amount")
                or _get_attr(data, "available_balance")
            )
            total = _safe_decimal(
                _get_attr(data, "equity") or _get_attr(data, "balance") or _get_attr(data, "total_collateral")
            )

            return Balance(
                exchange=Exchange.X10,
                available=available,
                total=total,
                currency="USD",
            )

        except Exception as e:
            logger.error(f"Failed to get X10 balance: {e}")
            raise ExchangeError(
                f"Failed to get balance: {e}",
                exchange="X10",
            ) from e

    async def get_fee_schedule(self, symbol: str | None = None) -> dict[str, Decimal]:
        """Get fee schedule."""
        # Use cached account fees if available
        if self._taker_fee is not None:
            return {
                "maker_fee": self._maker_fee or Decimal("0"),
                "taker_fee": self._taker_fee,
            }

        # Try to fetch from API
        if self._trading_client:
            try:
                # Use a default symbol to get account-level fees
                resp = await self._sdk_call_with_retry(
                    lambda: self._trading_client.account.get_fees(market_names=["BTC-USD"]),
                    operation_name="get_fees",
                )
                if resp.data:
                    fee = resp.data[0]
                    self._maker_fee = _safe_decimal(fee.maker_fee_rate)
                    self._taker_fee = _safe_decimal(fee.taker_fee_rate)
                    return {
                        "maker_fee": self._maker_fee,
                        "taker_fee": self._taker_fee,
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch account fees from X10: {e}")

        # Fallback to settings
        return {
            "maker_fee": self.settings.trading.maker_fee_x10,
            "taker_fee": self.settings.trading.taker_fee_x10,
        }

    # =========================================================================
    # Positions
    # =========================================================================

    async def list_positions(self) -> list[Position]:
        """Get all open positions."""
        if not self._trading_client:
            return list(self._position_cache.values())

        try:
            # Use retry wrapper for robustness against transient errors
            response = await self._sdk_call_with_retry(
                lambda: self._trading_client.account.get_positions(),
                operation_name="list_positions",
            )
            positions_data = getattr(response, "data", response) or []

            positions = []
            for p in positions_data:
                # Rule 5 Compliance: Use exact SDK field names
                # Source: x10/perpetual/positions.py -> PositionModel

                # 1. Size (Decimal) - 'qty' does not exist in SDK PositionModel
                qty = _safe_decimal(getattr(p, "size", 0))
                if qty == 0:
                    continue

                # 2. Side (Enum) - PositionSide.LONG / SHORT
                # Use string comparison to be safe against Enum type mismatch across
                # versions, but map STRICTLY to LONG/SHORT
                side_val = str(getattr(p, "side", "")).upper()
                if side_val == "LONG":
                    side = Side.BUY
                elif side_val == "SHORT":
                    side = Side.SELL
                else:
                    logger.warning(f"Unknown X10 PositionSide: {side_val}, defaulting to BUY")
                    side = Side.BUY

                symbol = _get_attr(p, "market") or _get_attr(p, "symbol", "")

                position = Position(
                    symbol=symbol,
                    exchange=Exchange.X10,
                    side=side,
                    qty=abs(qty),
                    # SDK uses 'open_price', NOT 'entry_price'
                    entry_price=_safe_decimal(getattr(p, "open_price", 0)),
                    mark_price=_safe_decimal(getattr(p, "mark_price", 0)),
                    liquidation_price=_safe_decimal(getattr(p, "liquidation_price", 0)) or None,
                    # SDK uses British 'unrealised_pnl'
                    unrealized_pnl=_safe_decimal(getattr(p, "unrealised_pnl", 0)),
                    leverage=_safe_decimal(getattr(p, "leverage", 1)),
                )

                positions.append(position)
                self._position_cache[symbol] = position

            self._position_cache_updated_at = datetime.now(UTC)
            return positions

        except Exception as e:
            logger.warning(f"Failed to list X10 positions: {e}")
            return list(self._position_cache.values())

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a specific symbol."""
        max_age_seconds = 2.0
        if self._position_cache_updated_at:
            age = (datetime.now(UTC) - self._position_cache_updated_at).total_seconds()
        else:
            age = 1e9

        # Check cache first when fresh
        if symbol in self._position_cache and age <= max_age_seconds:
            return self._position_cache[symbol]

        # Fetch fresh and check loosely
        positions = await self.list_positions()
        target_clean = symbol.replace("-USD", "")

        for p in positions:
            # Direct match
            if p.symbol == symbol:
                return p
            # Loose match (e.g. MEGA-USD vs MEGA)
            if p.symbol.replace("-USD", "") == target_clean:
                return p

        return None

    async def get_funding_history(
        self, symbol: str, start_time: datetime | None = None, limit: int = 100
    ) -> list[dict]:
        """
        Get itemized funding payment history for a symbol.

        Uses GET /user/funding/history API endpoint WITH PAGINATION.
        Returns list of funding payments with: fundingFee, side, size, markPrice, paidTime.

        Ref: https://api.docs.extended.exchange/#get-funding-payments

        IMPORTANT: API returns max 10,000 records but paginates at 100 per request.
        This method automatically fetches ALL pages to ensure complete funding history.
        """
        if not self._trading_client:
            return []

        try:
            import aiohttp

            # Resolve market name
            market_name = self._symbol_map.get(symbol, symbol)
            if market_name not in self._markets and f"{market_name}-USD" in self._markets:
                market_name = f"{market_name}-USD"

            # Build URL with query params
            cfg = getattr(self, "_endpoint_config", None)
            api_base_url = getattr(cfg, "api_base_url", None) if cfg else None
            if isinstance(api_base_url, str) and api_base_url.strip():
                api_base = api_base_url.strip().rstrip("/")
            else:
                api_base = _normalize_x10_api_v1_base_url(
                    self.settings.x10.base_url or "https://api.starknet.extended.exchange"
                ).rstrip("/")
                if not api_base:
                    api_base = "https://api.starknet.extended.exchange/api/v1"

            url = f"{api_base}/user/funding/history"

            headers = {"X-API-Key": self._api_key} if self._api_key else {}
            with contextlib.suppress(Exception):
                from x10.config import USER_AGENT

                headers.setdefault("User-Agent", USER_AGENT)

            # Reuse HTTP session for performance
            if not self._http_session:
                # 🚀 PERFORMANCE PREMIUM-OPTIMIZED: TCPConnector with aggressive connection pooling
                # Premium (24,000 req/min) allows 400x more concurrency than Standard (60 req/min)
                connector = aiohttp.TCPConnector(
                    limit=200,  # Total connections (increased from 100 for Premium)
                    limit_per_host=100,  # Max 100 concurrent connections to X10 API (was 30)
                    ttl_dns_cache=1800,  # Cache DNS results for 30 minutes (was 5 minutes)
                    use_dns_cache=True,  # Enable DNS caching to avoid repeated lookups
                    keepalive_timeout=300,  # Keep connections alive for 5 minutes (was 30s)
                    enable_cleanup_closed=True,  # Auto-cleanup closed connections
                    force_close=False,  # Enable keep-alive for connection reuse
                )
                self._http_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=30,  # Overall request timeout
                        connect=10,  # Connection establishment timeout (TLS handshake)
                        sock_read=5,  # Socket read timeout
                    ),
                )

            # ✅ FIX: Pagination Loop - Fetch ALL funding payments, not just first 100
            # API returns max 100 records per request but can have thousands total.
            # Previous bug: only fetched first page, losing ~37% of funding data.
            all_payments: list[dict] = []
            cursor: int | None = None
            max_pages = 200  # Safety: prevent infinite loop (200 pages * 100 = 20k records max)
            page_count = 0

            while page_count < max_pages:
                page_count += 1

                params: dict[str, Any] = {"market": market_name, "limit": limit}
                if start_time:
                    params["fromTime"] = int(start_time.timestamp() * 1000)
                if cursor is not None:
                    params["cursor"] = cursor

                # Transient failures happen in the wild (we've seen 500s in prod logs).
                # Retry a couple times per page to reduce partial results.
                transient_statuses = {429, 500, 502, 503, 504}
                page_attempts = 0
                data: dict | None = None

                while True:
                    await self._throttle_request(operation_name="funding_history")

                    async with self._http_session.get(url, params=params, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            break

                        if resp.status in transient_statuses and page_attempts < 2:
                            import asyncio
                            import random

                            page_attempts += 1
                            backoff = float(2 ** (page_attempts - 1))
                            delay = backoff + (backoff * random.uniform(0.0, 0.25))
                            logger.warning(
                                f"X10 funding history API returned {resp.status} (page {page_count}, attempt {page_attempts}/3). "
                                f"Retrying in {delay:.1f}s"
                            )
                            await asyncio.sleep(delay)
                            continue

                        logger.warning(f"X10 funding history API returned {resp.status}")
                        data = None
                        break

                if not data:
                    break

                payments = data.get("data", [])

                if not payments:
                    # No more data
                    break

                all_payments.extend(payments)

                # Check pagination info
                pagination = data.get("pagination", {})
                next_cursor = pagination.get("cursor")
                count = pagination.get("count", 0)

                logger.debug(
                    f"X10 funding history page {page_count}: {len(payments)} payments, "
                    f"cursor={next_cursor}, total_so_far={len(all_payments)}"
                )

                # If count < limit, we've reached the end
                if count < limit or next_cursor is None:
                    break

                cursor = next_cursor

            # Only log INFO if pagination was needed (page_count > 1), otherwise DEBUG
            # This reduces log spam from routine 30s funding checks
            if page_count > 1:
                logger.info(f"X10 funding history: {len(all_payments)} payments for {symbol} ({page_count} pages)")
            else:
                logger.debug(f"X10 funding history: {len(all_payments)} payments for {symbol}")
            return all_payments

        except Exception as e:
            logger.warning(f"Failed to get X10 funding history: {e}")
            return []

    async def get_realized_funding(self, symbol: str, start_time: datetime | None = None) -> Decimal:
        """
        Get cumulative realized funding payments for a symbol since start_time.

        IMPROVED: Now uses GET /user/funding/history for precise funding payments
        instead of relying on `realised_pnl` which mixes funding with execution PnL.

        Falls back to legacy `realised_pnl` if funding history API fails.
        """
        if not self._trading_client:
            return Decimal("0")

        try:
            # PREFERRED: Use funding history endpoint for precise payments
            payments = await self.get_funding_history(symbol, start_time)

            if payments:
                # Defensive filter: some API failures/changes can cause `fromTime` to be ignored.
                # Ensure we only attribute funding to this trade from `start_time` onwards.
                if start_time:
                    start_ts = start_time.timestamp()

                    def _paid_ts_seconds(item: dict) -> float | None:
                        raw = (
                            item.get("paidTime")
                            or item.get("paid_time")
                            or item.get("timestamp")
                            or item.get("time")
                            or item.get("paidAt")
                        )
                        if raw is None:
                            return None
                        try:
                            val = int(raw)
                        except Exception:
                            try:
                                val = int(float(raw))
                            except Exception:
                                return None
                        # Heuristic: ms timestamps are typically > 1e11.
                        if val > 10**11:
                            return float(val) / 1000.0
                        return float(val)

                    filtered: list[dict] = []
                    for p in payments:
                        ts = _paid_ts_seconds(p)
                        # If timestamp is missing/unparseable, keep it (best-effort).
                        if ts is None or ts >= start_ts:
                            filtered.append(p)
                    payments = filtered

                total_funding = Decimal("0")
                for p in payments:
                    # fundingFee can be positive (received) or negative (paid)
                    fee = _safe_decimal(p.get("fundingFee", 0))
                    total_funding += fee

                logger.debug(f"X10 precise funding for {symbol}: ${total_funding:.4f} from {len(payments)} payments")
                return total_funding

            # FALLBACK: Use realised_pnl from positions (legacy behavior)
            # This is less accurate as it includes execution PnL
            logger.debug(f"X10 funding history empty for {symbol}, falling back to realised_pnl")

            response = await self._sdk_call_with_retry(
                lambda: self._trading_client.account.get_positions(),
                operation_name="get_positions_fallback",
            )
            positions_data = getattr(response, "data", response) or []

            for p in positions_data:
                p_symbol = _get_attr(p, "market") or _get_attr(p, "symbol", "")

                if p_symbol == symbol or p_symbol.replace("-USD", "") == symbol:
                    return _safe_decimal(_get_attr(p, "realised_pnl"))

        except Exception as e:
            logger.warning(f"Failed to get X10 realized funding: {e}")
            raise ExchangeError(f"Failed to get realized funding: {e}", exchange="X10") from e

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Get current mark price for a symbol (from cache)."""
        market_name = self._resolve_market_name(symbol)

        # Return cached price - batch refresh updates the cache
        if market_name in self._price_cache:
            return self._price_cache[market_name]

        # If not cached, try a batch refresh
        if self._trading_client:
            await self.refresh_all_market_data()
            if market_name in self._price_cache:
                return self._price_cache[market_name]

        raise ExchangeError(f"Price not available for {symbol}", exchange="X10", symbol=symbol)

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate for a symbol (from cache)."""
        market_name = self._resolve_market_name(symbol)

        # Return cached rate - batch refresh updates the cache
        if market_name in self._funding_cache:
            return self._funding_cache[market_name]

        # If not cached, try a batch refresh
        if self._trading_client:
            await self.refresh_all_market_data()
            if market_name in self._funding_cache:
                return self._funding_cache[market_name]

        # Return zero rate if still not found
        return FundingRate(symbol=symbol, exchange=Exchange.X10, rate=Decimal("0"))

    # =========================================================================
    # Orders
    # =========================================================================

    async def _validate_order_price(self, request: OrderRequest) -> None:
        """
        Validate order price against current mark price to prevent fat-finger errors.
        Enforces Rule 4 (Safety).
        """
        # Market orders emulate using internal logic, but if a Limit price is provided, we MUST check it.
        # If order is Market, the adapter calculates the price itself safely below.
        if request.order_type == OrderType.MARKET:
            return

        # If explicit price is missing or 0 for a LIMIT order, that's invalid anyway
        if not request.price or request.price <= 0:
            return

        market_name = self._resolve_market_name(request.symbol)
        try:
            # Use internal cache if available
            mark_price = Decimal("0")
            cached = self._price_cache.get(market_name)

            if cached and cached > 0:
                mark_price = cached
            else:
                # Fallback to fetch
                mark_price = await self.get_mark_price(market_name)
        except Exception:
            mark_price = Decimal("0")

        if mark_price <= 0:
            # We can't validate, so we must block for safety (unless user explicitly bypasses, which creates risk)
            # For now: strict safety.
            raise ExchangeError(f"Mark price unavailable for validation of {request.symbol}", exchange="X10")

        # 10% Safety Band
        deviation = Decimal("0.10")
        lower_bound = mark_price * (Decimal("1") - deviation)
        upper_bound = mark_price * (Decimal("1") + deviation)

        if request.price < lower_bound or request.price > upper_bound:
            msg = (
                f"Price deviation blocked: {request.symbol} Order={request.price:.4f} "
                f"Mark={mark_price:.4f} (+/- {deviation * 100}%)"
            )
            logger.error(f"SAFETY INTERVENTION: {msg}")
            raise ExchangeError(msg, exchange="X10")

    async def place_order(self, request: OrderRequest) -> Order:
        """Place an order."""
        if not self._trading_client:
            raise ExchangeError("Trading client not initialized", exchange="X10")

        # [Rule 4] Price-Banding Check for LIMIT orders
        await self._validate_order_price(request)

        try:
            from x10.perpetual.orders import OrderSide
            from x10.perpetual.orders import TimeInForce as X10TimeInForce

            # Map side
            sdk_side = OrderSide.BUY if request.side == Side.BUY else OrderSide.SELL

            # Resolve market name from symbol map (e.g. "NEAR" -> "NEAR-USD")
            market_name = self._symbol_map.get(request.symbol, request.symbol)
            # Fallback: if not found and missing -USD, append it
            if (
                market_name not in self._markets
                and not market_name.endswith("USD")
                and f"{market_name}-USD" in self._markets
            ):
                market_name = f"{market_name}-USD"

            # Prepare price and TIF
            price = request.price
            tif = request.time_in_force
            post_only = tif == TimeInForce.POST_ONLY

            # Emulate MARKET order using aggressive LIMIT IOC around mark price.
            if request.order_type == OrderType.MARKET or not price:
                mark_price = Decimal("0")
                try:
                    mark_price = await self.get_mark_price(request.symbol)
                except Exception:
                    cached = self._price_cache.get(market_name) or self._price_cache.get(request.symbol)
                    if cached and cached > 0:
                        mark_price = cached
                    else:
                        with contextlib.suppress(Exception):
                            l1 = await self.get_orderbook_l1(market_name)
                            best_bid = _safe_decimal(l1.get("best_bid"))
                            best_ask = _safe_decimal(l1.get("best_ask"))
                            if request.side == Side.BUY:
                                mark_price = best_ask if best_ask > 0 else best_bid
                            else:
                                mark_price = best_bid if best_bid > 0 else best_ask
                        if mark_price <= 0:
                            cached_book = (
                                self._orderbook_cache.get(market_name)
                                or self._orderbook_cache.get(request.symbol)
                                or {}
                            )
                            best_bid = _safe_decimal(cached_book.get("best_bid"))
                            best_ask = _safe_decimal(cached_book.get("best_ask"))
                            if request.side == Side.BUY:
                                mark_price = best_ask if best_ask > 0 else best_bid
                            else:
                                mark_price = best_bid if best_bid > 0 else best_ask

                if mark_price <= 0:
                    raise ExchangeError(
                        f"Price not available for {request.symbol}",
                        exchange="X10",
                        symbol=request.symbol,
                    )
                slippage = _safe_decimal(
                    getattr(self.settings.execution, "taker_order_slippage", None),
                    default=Decimal("0.0075"),
                )
                # Safety clamp: avoid accidental extremes on misconfig.
                slippage = max(Decimal("0.0005"), min(slippage, Decimal("0.05")))

                if request.side == Side.BUY:
                    price = mark_price * (Decimal("1") + slippage)
                else:
                    price = mark_price * (Decimal("1") - slippage)

                # Round to appropriate precision (assuming 0.0001 for now or use tick size if available)
                # Getting tick size from market info would be better, but for safety lets format generic
                price = price.quantize(Decimal("0.000001"))

                # Force IOC for market emulation
                sdk_tif = X10TimeInForce.IOC
                post_only = False
            else:
                # Map standard TimeInForce
                if tif == TimeInForce.IOC:
                    sdk_tif = X10TimeInForce.IOC
                elif tif == TimeInForce.FOK:
                    sdk_tif = X10TimeInForce.FOK
                else:
                    sdk_tif = X10TimeInForce.GTT  # Default to GTT (Good Till Time) similar to default SDK

                # Post-only is a separate flag on Extended/X10. We represent it internally via TimeInForce.POST_ONLY.
                if post_only:
                    sdk_tif = X10TimeInForce.GTT

            # Place order via SDK with CORRECT arguments
            # Note: SDK uses 'market_name', 'amount_of_synthetic', 'external_id'
            # and infers LIMIT type from price.

            # Normalize precision
            qty = request.qty
            market_info = self._markets.get(market_name)
            if market_info:
                # X10 enforces step sizes that are not necessarily powers of 10 (e.g. 100).
                # Round qty DOWN by default to avoid oversizing and reduce-only violations.
                qty = _round_to_increment(qty, _safe_decimal(market_info.step_size), rounding="floor")

                min_qty = _safe_decimal(getattr(market_info, "min_qty", None))
                if qty <= 0 or (min_qty > 0 and qty < min_qty):
                    raise OrderRejectedError(
                        f"Invalid X10 quantity after step rounding: qty={qty} (min_qty={min_qty})",
                        exchange="X10",
                        symbol=request.symbol,
                    )

                if price:
                    # Round price to tick using a marketability-preserving direction:
                    # BUY -> ceil (more aggressive), SELL -> floor.
                    price = _round_to_increment(
                        price,
                        _safe_decimal(market_info.tick_size),
                        rounding=("ceil" if request.side == Side.BUY else "floor"),
                    )

            # Log AFTER normalization so logs match what we actually submit.
            logger.info(f"Placing X10 order: {request.side.value} {qty} {request.symbol} @ {price or 'MARKET'}")

            client_order_id = (
                str(request.client_order_id).strip() if request.client_order_id else self._generate_client_order_id()
            )

            result = await self._sdk_call_with_retry(
                lambda: self._trading_client.place_order(
                    market_name=market_name,
                    amount_of_synthetic=qty,  # Pass Decimal directly
                    price=price,
                    side=sdk_side,
                    post_only=post_only,
                    time_in_force=sdk_tif,
                    reduce_only=request.reduce_only,
                    external_id=client_order_id,
                ),
                operation_name="place_order",
                priority=True,  # Critical path: bypass background queue
            )

            # Retrieve the data payload from the WrappedApiResponse
            result_data = getattr(result, "data", result)

            # Debug Log the raw response to catch any field issues
            try:
                raw_dict = (
                    result_data.model_dump()
                    if hasattr(result_data, "model_dump")
                    else (result_data.dict() if hasattr(result_data, "dict") else str(result_data))
                )
                logger.debug(f"X10 place_order raw response: {raw_dict}")
            except Exception:
                pass

            order_id = str(_get_attr(result_data, "order_id") or _get_attr(result_data, "id", ""))
            status_str = _get_attr(result_data, "status", "pending")

            # Map status
            status_map = {
                "new": OrderStatus.OPEN,
                "open": OrderStatus.OPEN,
                "pending": OrderStatus.PENDING,
                "partially_filled": OrderStatus.PARTIALLY_FILLED,
                "filled": OrderStatus.FILLED,
                "cancelled": OrderStatus.CANCELLED,
                "canceled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            status = status_map.get(status_str.lower(), OrderStatus.PENDING)

            order = Order(
                order_id=order_id,
                symbol=request.symbol,
                exchange=Exchange.X10,
                side=request.side,
                order_type=request.order_type,
                qty=qty,
                price=price,  # Use the actual price sent
                time_in_force=request.time_in_force,
                reduce_only=request.reduce_only,
                status=status,
                filled_qty=Decimal("0"),  # PlacedOrderModel does NOT return filled_qty
                avg_fill_price=_safe_decimal(_get_attr(result_data, "avg_fill_price")),
                fee=_safe_decimal(_get_attr(result_data, "fee")),
                client_order_id=client_order_id,
            )

            # Cache metadata so we can reconstruct fills via trade history if the order endpoints lag.
            self._remember_submitted_order(order)

            logger.info(f"X10 order placed: {order_id} status={status}")
            return order

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"Failed to place X10 order: {e}")

            if "insufficient" in error_msg.lower():
                raise InsufficientBalanceError(
                    f"Insufficient balance: {e}",
                    required=request.qty * (request.price or Decimal("0")),
                    available=Decimal("0"),
                    exchange="X10",
                    symbol=request.symbol,
                ) from e

            raise OrderRejectedError(
                f"Order rejected: {e}",
                exchange="X10",
                symbol=request.symbol,
            ) from e

    def _generate_client_order_id(self) -> str:
        """Generate a best-effort external order id for X10 replace/cancel flows."""
        return f"bot_{int(time.time() * 1000)}_{random.randint(0, 1_000_000)}"

    def _remember_submitted_order(self, order: Order) -> None:
        """Keep a small LRU-ish cache of recent submitted orders (best-effort)."""
        order_id = str(getattr(order, "order_id", "") or "").strip()
        if not order_id:
            return

        # Store/refresh
        self._submitted_order_by_id.pop(order_id, None)
        self._submitted_order_by_id[order_id] = order

        client_id = str(getattr(order, "client_order_id", "") or "").strip()
        if client_id:
            self._client_order_id_by_order_id.pop(order_id, None)
            self._client_order_id_by_order_id[order_id] = client_id

        # Trim oldest entries (dict preserves insertion order in 3.7+).
        limit = max(50, int(getattr(self, "_order_cache_max_entries", 500) or 500))
        while len(self._submitted_order_by_id) > limit:
            oldest = next(iter(self._submitted_order_by_id))
            self._submitted_order_by_id.pop(oldest, None)
        while len(self._client_order_id_by_order_id) > limit:
            oldest = next(iter(self._client_order_id_by_order_id))
            self._client_order_id_by_order_id.pop(oldest, None)

    def _resolve_market_name(self, symbol: str) -> str:
        """Normalize a symbol to X10 market name (e.g., ETH -> ETH-USD)."""
        market_name = self._symbol_map.get(symbol, symbol)
        if (
            market_name not in self._markets
            and not market_name.endswith("USD")
            and f"{market_name}-USD" in self._markets
        ):
            market_name = f"{market_name}-USD"
        return market_name

    def _supports_orderbook_snapshot_limit(self) -> bool:
        """
        Detect whether the installed X10 SDK supports the `limit` parameter
        on `get_orderbook_snapshot`. Older SDKs do not accept it.
        """
        if self._orderbook_snapshot_supports_limit is not None:
            return self._orderbook_snapshot_supports_limit

        try:
            import inspect

            sig = inspect.signature(self._trading_client.markets_info.get_orderbook_snapshot)
            self._orderbook_snapshot_supports_limit = "limit" in sig.parameters
        except Exception:
            self._orderbook_snapshot_supports_limit = False

        return self._orderbook_snapshot_supports_limit

    async def _get_orderbook_snapshot(
        self,
        *,
        market_name: str,
        limit: int | None,
        operation_name: str,
    ) -> Any:
        """
        Fetch an orderbook snapshot with SDK compatibility fallback.

        If the SDK doesn't support `limit`, fall back to the default snapshot and
        rely on client-side slicing.
        """
        if not self._trading_client:
            raise ExchangeError("Trading client not initialized", exchange="X10", symbol=market_name)

        supports_limit = self._supports_orderbook_snapshot_limit()
        if limit is not None and supports_limit:
            try:
                return await self._sdk_call_with_retry(
                    lambda: self._trading_client.markets_info.get_orderbook_snapshot(
                        market_name=market_name,
                        limit=limit,
                    ),
                    operation_name=operation_name,
                )
            except TypeError as e:
                # SDK doesn't accept `limit` (unexpected kwarg) - fall back.
                if "limit" in str(e).lower():
                    self._orderbook_snapshot_supports_limit = False
                    supports_limit = False
                else:
                    raise

        if not supports_limit and not self._orderbook_snapshot_limit_logged:
            logger.warning(
                "X10 SDK does not support orderbook snapshot limit; "
                "falling back to full snapshot and client-side slicing."
            )
            self._orderbook_snapshot_limit_logged = True

        return await self._sdk_call_with_retry(
            lambda: self._trading_client.markets_info.get_orderbook_snapshot(market_name=market_name),
            operation_name=operation_name,
        )

    def _looks_like_not_found(self, exc: Exception) -> bool:
        msg = (str(exc) or "").lower()
        return ("404" in msg) or ("not found" in msg) or ("code 404" in msg)

    def _estimate_fee_usd(self, notional_usd: Decimal, *, is_taker: bool) -> Decimal:
        if notional_usd <= 0:
            return Decimal("0")
        if is_taker:
            rate = self._taker_fee if self._taker_fee is not None else self.settings.trading.taker_fee_x10
        else:
            rate = self._maker_fee if self._maker_fee is not None else self.settings.trading.maker_fee_x10
        try:
            return (notional_usd * _safe_decimal(rate)).copy_abs()
        except Exception:
            return Decimal("0")

    def _parse_order_model(
        self,
        model: Any,
        *,
        symbol: str,
        order_id_hint: str | None = None,
        client_order_id_hint: str | None = None,
    ) -> Order:
        order_id = str(order_id_hint or "") or str(_get_attr(model, "order_id") or _get_attr(model, "id") or "")
        order_id = str(order_id).strip()

        status_str = str(_get_attr(model, "status", "pending") or "pending")
        status_map = {
            "new": OrderStatus.OPEN,
            "open": OrderStatus.OPEN,
            "pending": OrderStatus.PENDING,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "cancelled": OrderStatus.CANCELLED,
            "canceled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
        }
        status = status_map.get(status_str.lower(), OrderStatus.PENDING)

        side_str = str(_get_attr(model, "side", "buy") or "buy")
        side = Side.BUY if side_str.lower() in ("buy", "long") else Side.SELL

        type_str = _get_attr(model, "type") or _get_attr(model, "order_type") or "limit"
        order_type = OrderType.MARKET if str(type_str).lower() == "market" else OrderType.LIMIT

        qty = _safe_decimal(_get_attr(model, "size") or _get_attr(model, "qty"))
        filled_qty = _safe_decimal(
            _get_attr(model, "filled_qty")
            or _get_attr(model, "filled_size")
            or _get_attr(model, "qty_filled")
            or _get_attr(model, "executed_amount")
        )
        avg_fill_price = _safe_decimal(
            _get_attr(model, "avg_fill_price") or _get_attr(model, "avg_price") or _get_attr(model, "average_price")
        )
        fee = _safe_decimal(_get_attr(model, "fee") or _get_attr(model, "payed_fee"))

        if fee <= 0 and filled_qty > 0 and avg_fill_price > 0:
            # Unknown maker/taker from this payload; be conservative and assume taker.
            fee = self._estimate_fee_usd(filled_qty * avg_fill_price, is_taker=True)

        external_id = _get_attr(model, "external_id") or _get_attr(model, "externalId")
        client_id = str(client_order_id_hint or "").strip() or str(external_id or "").strip() or None

        reduce_only = _safe_bool(_get_attr(model, "reduce_only") or _get_attr(model, "reduceOnly"), False)
        post_only = _safe_bool(_get_attr(model, "post_only") or _get_attr(model, "postOnly"), False)
        tif_str = str(_get_attr(model, "time_in_force") or _get_attr(model, "timeInForce") or "").strip().upper()

        # Extended uses a separate `postOnly` flag; represent it internally via TimeInForce.POST_ONLY.
        if post_only:
            tif = TimeInForce.POST_ONLY
        elif tif_str == "IOC":
            tif = TimeInForce.IOC
        elif tif_str == "FOK":
            tif = TimeInForce.FOK
        else:
            # Extended API default is GTT; represent it internally as GTC.
            tif = TimeInForce.GTC

        return Order(
            order_id=order_id or str(order_id_hint or ""),
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            order_type=order_type,
            qty=qty,
            price=_safe_decimal(_get_attr(model, "price")),
            time_in_force=tif,
            reduce_only=reduce_only,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            fee=fee,
            client_order_id=client_id,
        )

    async def _cache_filled_order(self, order: Order) -> None:
        """
        Cache filled/cancelled order for instant get_order() lookups.

        Avoids slow REST API searches. Cache entries include timestamp for TTL-based cleanup.
        """
        if not order.order_id:
            return

        async with self._ws_fill_cache_lock:
            self._ws_fill_cache[order.order_id] = (order, time.time())

    async def _cleanup_stale_fill_cache(self) -> int:
        """
        Remove stale entries from fill cache based on TTL.

        Called periodically from market data refresh loop.

        Returns:
            Number of entries removed.
        """
        now = time.time()
        async with self._ws_fill_cache_lock:
            stale_keys = [k for k, (_, ts) in self._ws_fill_cache.items() if now - ts > self._ws_fill_cache_ttl_seconds]
            for k in stale_keys:
                del self._ws_fill_cache[k]
            if stale_keys:
                logger.debug(f"Cleaned up {len(stale_keys)} stale X10 fill cache entries")
            return len(stale_keys)

    async def _get_cached_fill(self, order_id: str) -> Order | None:
        """
        Check WebSocket fill cache for instant order lookup.

        Returns cached order if found and not expired, None otherwise.
        """
        if not order_id:
            return None

        now = time.time()
        async with self._ws_fill_cache_lock:
            entry = self._ws_fill_cache.get(order_id)
            if entry:
                cached, ts = entry
                # Check TTL
                if now - ts > self._ws_fill_cache_ttl_seconds:
                    return None  # Expired
                logger.debug(
                    f"⚡ WS Fill Cache HIT for {cached.symbol} order {order_id} "
                    f"(status={cached.status}, filled={cached.filled_qty}/{cached.qty})"
                )
                return cached

        return None

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get order status by ID."""

        # 🚀 PERFORMANCE: Check WebSocket fill cache FIRST (instant lookup)
        # Avoids slow REST API search for orders already filled via WS.
        cached = await self._get_cached_fill(order_id)
        if cached:
            return cached  # ⚡ Instant return (~5ms vs REST search)

        if not self._trading_client or not order_id:
            return None

        order_id_str = str(order_id).strip()
        if not order_id_str:
            return None

        market_name = self._resolve_market_name(symbol)
        client_id = self._client_order_id_by_order_id.get(order_id_str)

        numeric_id: int | None = None
        with contextlib.suppress(Exception):
            numeric_id = int(order_id_str)

        # 1) Fast path: by numeric id (with a short retry for indexing latency)
        if numeric_id is not None:
            for attempt in range(3):
                try:
                    result_obj = await self._sdk_call_with_retry(
                        lambda: self._trading_client.account.get_order_by_id(numeric_id),
                        operation_name="get_order_by_id",
                    )
                    result = result_obj.data if result_obj else None
                    if result:
                        return self._parse_order_model(
                            result,
                            symbol=symbol,
                            order_id_hint=order_id_str,
                            client_order_id_hint=client_id,
                        )
                    break
                except Exception as e:
                    if not self._looks_like_not_found(e):
                        logger.warning("X10 get_order_by_id failed for %s: %s", order_id_str, e)
                        return None
                    if attempt < 2:
                        await asyncio.sleep(0.25)

        # 2) Try by external id (if we know it, or if caller passed a non-numeric id)
        external_id = client_id
        if external_id is None and numeric_id is None:
            external_id = order_id_str
        if external_id:
            with contextlib.suppress(Exception):
                resp = await self._sdk_call_with_retry(
                    lambda: self._trading_client.account.get_order_by_external_id(str(external_id)),
                    operation_name="get_order_by_external_id",
                )
                items = getattr(resp, "data", None) or []
                if isinstance(items, list) and items:
                    return self._parse_order_model(
                        items[0],
                        symbol=symbol,
                        order_id_hint=order_id_str,
                        client_order_id_hint=str(external_id),
                    )

        # 3) Check open orders (covers partial-fill / still-working orders)
        open_orders_checked = False
        try:
            open_resp = await self._sdk_call_with_retry(
                lambda: self._trading_client.account.get_open_orders(market_names=[market_name]),
                operation_name="get_open_orders",
            )
            open_orders_checked = True
            open_items = getattr(open_resp, "data", None) or []
            if isinstance(open_items, list):
                for it in open_items:
                    it_id = str(_get_attr(it, "order_id") or _get_attr(it, "id") or "").strip()
                    it_ext = str(_get_attr(it, "external_id") or _get_attr(it, "externalId") or "").strip()
                    if (it_id and it_id == order_id_str) or (external_id and it_ext and it_ext == str(external_id)):
                        return self._parse_order_model(
                            it,
                            symbol=symbol,
                            order_id_hint=it_id or order_id_str,
                            client_order_id_hint=str(external_id) if external_id else None,
                        )
        except Exception:
            pass

        # 4) Try orders history (recent window)
        history_checked = False
        try:
            hist = await self._sdk_call_with_retry(
                lambda: self._trading_client.account.get_orders_history(market_names=[market_name], limit=50),
                operation_name="get_orders_history",
            )
            history_checked = True
            items = getattr(hist, "data", None) or []
            if isinstance(items, list):
                for it in items:
                    it_id = str(_get_attr(it, "order_id") or _get_attr(it, "id") or "").strip()
                    it_ext = str(_get_attr(it, "external_id") or _get_attr(it, "externalId") or "").strip()
                    if (it_id and it_id == order_id_str) or (external_id and it_ext and it_ext == str(external_id)):
                        return self._parse_order_model(
                            it,
                            symbol=symbol,
                            order_id_hint=order_id_str,
                            client_order_id_hint=str(external_id) if external_id else None,
                        )
        except Exception:
            pass

        # 5) Last resort: reconstruct from trades (accurate fees + VWAP)
        if numeric_id is not None:
            with contextlib.suppress(Exception):
                trades_resp = await self._sdk_call_with_retry(
                    lambda: self._trading_client.account.get_trades(market_names=[market_name], limit=200),
                    operation_name="get_trades",
                )
                trades = getattr(trades_resp, "data", None) or []
                if isinstance(trades, list) and trades:
                    matched = [t for t in trades if int(_get_attr(t, "order_id", -1) or -1) == numeric_id]
                    if matched:
                        filled_qty = sum((_safe_decimal(_get_attr(t, "qty")) for t in matched), Decimal("0"))
                        notional = sum(
                            (
                                _safe_decimal(_get_attr(t, "price")) * _safe_decimal(_get_attr(t, "qty"))
                                for t in matched
                            ),
                            Decimal("0"),
                        )
                        avg_fill_price = (notional / filled_qty) if filled_qty > 0 else Decimal("0")
                        fee = sum((_safe_decimal(_get_attr(t, "fee")) for t in matched), Decimal("0"))

                        side_str = str(_get_attr(matched[0], "side", "buy") or "buy")
                        side = Side.BUY if side_str.lower() in ("buy", "long") else Side.SELL

                        cached = self._submitted_order_by_id.get(order_id_str)
                        target_qty = cached.qty if (cached and cached.qty > 0) else filled_qty
                        is_filled = target_qty > 0 and filled_qty >= target_qty
                        if is_filled:
                            status = OrderStatus.FILLED
                        else:
                            # If we were able to query both open orders + history and neither contained this id,
                            # treat the remaining size as terminal (likely cancelled/expired). Otherwise, keep it
                            # non-terminal to allow the caller to poll again.
                            status = (
                                OrderStatus.CANCELLED
                                if (open_orders_checked and history_checked)
                                else OrderStatus.PARTIALLY_FILLED
                            )
                        order_type = cached.order_type if cached else OrderType.LIMIT
                        price = cached.price if (cached and cached.price) else avg_fill_price

                        if fee <= 0 and avg_fill_price > 0 and filled_qty > 0:
                            fee = self._estimate_fee_usd(filled_qty * avg_fill_price, is_taker=True)

                        return Order(
                            order_id=order_id_str,
                            symbol=symbol,
                            exchange=Exchange.X10,
                            side=side,
                            order_type=order_type,
                            qty=target_qty,
                            price=price,
                            status=status,
                            filled_qty=filled_qty,
                            avg_fill_price=avg_fill_price,
                            fee=fee,
                            client_order_id=external_id,
                        )

        # 6) Final heuristic: if we can see a matching position, assume fill (best-effort).
        pos = None
        with contextlib.suppress(Exception):
            pos = await self.get_position(symbol)
        if pos and pos.qty > 0:
            fallback_price = pos.entry_price
            if fallback_price <= 0 and pos.mark_price and pos.mark_price > 0:
                fallback_price = pos.mark_price
            if fallback_price <= 0 and symbol in self._price_cache and self._price_cache[symbol] > 0:
                fallback_price = self._price_cache[symbol]

            est_fee = Decimal("0")
            if pos.qty > 0 and fallback_price > 0:
                est_fee = self._estimate_fee_usd(pos.qty * fallback_price, is_taker=True)

            cached = self._submitted_order_by_id.get(order_id_str)
            target_qty = cached.qty if (cached and cached.qty > 0) else pos.qty
            filled_qty = min(pos.qty, target_qty) if target_qty > 0 else pos.qty
            status = (
                OrderStatus.FILLED if (target_qty > 0 and filled_qty >= target_qty) else OrderStatus.PARTIALLY_FILLED
            )

            return Order(
                order_id=order_id_str,
                symbol=symbol,
                exchange=Exchange.X10,
                side=pos.side,
                order_type=cached.order_type if cached else OrderType.LIMIT,
                qty=target_qty,
                price=None,
                status=status,
                filled_qty=filled_qty,
                avg_fill_price=fallback_price,
                fee=est_fee,
                client_order_id=external_id,
            )

        return None

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        price: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> bool:
        """
        Modify an existing order via X10's replace flow (cancelId / previous_order_id).
        """
        if not self._trading_client:
            return False

        if price is None and qty is None:
            return True

        order_id_str = str(order_id).strip()
        if not order_id_str:
            return False

        cached = self._submitted_order_by_id.get(order_id_str)
        if cached is None:
            cached = await self.get_order(symbol, order_id_str)

        if cached is None:
            logger.warning(f"Cannot modify X10 order {order_id}: order not found")
            return False

        prev_external_id = cached.client_order_id or self._client_order_id_by_order_id.get(order_id_str)
        if not prev_external_id:
            if not order_id_str.isdigit():
                prev_external_id = order_id_str
            else:
                logger.warning(f"Cannot modify X10 order {order_id}: missing external id")
                return False

        target_qty = qty if qty is not None else cached.qty
        target_price = price if price is not None else cached.price

        if target_qty is None or target_qty <= 0:
            logger.warning(f"Cannot modify X10 order {order_id}: invalid qty={target_qty}")
            return False
        if target_price is None or target_price <= 0:
            logger.warning(f"Cannot modify X10 order {order_id}: invalid price={target_price}")
            return False

        from x10.perpetual.orders import OrderSide
        from x10.perpetual.orders import TimeInForce as X10TimeInForce

        sdk_side = OrderSide.BUY if cached.side == Side.BUY else OrderSide.SELL

        if cached.order_type == OrderType.MARKET:
            sdk_tif = X10TimeInForce.IOC
            post_only = False
        else:
            post_only = cached.time_in_force == TimeInForce.POST_ONLY
            if cached.time_in_force == TimeInForce.IOC:
                sdk_tif = X10TimeInForce.IOC
            elif cached.time_in_force == TimeInForce.FOK:
                sdk_tif = X10TimeInForce.FOK
            else:
                sdk_tif = X10TimeInForce.GTT
            if post_only:
                sdk_tif = X10TimeInForce.GTT

        market_name = self._resolve_market_name(symbol)
        market_info = self._markets.get(market_name)
        if market_info:
            target_qty = _round_to_increment(
                target_qty,
                _safe_decimal(market_info.step_size),
                rounding="floor",
            )

            min_qty = _safe_decimal(getattr(market_info, "min_qty", None))
            if target_qty <= 0 or (min_qty > 0 and target_qty < min_qty):
                logger.warning(f"Cannot modify X10 order {order_id}: qty={target_qty} (min_qty={min_qty})")
                return False

            target_price = _round_to_increment(
                target_price,
                _safe_decimal(market_info.tick_size),
                rounding=("ceil" if cached.side == Side.BUY else "floor"),
            )

        new_external_id = self._generate_client_order_id()

        try:
            logger.info(
                f"X10 replace requested: order_id={order_id_str} prev_external_id={prev_external_id} "
                f"new_external_id={new_external_id} price={target_price} qty={target_qty}"
            )
            result = await self._sdk_call_with_retry(
                lambda: self._trading_client.place_order(
                    market_name=market_name,
                    amount_of_synthetic=target_qty,
                    price=target_price,
                    side=sdk_side,
                    post_only=post_only,
                    time_in_force=sdk_tif,
                    reduce_only=cached.reduce_only,
                    external_id=new_external_id,
                    previous_order_id=str(prev_external_id),
                ),
                operation_name="replace_order",
            )

            result_data = getattr(result, "data", result)
            new_order_id = str(_get_attr(result_data, "order_id") or _get_attr(result_data, "id", "")).strip()
            if new_order_id:
                self._remember_submitted_order(
                    Order(
                        order_id=new_order_id,
                        symbol=cached.symbol,
                        exchange=Exchange.X10,
                        side=cached.side,
                        order_type=cached.order_type,
                        qty=target_qty,
                        price=target_price,
                        time_in_force=cached.time_in_force,
                        reduce_only=cached.reduce_only,
                        status=OrderStatus.PENDING,
                        filled_qty=Decimal("0"),
                        avg_fill_price=Decimal("0"),
                        fee=Decimal("0"),
                        client_order_id=new_external_id,
                    )
                )

            logger.info(
                f"Modified X10 order via replace: prev_external_id={prev_external_id} "
                f"new_external_id={new_external_id} price={target_price} qty={target_qty}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to modify X10 order {order_id}: {e}")
            return False

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""
        if not self._trading_client:
            return False

        try:
            # SDK supports cancel by numeric order_id or by external_id.
            with contextlib.suppress(Exception):
                numeric_id = int(str(order_id))
                await self._sdk_call_with_retry(
                    lambda: self._trading_client.orders.cancel_order(numeric_id),
                    operation_name="cancel_order",
                    priority=True,
                )
                logger.info(f"Cancelled X10 order {order_id}")
                return True

            external_id = self._client_order_id_by_order_id.get(str(order_id).strip()) or str(order_id)
            await self._sdk_call_with_retry(
                lambda: self._trading_client.orders.cancel_order_by_external_id(str(external_id)),
                operation_name="cancel_order_by_external_id",
                priority=True,
            )
            logger.info(f"Cancelled X10 order by external_id {external_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel all open orders."""
        if not self._trading_client:
            return 0

        try:
            # Count open orders first (API returns EmptyModel for massCancel).
            open_count = 0
            with contextlib.suppress(Exception):
                resp = await self._sdk_call_with_retry(
                    lambda: self._trading_client.account.get_open_orders(market_names=[symbol] if symbol else None),
                    operation_name="get_open_orders_for_cancel_all",
                    priority=True,
                )
                open_orders = getattr(resp, "data", None) or []
                open_count = len(open_orders) if isinstance(open_orders, list) else 0

            if symbol:
                await self._sdk_call_with_retry(
                    lambda: self._trading_client.orders.mass_cancel(markets=[symbol], cancel_all=None),
                    operation_name="mass_cancel",
                )
            else:
                await self._sdk_call_with_retry(
                    lambda: self._trading_client.orders.mass_cancel(cancel_all=True),
                    operation_name="mass_cancel_all",
                )

            logger.info(f"Cancelled ~{open_count} X10 orders via massCancel")
            return open_count

        except Exception as e:
            logger.warning(f"Failed to cancel all orders: {e}")
            return 0

    # =========================================================================
    # WebSocket
    # =========================================================================

    async def _start_websocket(self) -> None:
        """Start WebSocket client task."""
        if self._ws_task:
            return

        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("X10 WebSocket task started")

    async def _ws_loop(self) -> None:
        """WebSocket connection loop."""
        while True:
            try:
                self._ws_ready.clear()
                self._ws_ready_logged = False
                if not self._stream_client_cls or not self._api_key:
                    logger.warning("X10 Stream Client not available or missing API key")
                    return

                api_url = self._get_stream_api_url()

                # Initialize Stream Client
                client = self._stream_client_cls(api_url=api_url)
                self._stream_client = client

                logger.info("Connecting to X10 WS...")

                # Subscribe to account updates
                async with client.subscribe_to_account_updates(self._api_key) as stream:
                    if not self._ws_ready.is_set():
                        self._ws_ready.set()
                        if not self._ws_ready_logged:
                            logger.info("X10 WS ready (account updates stream connected)")
                            self._ws_ready_logged = True
                    async for msg in stream:
                        await self._handle_ws_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"X10 WS error: {e}")
                await asyncio.sleep(5)  # Reconnect delay
            finally:
                self._ws_ready.clear()

    def _get_stream_api_url(self) -> str:
        """
        X10 SDK stream client requires the dedicated stream base URL (ws/wss).

        SDK source of truth:
        - `libs/python_sdk-starknet/x10/perpetual/configuration.py` -> `MAINNET_CONFIG.stream_url`
          e.g. `wss://api.starknet.extended.exchange/stream.extended.exchange/v1`
        """
        cfg = getattr(self, "_endpoint_config", None)
        stream_url = getattr(cfg, "stream_url", None) if cfg else None
        if isinstance(stream_url, str) and stream_url.strip():
            return stream_url.strip().rstrip("/")

        # Fallback: derive from configured base_url (best-effort).
        base = (
            _normalize_x10_onboarding_url(self.settings.x10.base_url or "https://api.starknet.extended.exchange")
            .strip()
            .rstrip("/")
        )
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        elif not (base.startswith("wss://") or base.startswith("ws://")):
            base = "wss://" + base

        if "/stream.extended.exchange/" not in base:
            base = base.rstrip("/") + "/stream.extended.exchange/v1"
        return base.rstrip("/")

    async def _handle_ws_message(self, msg: Any) -> None:
        """Handle incoming WS message."""
        try:
            # X10 streams wrap payloads in WrappedStreamResponse[T].
            payload = getattr(msg, "data", None) or msg

            # 1. Orders
            orders = _get_attr(payload, "orders", None) or []
            if orders:
                for o_model in orders:
                    # Map to Order
                    status_map = {
                        "NEW": OrderStatus.OPEN,
                        "FILLED": OrderStatus.FILLED,
                        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                        "CANCELLED": OrderStatus.CANCELLED,
                        "REJECTED": OrderStatus.REJECTED,
                    }
                    status = status_map.get(str(o_model.status).upper(), OrderStatus.PENDING)

                    side = Side.BUY if str(o_model.side).upper() == "BUY" else Side.SELL

                    # Try to map type
                    order_type = OrderType.LIMIT
                    if str(o_model.type).upper() == "MARKET":
                        order_type = OrderType.MARKET

                    order = Order(
                        order_id=str(o_model.id),
                        symbol=o_model.market,
                        exchange=Exchange.X10,
                        side=side,
                        order_type=order_type,
                        qty=_safe_decimal(o_model.qty),
                        price=_safe_decimal(o_model.price),
                        status=status,
                        filled_qty=_safe_decimal(o_model.filled_qty),
                        avg_fill_price=_safe_decimal(o_model.average_price),
                        fee=_safe_decimal(o_model.payed_fee),
                        client_order_id=o_model.external_id,
                    )

                    # 🚀 PERFORMANCE: Populate fill cache for instant get_order() lookups
                # Store terminal states (FILLED/CANCELLED) for fast detection.
                if order.status in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.PARTIALLY_FILLED,
                ):
                    asyncio.create_task(self._cache_filled_order(order))

                for cb in self._order_callbacks:
                    asyncio.create_task(cb(order))

            # 2. Positions
            positions = _get_attr(payload, "positions", None) or []
            if positions:
                for p_model in positions:
                    # SDK Compliance: PositionModel fields matches list_positions logic
                    # positions.py: PositionSide.LONG / SHORT
                    side_val = str(p_model.side).upper()
                    side = Side.BUY if side_val == "LONG" else Side.SELL

                    pos = Position(
                        symbol=p_model.market,
                        exchange=Exchange.X10,
                        side=side,
                        qty=_safe_decimal(p_model.size),  # SDK: size
                        entry_price=_safe_decimal(p_model.open_price),  # SDK: open_price
                        mark_price=_safe_decimal(p_model.mark_price),
                        liquidation_price=_safe_decimal(getattr(p_model, "liquidation_price", 0)) or None,
                        unrealized_pnl=_safe_decimal(p_model.unrealised_pnl),  # SDK: British spelling
                        leverage=_safe_decimal(p_model.leverage),
                    )

                    for cb in self._position_callbacks:
                        asyncio.create_task(cb(pos))

        except Exception as e:
            logger.error(f"Error handling X10 WS message: {e}")

    async def subscribe_positions(self, callback: Callable[[Position], Awaitable[None]]) -> None:
        """Subscribe to position updates."""
        self._position_callbacks.append(callback)
        if not self._ws_task and self._stream_client_cls:
            await self._start_websocket()
        logger.debug("X10 position subscription registered")

    async def subscribe_orders(self, callback: Callable[[Order], Awaitable[None]]) -> None:
        """Subscribe to order updates."""
        self._order_callbacks.append(callback)
        if not self._ws_task and self._stream_client_cls:
            await self._start_websocket()
        logger.debug("X10 order subscription registered")

    def ws_ready(self) -> bool:
        return bool(self._ws_ready.is_set()) and bool(self._ws_task and not self._ws_task.done())

    async def wait_ws_ready(self, *, timeout: float) -> bool:
        if self.ws_ready():
            return True
        if not self._ws_task and self._stream_client_cls:
            await self._start_websocket()
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=max(0.0, float(timeout)))
        except TimeoutError:
            return False
        return self.ws_ready()

    async def subscribe_orderbook_l1(
        self,
        symbols: list[str] | None = None,
        *,
        depth: int | None = None,
    ) -> None:
        """Subscribe to public orderbook updates and keep L1 cache hot."""
        try:
            parsed = int(depth) if depth is not None else 1
        except Exception:
            parsed = 1
        if parsed != 1:
            logger.warning("X10 L1 stream uses depth=1; ignoring depth=%s", parsed)
        self._ob_ws_depth = 1

        # Subscription mode based on settings.
        # NOTE: The ALL_MARKETS orderbook stream is very high throughput and tends to disconnect.
        # Prefer single-market subscriptions when a specific symbol is requested.
        multi_market = bool(getattr(self.settings.websocket, "x10_ws_orderbook_multi_market", False))

        raw_allow = set(symbols) if symbols else set()
        allow = {self._resolve_market_name(sym) for sym in raw_allow if sym}
        self._ob_ws_allowlist = allow
        self._ob_ws_markets = allow  # Track active set (normalized)

        if not allow:
            logger.debug("X10 orderbooks WS subscribe called with no symbols; skipping.")
            return

        if not multi_market and len(allow) > 1:
            logger.warning("X10 orderbooks WS multi-market disabled and multiple symbols requested; skipping WS start.")
            return

        # Prefer per-market WS tasks to avoid duplicate connections (shared with ensure_orderbook_ws()).
        if self._ob_ws_task and not self._ob_ws_task.done():
            self._ob_ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ob_ws_task
            self._ob_ws_task = None

        await asyncio.gather(
            *(self.ensure_orderbook_ws(sym, timeout=0.0) for sym in allow),
            return_exceptions=True,
        )

    async def _start_orderbook_websocket(self) -> None:
        """Start public orderbook stream task."""
        if self._ob_ws_task:
            return
        self._ob_ws_task = asyncio.create_task(self._ob_ws_loop())
        logger.info("X10 Orderbook WebSocket task started")

    async def _ob_ws_loop(self) -> None:
        """
        Orderbook WebSocket manager loop.

        REFACTORED: This loop no longer manages its own per-market tasks.
        It simply delegates to ensure_orderbook_ws() which is the sole authority
        for per-market connections. This prevents double-socket issues.
        """
        ws_settings = getattr(self.settings, "websocket", None)
        base_delay = float(getattr(ws_settings, "reconnect_delay_initial", 2.0) or 2.0)

        while True:
            try:
                allow = self._ob_ws_allowlist

                if not allow:
                    await asyncio.sleep(1.0)
                    continue

                # Delegate to ensure_orderbook_ws for each market
                # This handles creation, TTL, and pruning automatically
                await asyncio.gather(
                    *(self.ensure_orderbook_ws(market, timeout=0.0) for market in list(allow)),
                    return_exceptions=True,
                )

                # Short sleep before next check
                await asyncio.sleep(2.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"X10 orderbook WS manager error: {e}")
                await asyncio.sleep(base_delay)

    async def _handle_orderbook_ws_message(self, msg: Any) -> None:
        """Handle incoming orderbook WS message."""
        try:
            data = getattr(msg, "data", None)
            if not data:
                return

            market = str(getattr(data, "market", "") or "")
            if not market:
                return

            allow = self._ob_ws_allowlist
            if allow is not None and market not in allow:
                return

            bids_raw = getattr(data, "bid", None) or getattr(data, "bids", None) or []
            asks_raw = getattr(data, "ask", None) or getattr(data, "asks", None) or []

            # 1. Parse Full Depth for Depth Cache
            bids_parsed: list[tuple[Decimal, Decimal]] = []
            asks_parsed: list[tuple[Decimal, Decimal]] = []

            for row in bids_raw:
                if isinstance(row, (list, tuple)):
                    p = _safe_decimal(row[0])
                    q = _safe_decimal(row[1]) if len(row) > 1 else Decimal("0")
                else:
                    p = _safe_decimal(getattr(row, "price", 0))
                    q = _safe_decimal(getattr(row, "qty", 0) or getattr(row, "size", 0))
                if p > 0 and q > 0:
                    bids_parsed.append((p, q))

            for row in asks_raw:
                if isinstance(row, (list, tuple)):
                    p = _safe_decimal(row[0])
                    q = _safe_decimal(row[1]) if len(row) > 1 else Decimal("0")
                else:
                    p = _safe_decimal(getattr(row, "price", 0))
                    q = _safe_decimal(getattr(row, "qty", 0) or getattr(row, "size", 0))
                if p > 0 and q > 0:
                    asks_parsed.append((p, q))

            # Store full depth (sorted)
            if bids_parsed or asks_parsed:
                # Bids descending, Asks ascending
                bids_parsed.sort(key=lambda x: x[0], reverse=True)
                asks_parsed.sort(key=lambda x: x[0])

                now = datetime.now(UTC)
                self._orderbook_depth_cache[market] = {"bids": bids_parsed, "asks": asks_parsed}
                self._orderbook_depth_updated_at[market] = now

            # 2. Extract Best L1 for L1 Cache
            # (Re-use parsed list for consistency, or fallback to raw if empty but structure exists?)
            best_bid = Decimal("0")
            bid_qty = Decimal("0")
            if bids_parsed:
                best_bid, bid_qty = bids_parsed[0]

            best_ask = Decimal("0")
            ask_qty = Decimal("0")
            if asks_parsed:
                best_ask, ask_qty = asks_parsed[0]

            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                return

            now = datetime.now(UTC)
            self._orderbook_cache[market] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_qty": bid_qty,
                "ask_qty": ask_qty,
            }
            self._orderbook_updated_at[market] = now
            self._price_cache[market] = (best_bid + best_ask) / 2
            self._price_updated_at[market] = now

        except Exception as e:
            logger.debug(f"Error handling X10 orderbook WS message: {e}")

    # =========================================================================
    # Per-Market WebSocket (On-Demand, like Lighter pattern)
    # =========================================================================

    def _prune_orderbook_ws_tasks(self, *, keep_market: str | None = None) -> None:
        """
        Prune stale/done per-market WS tasks (best-effort, non-locking).

        This should be called periodically or before creating new connections
        to ensure we don't leak resources.

        Args:
            keep_market: If specified, this market is preserved even if TTL expired.
        """
        now = time.monotonic()
        to_remove: list[str] = []

        for market, task in list(self._per_market_ws_tasks.items()):
            # Always clean up done tasks
            if task.done():
                to_remove.append(market)
                continue

            # Check TTL
            last_used = self._ob_ws_last_used.get(market, 0.0)
            age = now - last_used

            # Skip if this is the market we explicitly want to keep
            if keep_market and market == keep_market:
                continue

            if age > self._ob_ws_ttl_seconds:
                logger.info(
                    f"Pruning stale X10 orderbook WS for {market} (age={age:.1f}s > TTL={self._ob_ws_ttl_seconds}s)"
                )
                task.cancel()
                to_remove.append(market)

        # Cleanup maps
        for market in to_remove:
            self._per_market_ws_tasks.pop(market, None)
            self._per_market_ws_ready.pop(market, None)
            self._ob_ws_last_used.pop(market, None)
            self._orderbook_depth_cache.pop(market, None)
            # Note: Keep L1 cache (_orderbook_cache) as it may still be useful for best-effort pricing

    async def ensure_orderbook_ws(self, symbol: str, *, timeout: float = 2.0) -> bool:
        """
        Ensure a per-market orderbook WS is running for the given symbol.

        This is the SOLE AUTHORITY for creating per-market WS connections.
        Other code (subscribe_orderbook_l1, _ob_ws_loop) should call this method.

        Args:
            symbol: Market symbol (e.g., "ETH-USD")
            timeout: Max seconds to wait for first snapshot. 0 means don't wait.

        Returns:
            True if ready (snapshot loaded), False if timeout or error.
        """
        if not symbol:
            return False

        market_name = self._resolve_market_name(symbol)

        async with self._per_market_ws_lock:
            # Prune stale connections before creating new ones (keeps resources bounded)
            self._prune_orderbook_ws_tasks(keep_market=market_name)

            # Update usage timestamp (this market is being actively requested)
            self._ob_ws_last_used[market_name] = time.monotonic()

            task = self._per_market_ws_tasks.get(market_name)
            if task is None or task.done():
                # Clear any stale ready state
                if market_name in self._per_market_ws_ready:
                    self._per_market_ws_ready[market_name].clear()
                else:
                    self._per_market_ws_ready[market_name] = asyncio.Event()

                # Start new per-market WS task
                self._per_market_ws_tasks[market_name] = asyncio.create_task(
                    self._per_market_orderbook_ws_loop(market_name),
                    name=f"x10_ob_ws_{market_name}",
                )
                logger.info(f"X10 per-market orderbook WS started (on-demand): symbol={market_name}")

        if timeout <= 0:
            return True

        # Wait for first snapshot
        ev = self._per_market_ws_ready.get(market_name)
        if ev is None:
            return False
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, float(timeout)))
        except TimeoutError:
            logger.warning(f"X10 orderbook WS wait timeout for {market_name}")
            return False
        return ev.is_set()

    async def _per_market_orderbook_ws_loop(self, market_name: str) -> None:
        """
        Per-market orderbook WebSocket loop.

        Connects to Extended's depth=1 orderbook stream for best bid/ask (L1).
        - Push frequency: 10ms (snapshots)
        - Used for fast hedge pricing; full depth is fetched on-demand via REST.

        Stream URL: wss://stream.extended.exchange/v1/orderbooks/{market}?depth=1
        """

        base_delay = 1.0
        max_delay = 30.0
        delay = base_delay
        last_seq: int | None = None  # Track sequence for gap detection

        while True:
            try:
                # Build WebSocket URL for this specific market (L1 depth=1)
                base_url = self._get_stream_api_url()
                # Pattern: wss://host/stream.extended.exchange/v1/orderbooks/{market}
                if "/stream.extended.exchange/" in base_url:
                    ws_url = f"{base_url}/orderbooks/{market_name}"
                else:
                    ws_url = f"{base_url}/stream.extended.exchange/v1/orderbooks/{market_name}"
                if self._ob_ws_depth == 1:
                    ws_url = f"{ws_url}?depth=1"

                logger.debug(f"X10 per-market WS (L1) connecting to: {ws_url}")

                try:
                    import websockets
                except ImportError:
                    logger.error("websockets library not installed for X10 per-market WS")
                    return

                extra_headers = None
                with contextlib.suppress(Exception):
                    from x10.config import USER_AGENT

                    extra_headers = {"User-Agent": USER_AGENT}

                async with websockets.connect(
                    ws_url,
                    # 🚀 PERFORMANCE: Match X10 API docs (server pings every 15s, expects pong within 10s)
                    ping_interval=15,
                    ping_timeout=10,
                    extra_headers=extra_headers,
                ) as ws:  # type: ignore[attr-defined]
                    delay = base_delay  # Reset backoff on successful connect
                    last_seq = None  # Reset sequence tracker on new connection

                    async for raw in ws:
                        # Debug logging (reduced to WARNING to avoid spam)
                        if isinstance(raw, str) and raw == "ping":
                            logger.debug(f"[WS_PING] {market_name}: Keepalive")
                        else:
                            msg_type = "unknown"
                            try:
                                msg = json_loads(raw) if isinstance(raw, str) else raw
                                msg_type = msg.get("type", "unknown") if isinstance(msg, dict) else "binary"
                            except Exception:
                                pass
                            if msg_type in ("SNAPSHOT", "DELTA"):
                                logger.debug(f"[WS] {market_name}: {msg_type}")

                        # Handle ping/pong (text "ping")
                        if raw == "ping":
                            await ws.send("pong")
                            continue

                        try:
                            msg = json_loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            continue

                        # Handle Extended's JSON PING message type
                        if isinstance(msg, dict) and msg.get("type") == "PING":
                            await ws.send(json_dumps({"type": "PONG"}))
                            continue

                        # Check sequence for gap detection
                        seq = msg.get("seq") if isinstance(msg, dict) else None
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                logger.warning(
                                    f"X10 WS seq gap for {market_name}: expected "
                                    f"{last_seq + 1}, got {seq}. Reconnecting..."
                                )
                                break  # Reconnect to resync
                            last_seq = seq

                        # Process orderbook update (SNAPSHOT or DELTA)
                        await self._handle_per_market_ws_message(market_name, msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"X10 per-market WS error for {market_name}: {e}")

            # Exponential backoff with jitter
            jitter = random.random() * 0.25 * delay
            await asyncio.sleep(min(max_delay, delay + jitter))
            delay = min(max_delay, max(base_delay, delay * 2))

    async def _handle_per_market_ws_message(self, market_name: str, msg: dict) -> None:
        """Handle incoming per-market L1 orderbook WS message."""
        try:
            # Update usage timestamp (keeps connection alive during activity)
            self._ob_ws_last_used[market_name] = time.monotonic()

            msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
            if msg_type:
                logger.debug(f"X10 per-market WS msg: market={market_name} type={msg_type}")

            payload = msg.get("data", msg) if isinstance(msg, dict) else msg
            if not isinstance(payload, dict):
                return

            # Verify market matches (best-effort)
            msg_market = payload.get("m") or payload.get("market") or payload.get("symbol") or ""
            if msg_market and str(msg_market) != market_name:
                return

            bids_raw = payload.get("b")
            asks_raw = payload.get("a")
            if bids_raw is None and asks_raw is None:
                bids_raw = payload.get("bid") or payload.get("bids")
                asks_raw = payload.get("ask") or payload.get("asks")

            def coerce_rows(raw: Any) -> list[Any]:
                if raw is None:
                    return []
                if isinstance(raw, dict):
                    return [raw]
                if isinstance(raw, (list, tuple)):
                    if raw and not isinstance(raw[0], (list, tuple, dict)):
                        return [raw]
                    return list(raw)
                return [raw]

            def parse_levels(raw_list: list[Any]) -> list[tuple[Decimal, Decimal]]:
                result: list[tuple[Decimal, Decimal]] = []
                for entry in raw_list:
                    if isinstance(entry, (list, tuple)):
                        price = _safe_decimal(entry[0] if len(entry) > 0 else 0)
                        qty = _safe_decimal(entry[1] if len(entry) > 1 else 0)
                    elif isinstance(entry, dict):
                        price = _safe_decimal(entry.get("p") or entry.get("price") or entry.get("px") or 0)
                        qty = _safe_decimal(entry.get("q") or entry.get("qty") or entry.get("size") or 0)
                    else:
                        price = _safe_decimal(getattr(entry, "price", 0))
                        qty = _safe_decimal(getattr(entry, "qty", 0) or getattr(entry, "size", 0))
                    if price > 0:
                        result.append((price, qty))
                return result

            bids = parse_levels(coerce_rows(bids_raw))
            asks = parse_levels(coerce_rows(asks_raw))

            if not bids or not asks:
                return

            # Extract best bid/ask for L1 cache
            best_bid, bid_qty = max(bids, key=lambda x: x[0])
            best_ask, ask_qty = min(asks, key=lambda x: x[0])

            # Validate book isn't crossed
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                logger.debug(f"X10 per-market WS invalid book: {market_name} bid={best_bid} ask={best_ask}")
                return

            # Update L1 caches only (depth is fetched on-demand via REST)
            now = datetime.now(UTC)

            self._orderbook_cache[market_name] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_qty": bid_qty,
                "ask_qty": ask_qty,
            }
            self._orderbook_updated_at[market_name] = now

            # Price cache (mid-price)
            self._price_cache[market_name] = (best_bid + best_ask) / 2
            self._price_updated_at[market_name] = now

            # Mark ready on first valid update
            ev = self._per_market_ws_ready.get(market_name)
            if ev and not ev.is_set():
                ev.set()
                logger.info(f"X10 per-market WS ready: {market_name} bid={best_bid} ask={best_ask}")

        except Exception as e:
            logger.debug(f"X10 per-market WS message error: {e}")

    async def validate_funding_rate_cache(self, sample_size: int = 3) -> dict:
        """
        Validate cached funding rates against fresh data.

        X10 doesn't cache funding rates - it fetches fresh rates on every call.
        This method always returns OK status for X10.

        Args:
            sample_size: Number of markets to validate (unused, for API compatibility)

        Returns:
            dict with status="ok" (no discrepancies possible)
        """
        return {
            "status": "ok",
            "discrepancies": [],
        }

    async def validate_price_cache(self, sample_size: int = 3) -> dict:
        """
        Validate cached prices against fresh data.

        X10 updates price cache via WebSocket and refreshes stale data on demand.
        This method performs a best-effort validation.

        Args:
            sample_size: Number of markets to validate

        Returns:
            dict with status and any discrepancies found
        """
        discrepancies = []

        # Sample a few markets if we have cached data
        if not self._price_cache:
            return {"status": "ok", "discrepancies": []}

        # Get sample of market names
        market_names = list(self._price_cache.keys())[:sample_size]

        for market_name in market_names:
            try:
                # Fetch fresh price via REST
                markets = await self._public_client.get_markets()
                fresh_price = None
                for market in markets:
                    m_name = getattr(market, "name", None) or market.get("name") if isinstance(market, dict) else None
                    if m_name == market_name:
                        # Get mark price or last price
                        fresh_price = _safe_decimal(
                            getattr(market, "mark_price", None)
                            or (market.get("mark_price") if isinstance(market, dict) else None)
                            or getattr(market, "last_price", None)
                            or (market.get("last_price") if isinstance(market, dict) else None)
                            or 0
                        )
                        break

                if fresh_price and fresh_price > 0:
                    cached_price = self._price_cache.get(market_name, Decimal("0"))
                    # Check for significant discrepancy (>1%)
                    if cached_price > 0:
                        diff_pct = abs(cached_price - fresh_price) / fresh_price
                        if diff_pct > Decimal("0.01"):
                            discrepancies.append(
                                f"{market_name}: cached={cached_price:.6f}, "
                                f"fresh={fresh_price:.6f}, diff={diff_pct:.2%}"
                            )
            except Exception as e:
                logger.debug(f"Price validation error for {market_name}: {e}")

        if discrepancies:
            return {
                "status": "discrepancies_found",
                "discrepancies": discrepancies,
            }

        return {"status": "ok", "discrepancies": []}
