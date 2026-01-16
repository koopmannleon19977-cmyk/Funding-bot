"""
Pricing logic for leg1 maker attempts.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from funding_bot.domain.models import Side, TimeInForce, Trade
from funding_bot.services.execution_leg1_types import Leg1AttemptContext, Leg1Config, Leg1State


def _compute_leg1_pricing(
    self,
    trade: Trade,
    config: Leg1Config,
    state: Leg1State,
    attempt_index: int,
) -> tuple[Leg1AttemptContext | None, object | None]:
    remaining_qty = trade.target_qty - state.total_filled

    # Calculate aggressiveness (0.0 to 1.0)
    aggressiveness = (
        Decimal(attempt_index) / Decimal(config.max_attempts - 1)
        if config.max_attempts > 1
        else Decimal("0")
    )

    # 1. Get latest market data
    price_data = self.market_data.get_price(trade.symbol)
    if not price_data:
        return None, None

    lighter_info = self.market_data.get_market_info(
        trade.symbol, trade.leg1.exchange
    )
    tick = lighter_info.tick_size if lighter_info else Decimal("0.01")

    ob = None
    if hasattr(self.market_data, "get_orderbook"):
        ob = self.market_data.get_orderbook(trade.symbol)

    best_bid = (
        ob.lighter_bid
        if ob and ob.lighter_bid > 0
        else price_data.lighter_price * Decimal("0.9999")
    )

    best_ask = (
        ob.lighter_ask
        if ob and ob.lighter_ask > 0
        else price_data.lighter_price * Decimal("1.0001")
    )

    if best_bid >= best_ask:
        best_bid = price_data.lighter_price * Decimal("0.9999")
        best_ask = price_data.lighter_price * Decimal("1.0001")

    # Calculate Price
    # MAX_AGGRESSIVENESS: Limit how far we chase the price
    # 0.5 = max 50% of spread, prevents market-taking behavior
    max_aggressiveness = config.max_aggr_setting

    # Smart Pricing (maker): if the top-of-book is too thin for our remaining size,
    # start more aggressive earlier (still capped inside the spread).
    smart_aggr_floor = Decimal("0")
    l1_qty = Decimal("0")
    l1_util: Decimal | None = None
    if config.smart_enabled and config.smart_maker_floor > 0 and ob is not None:
        with contextlib.suppress(Exception):
            l1_qty = (
                ob.lighter_bid_qty
                if trade.leg1.side == Side.BUY
                else ob.lighter_ask_qty
            )
        l1_util = remaining_qty / l1_qty if l1_qty > 0 else Decimal("1e18")

        if l1_util > config.smart_l1_util_trigger:
            scale = (l1_util - config.smart_l1_util_trigger) / Decimal("2.0")
            if scale < 0:
                scale = Decimal("0")
            if scale > 1:
                scale = Decimal("1")
            smart_aggr_floor = config.smart_maker_floor + (Decimal("1") - config.smart_maker_floor) * scale
            if smart_aggr_floor < 0:
                smart_aggr_floor = Decimal("0")
            if smart_aggr_floor > 1:
                smart_aggr_floor = Decimal("1")
            aggressiveness = max(aggressiveness, smart_aggr_floor)

    capped_aggr = min(aggressiveness, max_aggressiveness)

    if trade.leg1.side == Side.BUY:
        start_price = best_bid
        # Removed: "If final attempt, cross the spread" - this caused market-taking!
        # Now we always use capped aggressiveness to prevent paying full spread
        target_max = best_ask - tick
        if start_price >= target_max:
            price = start_price
        else:
            gap = target_max - start_price
            raw_price = start_price + (gap * capped_aggr)
            # CEILING to tick size (higher price for buy) is more aggressive
            price = (raw_price / tick).quantize(
                Decimal("1"), rounding="ROUND_CEILING"
            ) * tick
    else:
        start_price = best_ask
        # Removed: "If final attempt, cross the spread" - this caused market-taking!
        # Now we always use capped aggressiveness to prevent paying full spread
        target_min = best_bid + tick
        if start_price <= target_min:
            price = start_price
        else:
            gap_distance = start_price - target_min
            raw_adj = gap_distance * capped_aggr
            raw_price = start_price - raw_adj
            # FLOOR to tick size (lower price for sell) is more aggressive
            price = (raw_price / tick).quantize(
                Decimal("1"), rounding="ROUND_FLOOR"
            ) * tick

    is_final_attempt = (attempt_index == config.max_attempts - 1)
    # Default: final attempt uses GTC, earlier attempts use POST_ONLY.
    # If maker_force_post_only=true, always use POST_ONLY (taker prevention).
    if config.force_post_only_setting:
        time_in_force = TimeInForce.POST_ONLY
    else:
        time_in_force = TimeInForce.GTC if is_final_attempt else TimeInForce.POST_ONLY

    ctx = Leg1AttemptContext(
        attempt_index=attempt_index,
        is_final_attempt=is_final_attempt,
        remaining_qty=remaining_qty,
        aggressiveness=aggressiveness,
        capped_aggr=capped_aggr,
        tick=tick,
        best_bid=best_bid,
        best_ask=best_ask,
        price=price,
        time_in_force=time_in_force,
        smart_aggr_floor=smart_aggr_floor,
        l1_qty=l1_qty,
        l1_util=l1_util,
    )
    return ctx, price_data
