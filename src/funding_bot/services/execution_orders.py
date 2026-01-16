"""
Execution order helpers (moved from ExecutionEngine).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from funding_bot.domain.errors import (
    OrderRejectedError,
)
from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.services.liquidity_gates import (
    estimate_taker_vwap_price_by_impact,
)
from funding_bot.services.market_data import (
    OrderbookDepthSnapshot,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")

async def _place_lighter_taker_ioc(
    self,
    *,
    symbol: str,
    side: Side,
    qty: Decimal,
    slippage: Decimal,
    reduce_only: bool,
    timeout_seconds: float,
    purpose: str,
) -> Order | None:
    """
    Place a marketable LIMIT IOC on Lighter with an explicit slippage cap.

    We avoid adapter MARKET emulation (which uses a wide fixed slippage) and instead
    compute a tight price from a fresh L1 snapshot.
    """
    if qty <= 0:
        return None

    book = await self._get_best_effort_orderbook(symbol, max_age_seconds=1.0)

    best_bid = Decimal("0")
    best_ask = Decimal("0")
    l1_qty = Decimal("0")
    if book:
        best_bid = book.lighter_bid
        best_ask = book.lighter_ask
        l1_qty = book.lighter_ask_qty if side == Side.BUY else book.lighter_bid_qty

    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        price_data = self.market_data.get_price(symbol)
        if price_data:
            best_bid = price_data.lighter_price * Decimal("0.9999")
            best_ask = price_data.lighter_price * Decimal("1.0001")

    base_price = best_ask if side == Side.BUY else best_bid

    # Smart Pricing: if the top-of-book is too thin for our taker IOC, use depth-VWAP
    # within a capped impact window as the base.
    es = self.settings.execution
    smart_enabled = bool(getattr(es, "smart_pricing_enabled", False))
    smart_levels = int(
        getattr(es, "smart_pricing_depth_levels", 0)
        or getattr(self.settings.trading, "depth_gate_levels", 20)
        or 20
    )
    smart_levels = max(1, min(smart_levels, 250))
    smart_impact = _safe_decimal(
        getattr(es, "smart_pricing_max_price_impact_percent", None),
        default=_safe_decimal(
            getattr(self.settings.trading, "depth_gate_max_price_impact_percent", None),
            default=Decimal("0.0015"),
        ),
    )
    smart_l1_util_trigger = _safe_decimal(
        getattr(es, "smart_pricing_l1_utilization_trigger", None),
        default=_safe_decimal(
            getattr(self.settings.trading, "max_l1_qty_utilization", None),
            default=Decimal("0.7"),
        ),
    )
    if smart_l1_util_trigger <= 0:
        smart_l1_util_trigger = Decimal("0.7")

    if smart_enabled and base_price > 0:
        util = qty / l1_qty if l1_qty > 0 else Decimal("1e18")
        if util > smart_l1_util_trigger:
            try:
                depth = await self.lighter.get_orderbook_depth(symbol, depth=smart_levels)  # type: ignore[attr-defined]
                if not isinstance(depth, dict):
                    raise TypeError("lighter.get_orderbook_depth returned non-dict")
                bids = depth.get("bids") or []
                asks = depth.get("asks") or []
                depth_ob = OrderbookDepthSnapshot(
                    symbol=symbol,
                    lighter_bids=bids,
                    lighter_asks=asks,
                )
                vwap_price, _m = estimate_taker_vwap_price_by_impact(
                    depth_ob,
                    exchange="LIGHTER",
                    side=side,
                    target_qty=qty,
                    max_price_impact_pct=smart_impact,
                )
                if vwap_price and vwap_price > 0:
                    base_price = vwap_price
            except Exception as e:
                logger.debug(f"SmartPricing depth VWAP failed (Lighter {symbol}): {e}")

    if base_price <= 0:
        raise OrderRejectedError(
            f"Lighter taker IOC failed ({purpose}): no price data",
            exchange="LIGHTER",
            symbol=symbol,
        )

    lighter_info = self.market_data.get_market_info(symbol, Exchange.LIGHTER)
    tick = lighter_info.tick_size if lighter_info else Decimal("0.01")

    if side == Side.BUY:
        raw = base_price * (Decimal("1") + slippage)
        price = (raw / tick).quantize(Decimal("1"), rounding="ROUND_CEILING") * tick
    else:
        raw = base_price * (Decimal("1") - slippage)
        price = (raw / tick).quantize(Decimal("1"), rounding="ROUND_FLOOR") * tick

    logger.info(
        f"Lighter taker IOC ({purpose}): {side.value} {qty} {symbol} @ {price:.6f} "
        f"(Base: {base_price:.6f}, Slippage: {slippage:.4%})"
    )

    request = OrderRequest(
        symbol=symbol,
        exchange=Exchange.LIGHTER,
        side=side,
        qty=qty,
        order_type=OrderType.LIMIT,
        price=price,
        time_in_force=TimeInForce.IOC,
        reduce_only=reduce_only,
    )

    order = await self.lighter.place_order(request)
    filled = await self._wait_for_fill(
        self.lighter,
        symbol,
        order.order_id,
        timeout=timeout_seconds,
    )
    return filled

async def _wait_for_fill(
    self,
    adapter: ExchangePort,
    symbol: str,
    order_id: str,
    timeout: float = 30.0,
    check_callback: Callable[[], Awaitable[bool]] | None = None,
) -> Order | None:
    """Wait for order to fill or timeout."""
    await self._ensure_ws_order_subscriptions()

    ws_enabled = bool(getattr(self.settings.execution, "ws_fill_wait_enabled", True))
    if not ws_enabled:
        return await self._wait_for_fill_polling(
            adapter, symbol, order_id, timeout=timeout, check_callback=check_callback
        )

    # If the adapter can't confirm WS readiness, fall back to tight polling. This avoids a
    # false sense of safety where we wait on WS but end up reacting seconds late.
    ready_fn = getattr(adapter, "ws_ready", None)
    if callable(ready_fn):
        with contextlib.suppress(Exception):
            if not bool(ready_fn()):
                return await self._wait_for_fill_polling(
                    adapter, symbol, order_id, timeout=timeout, check_callback=check_callback
                )

    is_pending_placeholder = order_id.startswith("pending_")
    skip_initial_poll = adapter.exchange == Exchange.X10

    # For Lighter we may have a placeholder `pending_{client_index}_{market}` until the system
    # order id is discoverable via REST. WS updates include `client_order_index`, so we can
    # still wait event-driven by keying on the client id.
    watch_key = (adapter.exchange, order_id)
    if is_pending_placeholder:
        parts = order_id.split("_")
        client_id = parts[1] if len(parts) >= 3 else ""
        if client_id:
            watch_key = (adapter.exchange, f"client:{client_id}")
        else:
            return await self._wait_for_fill_polling(
                adapter, symbol, order_id, timeout=timeout, check_callback=check_callback
            )

    q: asyncio.Queue[Order] = asyncio.Queue(maxsize=20)
    cached: Order | None = None

    async with self._ws_orders_lock:
        self._order_watchers.setdefault(watch_key, set()).add(q)
        cached = self._last_order_update.get(watch_key)

    def _is_effectively_done(order: Order) -> bool:
        # "Done" means we should stop waiting and return the latest order snapshot.
        # - Terminal status: filled/cancelled/rejected/expired
        # - Some adapters may lag status updates; if filled_qty covers full qty, treat as done.
        if order.status.is_terminal():
            return True
        return order.qty > 0 and order.filled_qty >= order.qty

    try:
        # Handle the common race where a WS update arrives before the watcher is registered.
        if cached and _is_effectively_done(cached):
            return cached

        # Avoid a synchronous REST get_order for placeholder ids: on Lighter this can trigger a
        # client->system id resolution path and defeat the WS fast-path. We still keep periodic
        # polling below as a safety net.
        if not is_pending_placeholder and cached is None and not skip_initial_poll:
            try:
                first = await adapter.get_order(symbol, order_id)
            except Exception as e:
                logger.debug(f"Initial order poll failed for {symbol} {order_id}: {e}")
                first = None
            if first:
                self._last_order_update[watch_key] = first
                if _is_effectively_done(first):
                    return first

        t0 = time.monotonic()
        poll_every = float(
            _safe_decimal(
                getattr(self.settings.execution, "ws_fill_wait_fallback_poll_seconds", None),
                default=Decimal("2.0"),
            )
        )
        poll_every = max(0.2, min(poll_every, 10.0))
        next_poll = t0 + poll_every

        while True:
            elapsed = time.monotonic() - t0
            remaining = timeout - elapsed
            if remaining <= 0:
                break

            # Wait primarily for WS updates; poll occasionally as a safety net.
            wait_s = min(remaining, max(0.05, next_poll - time.monotonic()))
            try:
                upd = await asyncio.wait_for(q.get(), timeout=wait_s)
                self._last_order_update[watch_key] = upd
                if _is_effectively_done(upd):
                    return upd
                if check_callback:
                    await check_callback()
                continue
            except TimeoutError:
                if check_callback:
                    await check_callback()

            if time.monotonic() >= next_poll:
                try:
                    polled = await adapter.get_order(symbol, order_id)
                except Exception as e:
                    logger.debug(f"Order poll failed for {symbol} {order_id}: {e}")
                    polled = None
                next_poll = time.monotonic() + poll_every
                if polled:
                    self._last_order_update[watch_key] = polled
                    if _is_effectively_done(polled):
                        return polled

        # Final check
        return await adapter.get_order(symbol, order_id)

    finally:
        async with self._ws_orders_lock:
            watchers = self._order_watchers.get(watch_key)
            if watchers and q in watchers:
                watchers.remove(q)
            if watchers is not None and not watchers:
                self._order_watchers.pop(watch_key, None)

async def _wait_for_fill_polling(
    self,
    adapter: ExchangePort,
    symbol: str,
    order_id: str,
    *,
    timeout: float,
    check_callback: Callable[[], Awaitable[bool]] | None = None,
) -> Order | None:
    poll_interval = 0.5
    elapsed = 0.0

    while elapsed < timeout:
        try:
            order = await adapter.get_order(symbol, order_id)
        except Exception as e:
            logger.debug(f"Order poll failed for {symbol} {order_id}: {e}")
            order = None

        if order:
            # CRITICAL FIX: Check filled quantity FIRST.
            # IOC orders on X10 might return status=EXPIRED
            # even if filled. Trust `filled_qty` as source of truth.
            is_effectively_done = (
                order.is_filled
                or order.status in (
                    OrderStatus.EXPIRED,
                    OrderStatus.CANCELLED,
                )
            )
            if order.filled_qty > 0 and is_effectively_done:
                return order

            if order.is_filled:
                return order

            if order.status in (
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                if order.filled_qty == 0:
                    logger.warning(
                        f"Order {order_id} ended early without fill: "
                        f"status={order.status}"
                    )
                else:
                    logger.debug(
                        f"Order {order_id} ended early: "
                        f"status={order.status}"
                    )
                return order

            if order.status.is_terminal():
                return order

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        if check_callback:
            await check_callback()

    return await adapter.get_order(symbol, order_id)

