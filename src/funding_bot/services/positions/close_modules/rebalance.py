"""
Rebalance operations for position management.

Handles delta drift correction by reducing the larger leg
to restore delta neutrality without closing the entire position.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
    TradeLeg,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.positions.close.logging import (
    log_rebalance_complete,
    log_rebalance_order_placed,
    log_rebalance_start,
)
from funding_bot.services.positions.utils import _round_price_to_tick
from funding_bot.utils.decimals import safe_decimal

if TYPE_CHECKING:
    from funding_bot.ports.exchange import ExchangePort
    from funding_bot.services.market_data import MarketDataService

logger = get_logger(__name__)

# Track symbols that have been warned about skipped rebalance
_rebalance_skip_warned: set[str] = set()


def calculate_rebalance_target(
    trade: Trade,
    leg1_mark_price: Decimal | None,
    leg2_mark_price: Decimal | None,
) -> tuple[Exchange, TradeLeg, Decimal, Decimal]:
    """
    Calculate which leg to rebalance and by how much.

    Determines the net delta of the position and identifies which leg
    needs to be reduced to restore delta neutrality.

    Args:
        trade: Trade with leg1 (Lighter) and leg2 (X10).
        leg1_mark_price: Current mark price for leg1, or None to use entry.
        leg2_mark_price: Current mark price for leg2, or None to use entry.

    Returns:
        Tuple of (exchange, leg, rebalance_notional, net_delta):
        - exchange: Which exchange to trade on
        - leg: The TradeLeg to reduce
        - rebalance_notional: USD value to reduce
        - net_delta: Current net delta (positive = net long)
    """
    leg1_px = (
        leg1_mark_price
        if leg1_mark_price and leg1_mark_price > 0
        else trade.leg1.entry_price
    )
    leg2_px = (
        leg2_mark_price
        if leg2_mark_price and leg2_mark_price > 0
        else trade.leg2.entry_price
    )

    leg1_notional = trade.leg1.filled_qty * leg1_px
    leg2_notional = trade.leg2.filled_qty * leg2_px

    leg1_delta = leg1_notional if trade.leg1.side == Side.BUY else -leg1_notional
    leg2_delta = leg2_notional if trade.leg2.side == Side.BUY else -leg2_notional

    net_delta = leg1_delta + leg2_delta

    if net_delta > 0:
        if leg1_delta > 0 and leg2_delta < 0:
            return Exchange.LIGHTER, trade.leg1, abs(net_delta), net_delta
        else:
            return Exchange.X10, trade.leg2, abs(net_delta), net_delta
    else:
        if leg1_delta < 0 and leg2_delta > 0:
            return Exchange.LIGHTER, trade.leg1, abs(net_delta), net_delta
        else:
            return Exchange.X10, trade.leg2, abs(net_delta), net_delta


def get_rebalance_price(
    market_data: "MarketDataService",
    trade: Trade,
    rebalance_exchange: Exchange,
    rebalance_side: Side,
) -> Decimal | None:
    """Get the appropriate price for a rebalance order."""
    price_data = market_data.get_price(trade.symbol)
    ob = market_data.get_orderbook(trade.symbol)

    if rebalance_exchange == Exchange.LIGHTER:
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            return ob.lighter_bid if rebalance_side == Side.BUY else ob.lighter_ask
        elif price_data and price_data.lighter_price > 0:
            return price_data.lighter_price
    else:
        if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
            return ob.x10_bid if rebalance_side == Side.BUY else ob.x10_ask
        elif price_data and price_data.x10_price > 0:
            return price_data.x10_price

    return None


async def execute_rebalance_maker(
    trade: Trade,
    adapter: "ExchangePort",
    rebalance_exchange: Exchange,
    rebalance_side: Side,
    rebalance_qty: Decimal,
    order_price: Decimal,
    maker_timeout: float,
) -> tuple[Order | None, Decimal]:
    """
    Execute maker order for rebalance with timeout.

    Returns:
        (final_order_state, remaining_qty)
    """
    maker_request = OrderRequest(
        symbol=trade.symbol,
        exchange=rebalance_exchange,
        side=rebalance_side,
        qty=rebalance_qty,
        price=order_price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=True,
    )

    maker_order = await adapter.place_order(maker_request)

    if not maker_order:
        return None, rebalance_qty

    log_rebalance_order_placed(
        trade=trade,
        exchange=rebalance_exchange,
        order_id=str(maker_order.order_id),
        side=rebalance_side,
        qty=rebalance_qty,
        price=order_price,
        time_in_force=TimeInForce.POST_ONLY,
        post_only=True,
    )

    logger.info(
        f"Rebalance maker order placed: {maker_order.order_id} "
        f"on {rebalance_exchange.value}"
    )

    # Wait for fill with timeout
    start_time = time.time()
    updated = None

    while time.time() - start_time < maker_timeout:
        await asyncio.sleep(0.5)
        updated = await adapter.get_order(trade.symbol, maker_order.order_id)
        if updated:
            if updated.is_filled or updated.status == OrderStatus.FILLED:
                return updated, Decimal("0")
            if not updated.is_active:
                break

    # Cancel maker if not filled
    if maker_order and (not updated or updated.is_active):
        try:
            logger.info(
                f"Cancelling maker order {maker_order.order_id} before IOC fallback"
            )
            await adapter.cancel_order(maker_order.order_id, trade.symbol)
        except Exception as e:
            logger.warning(f"Failed to cancel maker order {maker_order.order_id}: {e}")

    # Calculate remaining qty
    remaining_qty = rebalance_qty
    if updated and updated.filled_qty > 0:
        remaining_qty = max(Decimal("0"), rebalance_qty - updated.filled_qty)

    return updated, remaining_qty


async def execute_rebalance_ioc(
    market_data: "MarketDataService",
    trade: Trade,
    adapter: "ExchangePort",
    rebalance_exchange: Exchange,
    rebalance_side: Side,
    remaining_qty: Decimal,
    tick: Decimal,
) -> Order | None:
    """Execute IOC fallback for rebalance."""
    ob = market_data.get_orderbook(trade.symbol)
    price_data = market_data.get_price(trade.symbol)

    # Get aggressive price for IOC
    fallback_lighter = price_data.lighter_price if price_data else Decimal("0")
    fallback_x10 = price_data.x10_price if price_data else Decimal("0")

    if rebalance_side == Side.BUY:
        if rebalance_exchange == Exchange.LIGHTER:
            base_price = (
                ob.lighter_ask if ob and ob.lighter_ask > 0 else fallback_lighter
            )
        else:
            base_price = ob.x10_ask if ob and ob.x10_ask > 0 else fallback_x10
    else:
        if rebalance_exchange == Exchange.LIGHTER:
            base_price = (
                ob.lighter_bid if ob and ob.lighter_bid > 0 else fallback_lighter
            )
        else:
            base_price = ob.x10_bid if ob and ob.x10_bid > 0 else fallback_x10

    if base_price <= 0:
        return None

    ioc_price = _round_price_to_tick(base_price, tick, rounding="down")

    ioc_request = OrderRequest(
        symbol=trade.symbol,
        exchange=rebalance_exchange,
        side=rebalance_side,
        qty=remaining_qty,
        price=ioc_price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )

    ioc_order = await adapter.place_order(ioc_request)

    if ioc_order:
        log_rebalance_order_placed(
            trade=trade,
            exchange=rebalance_exchange,
            order_id=str(ioc_order.order_id),
            side=rebalance_side,
            qty=remaining_qty,
            price=ioc_price,
            time_in_force=TimeInForce.IOC,
            post_only=False,
        )

    return ioc_order


def update_leg_after_fill(
    rebalance_leg: TradeLeg,
    filled_qty: Decimal,
    fee: Decimal,
) -> None:
    """
    Update leg state after a fill.

    Reduces the leg's filled_qty and accumulates fees.
    Ensures filled_qty never goes negative.

    Args:
        rebalance_leg: The TradeLeg to update.
        filled_qty: Quantity that was filled (to subtract).
        fee: Trading fee to accumulate.
    """
    rebalance_leg.filled_qty = max(Decimal("0"), rebalance_leg.filled_qty - filled_qty)
    rebalance_leg.fees += fee


async def rebalance_trade(
    manager,  # PositionManager instance (self)
    trade: Trade,
    leg1_mark_price: Decimal | None = None,
    leg2_mark_price: Decimal | None = None,
) -> None:
    """
    Restore delta neutrality without full position close.

    Orchestrates: delta calc → maker order → IOC fallback → state update.

    Args:
        manager: The PositionManager instance
        trade: The active trade with delta drift
        leg1_mark_price: Current mark price for leg1 (Lighter)
        leg2_mark_price: Current mark price for leg2 (X10)

    NOTE: This IS a reduce-only operation - it closes part of the larger leg.
    """
    ts = manager.settings.trading
    maker_timeout = float(getattr(ts, "rebalance_maker_timeout_seconds", 6.0))
    use_ioc_fallback = bool(getattr(ts, "rebalance_use_ioc_fallback", True))

    # 1. Calculate rebalance target
    rebalance_exchange, rebalance_leg, rebalance_notional, net_delta = (
        calculate_rebalance_target(trade, leg1_mark_price, leg2_mark_price)
    )

    # Calculate notionals for logging
    leg1_px = (
        leg1_mark_price
        if leg1_mark_price and leg1_mark_price > 0
        else trade.leg1.entry_price
    )
    leg2_px = (
        leg2_mark_price
        if leg2_mark_price and leg2_mark_price > 0
        else trade.leg2.entry_price
    )
    leg1_notional = trade.leg1.filled_qty * leg1_px
    leg2_notional = trade.leg2.filled_qty * leg2_px

    min_notional_usd = safe_decimal(
        getattr(ts, "rebalance_min_notional_usd", None), Decimal("0")
    )
    min_notional_pct = safe_decimal(
        getattr(ts, "rebalance_min_notional_pct", None), Decimal("0")
    )
    notional_floor = max(
        min_notional_usd,
        (max(leg1_notional, leg2_notional) * min_notional_pct)
        if min_notional_pct > 0
        else Decimal("0"),
    )

    if rebalance_notional < notional_floor:
        if trade.symbol not in _rebalance_skip_warned:
            _rebalance_skip_warned.add(trade.symbol)
            logger.info(
                f"Rebalance skipped for {trade.symbol}: "
                f"notional=${rebalance_notional:.2f} < min=${notional_floor:.2f}"
            )
        return

    log_rebalance_start(
        trade=trade,
        net_delta=net_delta,
        leg1_notional=leg1_notional,
        leg2_notional=leg2_notional,
        rebalance_exchange=rebalance_exchange,
        rebalance_notional=rebalance_notional,
    )

    # 2. Calculate rebalancing quantity
    rebalance_px = leg1_px if rebalance_exchange == Exchange.LIGHTER else leg2_px
    if rebalance_px <= 0:
        logger.warning(
            f"Cannot rebalance {trade.symbol}: invalid price for {rebalance_exchange.value}"
        )
        return

    rebalance_qty = rebalance_notional / rebalance_px
    adapter = manager.lighter if rebalance_exchange == Exchange.LIGHTER else manager.x10
    rebalance_side = rebalance_leg.side.inverse()

    # 3. Get price and tick
    market_info = manager.market_data.get_market_info(trade.symbol, rebalance_exchange)
    tick = market_info.tick_size if market_info else Decimal("0.01")

    base_price = get_rebalance_price(
        manager.market_data, trade, rebalance_exchange, rebalance_side
    )
    if not base_price:
        logger.warning(
            f"Cannot rebalance {trade.symbol}: no price data for {rebalance_exchange.value}"
        )
        return

    order_price = _round_price_to_tick(base_price, tick, rounding="down")

    logger.info(
        f"Rebalancing {trade.symbol}: {rebalance_side.value} {rebalance_qty:.6f} "
        f"@ {order_price:.2f} on {rebalance_exchange.value} "
        f"(net_delta={net_delta:.2f}, rebalance_notional=${rebalance_notional:.2f})"
    )

    try:
        # 4. Execute maker order
        updated, remaining_qty = await execute_rebalance_maker(
            trade,
            adapter,
            rebalance_exchange,
            rebalance_side,
            rebalance_qty,
            order_price,
            maker_timeout,
        )

        # Handle maker fill
        if updated and updated.is_filled:
            logger.info(
                f"Rebalance maker filled: {updated.filled_qty}/{rebalance_qty} "
                f"@ {updated.avg_fill_price:.2f} on {rebalance_exchange.value}"
            )
            update_leg_after_fill(rebalance_leg, updated.filled_qty, updated.fee)
            await manager._update_trade(trade)
            log_rebalance_complete(
                trade, updated.filled_qty, updated.avg_fill_price, updated.fee
            )
            return

        # Handle partial fill
        if updated and updated.filled_qty > 0:
            logger.info(
                f"Maker partially filled: {updated.filled_qty}/{rebalance_qty}, "
                f"IOC qty: {remaining_qty}"
            )
            update_leg_after_fill(rebalance_leg, updated.filled_qty, updated.fee)
            await manager._update_trade(trade)

        # 5. IOC fallback
        if use_ioc_fallback and remaining_qty > 0:
            logger.info(
                f"Rebalance maker not filled within {maker_timeout}s, trying IOC fallback"
            )

            ioc_order = await execute_rebalance_ioc(
                manager.market_data,
                trade,
                adapter,
                rebalance_exchange,
                rebalance_side,
                remaining_qty,
                tick,
            )

            if ioc_order and ioc_order.is_filled:
                logger.info(
                    f"Rebalance IOC filled: {ioc_order.filled_qty}/{remaining_qty} "
                    f"@ {ioc_order.avg_fill_price:.2f} on {rebalance_exchange.value}"
                )
                update_leg_after_fill(
                    rebalance_leg, ioc_order.filled_qty, ioc_order.fee
                )
                await manager._update_trade(trade)
                log_rebalance_complete(
                    trade,
                    ioc_order.filled_qty,
                    ioc_order.avg_fill_price,
                    ioc_order.fee,
                )
            elif ioc_order and not ioc_order.is_filled:
                logger.warning(
                    f"Rebalance IOC not filled: {ioc_order.order_id} "
                    f"on {rebalance_exchange.value}"
                )
            else:
                logger.warning(f"Rebalance IOC failed on {rebalance_exchange.value}")

    except Exception as e:
        logger.warning(f"Rebalance failed for {trade.symbol}: {e}")
