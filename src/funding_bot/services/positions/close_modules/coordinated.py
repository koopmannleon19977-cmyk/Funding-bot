"""
Coordinated dual-leg close operations.

Handles synchronized closing of both position legs to minimize
unhedged exposure during close operations.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.positions.close.logging import (
    log_close_order_placed,
    log_coordinated_close_complete,
    log_coordinated_close_ioc_escalate,
    log_coordinated_close_maker_placed,
    log_coordinated_close_start,
)
from funding_bot.services.positions.utils import _round_price_to_tick

logger = get_logger(__name__)


@dataclass
class CoordinatedCloseContext:
    """Context for coordinated close operation."""

    trade: Trade
    price_data: Any
    ob: Any
    leg1_close_qty: Decimal
    leg2_close_qty: Decimal
    leg1_close_side: Side
    leg2_close_side: Side
    leg1_maker_price: Decimal
    leg2_maker_price: Decimal
    lighter_tick: Decimal
    x10_tick: Decimal
    x10_use_maker: bool
    maker_timeout: float
    escalate_to_ioc: bool


def build_coordinated_close_context(
    manager,  # PositionManager instance
    trade: Trade,
) -> CoordinatedCloseContext | None:
    """
    Build context for coordinated close operation.

    Returns None if there's nothing to close.
    """
    ts = manager.settings.trading
    x10_use_maker = bool(getattr(ts, "coordinated_close_x10_use_maker", True))
    maker_timeout = float(getattr(ts, "coordinated_close_maker_timeout_seconds", 6.0))
    escalate_to_ioc = bool(getattr(ts, "coordinated_close_escalate_to_ioc", True))

    price_data = manager.market_data.get_price(trade.symbol)
    ob = manager.market_data.get_orderbook(trade.symbol)

    leg1_close_qty = trade.leg1.filled_qty
    leg2_close_qty = trade.leg2.filled_qty

    if leg1_close_qty == 0 and leg2_close_qty == 0:
        return None

    # Get market info for tick sizes
    lighter_info = manager.market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
    x10_info = manager.market_data.get_market_info(trade.symbol, Exchange.X10)
    lighter_tick = lighter_info.tick_size if lighter_info else Decimal("0.01")
    x10_tick = x10_info.tick_size if x10_info else Decimal("0.01")

    # Determine close sides
    leg1_close_side = trade.leg1.side.inverse()
    leg2_close_side = trade.leg2.side.inverse()

    # Calculate maker prices
    leg1_maker_price = calculate_leg_maker_price(
        ob, price_data, leg1_close_qty, leg1_close_side, "lighter", lighter_tick
    )
    leg2_maker_price = calculate_leg_maker_price(
        ob, price_data, leg2_close_qty, leg2_close_side, "x10", x10_tick
    )

    return CoordinatedCloseContext(
        trade=trade,
        price_data=price_data,
        ob=ob,
        leg1_close_qty=leg1_close_qty,
        leg2_close_qty=leg2_close_qty,
        leg1_close_side=leg1_close_side,
        leg2_close_side=leg2_close_side,
        leg1_maker_price=leg1_maker_price,
        leg2_maker_price=leg2_maker_price,
        lighter_tick=lighter_tick,
        x10_tick=x10_tick,
        x10_use_maker=x10_use_maker,
        maker_timeout=maker_timeout,
        escalate_to_ioc=escalate_to_ioc,
    )


def calculate_leg_maker_price(
    ob: Any,
    price_data: Any,
    close_qty: Decimal,
    close_side: Side,
    exchange: str,
    tick: Decimal,
) -> Decimal:
    """Calculate maker price for a leg based on orderbook or price data."""
    if close_qty <= 0:
        return Decimal("0")

    price = Decimal("0")

    if exchange == "lighter":
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            price = ob.lighter_bid if close_side == Side.SELL else ob.lighter_ask
        elif price_data and price_data.lighter_price > 0:
            price = price_data.lighter_price
    else:  # x10
        if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
            price = ob.x10_bid if close_side == Side.SELL else ob.x10_ask
        elif price_data and price_data.x10_price > 0:
            price = price_data.x10_price

    if price > 0:
        return _round_price_to_tick(price, tick, rounding="down")
    return Decimal("0")


def calculate_ioc_price(
    ob: Any,
    price_data: Any,
    close_side: Side,
    exchange: str,
    tick: Decimal,
) -> Decimal:
    """Calculate IOC price (cross spread) for a leg."""
    price = Decimal("0")

    if exchange == "lighter":
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            price = ob.lighter_ask if close_side == Side.BUY else ob.lighter_bid
        elif price_data and price_data.lighter_price > 0:
            price = price_data.lighter_price
    else:  # x10
        if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
            price = ob.x10_ask if close_side == Side.BUY else ob.x10_bid
        elif price_data and price_data.x10_price > 0:
            price = price_data.x10_price

    if price > 0:
        return _round_price_to_tick(price, tick, rounding="down")
    return Decimal("0")


async def submit_maker_order(
    manager,  # PositionManager instance
    trade: Trade,
    exchange: Exchange,
    leg: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
    tick: Decimal,
) -> Order | None:
    """Submit a maker POST_ONLY order for coordinated close."""
    adapter = manager.lighter if exchange == Exchange.LIGHTER else manager.x10

    # Both exchanges support POST_ONLY TimeInForce
    # X10 adapter converts POST_ONLY to post_only=True flag internally
    request = OrderRequest(
        symbol=trade.symbol,
        exchange=exchange,
        side=side,
        qty=qty,
        price=_round_price_to_tick(price, tick, rounding="down"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=True,
    )

    order = await adapter.place_order(request)

    if order:
        # Log coordinated close specific event
        log_coordinated_close_maker_placed(
            trade=trade,
            exchange=exchange,
            leg=leg,
            order_id=str(order.order_id),
            side=side,
            qty=qty,
            price=price,
        )

        logger.info(
            f"Maker order submitted: {exchange.value} {leg} {side.value} "
            f"{qty} @ {price} = {order.order_id}"
        )

        # Log the close order event (for post-close readback)
        log_close_order_placed(
            trade=trade,
            exchange=exchange,
            leg=leg,
            order_id=order.order_id,
            client_order_id=getattr(order, "client_order_id", None),
            side=side,
            qty=qty,
            price=price,
            time_in_force=request.time_in_force,
            post_only=True,
        )

    return order


async def submit_coordinated_maker_orders(
    manager,  # PositionManager instance
    ctx: CoordinatedCloseContext,
) -> tuple[dict[str, Order | None], dict[str, str]]:
    """Submit maker orders for both legs in parallel."""
    maker_orders: dict[str, Order | None] = {}
    maker_order_ids: dict[str, str] = {}
    maker_tasks: dict[str, Awaitable[Order]] = {}

    if ctx.leg1_close_qty > 0 and ctx.leg1_maker_price > 0:
        logger.info(
            f"Submitting Lighter maker: {ctx.leg1_close_side.value} "
            f"{ctx.leg1_close_qty} @ {ctx.leg1_maker_price}"
        )
        maker_tasks["leg1"] = submit_maker_order(
            manager,
            ctx.trade,
            Exchange.LIGHTER,
            "leg1",
            ctx.leg1_close_side,
            ctx.leg1_close_qty,
            ctx.leg1_maker_price,
            ctx.lighter_tick,
        )

    if ctx.leg2_close_qty > 0 and ctx.leg2_maker_price > 0 and ctx.x10_use_maker:
        logger.info(
            f"Submitting X10 maker: {ctx.leg2_close_side.value} "
            f"{ctx.leg2_close_qty} @ {ctx.leg2_maker_price}"
        )
        maker_tasks["leg2"] = submit_maker_order(
            manager,
            ctx.trade,
            Exchange.X10,
            "leg2",
            ctx.leg2_close_side,
            ctx.leg2_close_qty,
            ctx.leg2_maker_price,
            ctx.x10_tick,
        )

    if maker_tasks:
        results = await asyncio.gather(*maker_tasks.values(), return_exceptions=True)
        for leg, result in zip(maker_tasks.keys(), results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(f"Maker order failed for {leg}: {result}")
                maker_orders[leg] = None
            else:
                order_result: Order | None = result
                maker_orders[leg] = order_result
                if order_result:
                    maker_order_ids[leg] = order_result.order_id
                    logger.info(f"Maker order placed: {leg} = {order_result.order_id}")

    return maker_orders, maker_order_ids


async def wait_for_coordinated_fills(
    manager,  # PositionManager instance
    trade: Trade,
    legs_to_close: list[str],
    maker_order_ids: dict[str, str],
    maker_timeout: float,
) -> set[str]:
    """Wait for maker orders to fill with timeout."""
    start_time = time.time()
    filled_legs: set[str] = set()

    while time.time() - start_time < maker_timeout and legs_to_close:
        await asyncio.sleep(0.3)

        for leg in list(legs_to_close):
            if leg not in maker_order_ids:
                continue

            order_id = maker_order_ids[leg]
            adapter = manager.lighter if leg == "leg1" else manager.x10

            try:
                updated = await adapter.get_order(trade.symbol, order_id)
                if not updated:
                    continue

                if updated.is_filled or updated.status == OrderStatus.FILLED:
                    logger.info(
                        f"Maker filled for {leg}: {updated.filled_qty} "
                        f"@ {updated.avg_fill_price}"
                    )
                    filled_legs.add(leg)
                    legs_to_close.remove(leg)
                    # Update trade leg
                    if leg == "leg1":
                        trade.leg1.exit_price = updated.avg_fill_price
                        trade.leg1.fees += updated.fee
                    else:
                        trade.leg2.exit_price = updated.avg_fill_price
                        trade.leg2.fees += updated.fee
                elif not updated.is_active:
                    logger.warning(
                        f"Maker order {leg} no longer active: {updated.status}"
                    )
                    legs_to_close.remove(leg)
            except Exception as e:
                logger.warning(f"Failed to check maker order for {leg}: {e}")

    return filled_legs


async def execute_ioc_close(
    manager,  # PositionManager instance
    trade: Trade,
    exchange: Exchange,
    leg: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
) -> Order | None:
    """Execute an IOC close order for a leg."""
    adapter = manager.lighter if exchange == Exchange.LIGHTER else manager.x10

    request = OrderRequest(
        symbol=trade.symbol,
        exchange=exchange,
        side=side,
        qty=qty,
        price=price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )

    order = await adapter.place_order(request)

    if order:
        log_close_order_placed(
            trade=trade,
            exchange=exchange,
            leg=leg,
            order_id=order.order_id,
            client_order_id=getattr(order, "client_order_id", None),
            side=side,
            qty=qty,
            price=price,
            time_in_force=TimeInForce.IOC,
            post_only=False,
        )
        logger.info(
            f"IOC order placed: {exchange.value} {leg} {side.value} "
            f"{qty} @ {price} = {order.order_id}"
        )

    return order


async def escalate_unfilled_to_ioc(
    manager,  # PositionManager instance
    ctx: CoordinatedCloseContext,
    legs_to_close: list[str],
    filled_legs: set[str],
) -> None:
    """Escalate unfilled legs to IOC orders."""
    log_coordinated_close_ioc_escalate(trade=ctx.trade, legs=list(legs_to_close))
    logger.info(
        f"Escalating to IOC for unfilled legs: {', '.join(legs_to_close)} (synchronous)"
    )

    ioc_tasks = []

    if "leg1" in legs_to_close and ctx.leg1_close_qty > 0:
        ioc_price = calculate_ioc_price(
            ctx.ob, ctx.price_data, ctx.leg1_close_side, "lighter", ctx.lighter_tick
        )
        if ioc_price > 0:
            ioc_tasks.append(
                execute_ioc_close(
                    manager,
                    ctx.trade,
                    Exchange.LIGHTER,
                    "leg1",
                    ctx.leg1_close_side,
                    ctx.leg1_close_qty,
                    ioc_price,
                )
            )

    if "leg2" in legs_to_close and ctx.leg2_close_qty > 0:
        ioc_price = calculate_ioc_price(
            ctx.ob, ctx.price_data, ctx.leg2_close_side, "x10", ctx.x10_tick
        )
        if ioc_price > 0:
            ioc_tasks.append(
                execute_ioc_close(
                    manager,
                    ctx.trade,
                    Exchange.X10,
                    "leg2",
                    ctx.leg2_close_side,
                    ctx.leg2_close_qty,
                    ioc_price,
                )
            )

    if ioc_tasks:
        results = await asyncio.gather(*ioc_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"IOC close failed: {result}")
            else:
                logger.info(f"IOC close completed: {result}")

        log_coordinated_close_complete(trade=ctx.trade, filled_legs=filled_legs)


async def close_both_legs_coordinated(manager, trade: Trade) -> None:
    """
    Coordinated dual-leg close to minimize unhedged exposure.

    Strategy:
    1. Submit POST_ONLY maker orders on BOTH legs simultaneously
    2. Wait for fills with short timeout
    3. Escalate UNFILLED legs to IOC on BOTH (no unhedged window)
    4. Fallback to sequential close on error
    """
    ts = manager.settings.trading
    coordinated_enabled = bool(getattr(ts, "coordinated_close_enabled", True))

    if not coordinated_enabled:
        logger.info(
            f"Coordinated close disabled for {trade.symbol}, "
            "falling back to sequential"
        )
        await manager._close_lighter_smart(trade)
        await manager._update_trade(trade)
        await manager._close_x10_smart(trade)
        await manager._update_trade(trade)
        return

    log_coordinated_close_start(trade)
    logger.info(
        f"Coordinated close for {trade.symbol} - parallel maker orders on both legs"
    )

    # Build context
    ctx = build_coordinated_close_context(manager, trade)
    if ctx is None:
        logger.info(f"No position to close for {trade.symbol}")
        return

    # Determine which legs need closing
    legs_to_close = []
    if ctx.leg1_close_qty > 0:
        legs_to_close.append("leg1")
    if ctx.leg2_close_qty > 0:
        legs_to_close.append("leg2")

    try:
        # Step 1: Submit maker orders
        _, maker_order_ids = await submit_coordinated_maker_orders(manager, ctx)

        # Step 2: Wait for fills
        filled_legs = await wait_for_coordinated_fills(
            manager, trade, legs_to_close, maker_order_ids, ctx.maker_timeout
        )

        if not legs_to_close:
            logger.info(f"All legs filled via maker orders for {trade.symbol}")
            log_coordinated_close_complete(trade=trade, filled_legs=filled_legs)
            return

        logger.info(
            f"Maker timeout: {len(legs_to_close)} legs remaining "
            f"({', '.join(legs_to_close)})"
        )

        # Step 3: Escalate to IOC
        if ctx.escalate_to_ioc:
            await escalate_unfilled_to_ioc(manager, ctx, legs_to_close, filled_legs)

    except Exception as e:
        logger.warning(f"Coordinated close failed for {trade.symbol}: {e}")
        logger.info(f"Falling back to sequential close for {trade.symbol}")
        await manager._close_lighter_smart(trade)
        await manager._update_trade(trade)
        await manager._close_x10_smart(trade)
        await manager._update_trade(trade)
