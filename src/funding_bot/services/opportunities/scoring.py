"""
Scoring and EV calculations for opportunities.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.services.market_data import FundingSnapshot, OrderbookSnapshot, PriceSnapshot

# =============================================================================
# PHASE 4: VELOCITY CALCULATION (Slope-based APY adjustment)
# =============================================================================


def _calculate_funding_velocity(
    historical_apy: list[Decimal] | None,
    lookback_hours: int = 6,
) -> Decimal:
    """
    Calculate funding rate velocity (slope) via linear regression.

    Returns the hourly change in APY (slope).
    - Positive: Funding is increasing (bullish for the trade)
    - Negative: Funding is decreasing (bearish for the trade)

    Args:
        historical_apy: List of historical APY values (oldest first, ASC chronological)
        lookback_hours: Number of hours to look back

    Returns:
        Hourly velocity (change in APY per hour)
        Returns 0 if insufficient data (< 3 points)
    """
    if not historical_apy or len(historical_apy) < 3:
        return Decimal("0")

    # Take last N hours
    rates = list(historical_apy[-lookback_hours:])
    n = len(rates)

    if n < 3:
        return Decimal("0")

    # Linear regression: y = mx + b, we want m (slope)
    # x values: 0, 1, 2, ..., n-1 (hours ago)
    x_mean = Decimal(n - 1) / 2
    y_mean = sum(rates) / n

    numerator = sum((Decimal(i) - x_mean) * (rates[i] - y_mean) for i in range(n))
    denominator = sum((Decimal(i) - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return Decimal("0")

    return numerator / denominator


def _get_maker_fill_probability(
    trading_settings,
    exchange: str = "lighter",
    account_type: str = "premium",
) -> Decimal:
    """
    Get exchange-specific maker fill probability.

    Args:
        trading_settings: Trading settings object
        exchange: "lighter" or "x10"
        account_type: "standard" or "premium" (for Lighter)

    Returns:
        Maker fill probability (0-1)
    """
    # Get probability dict from settings
    prob_dict = getattr(trading_settings, "maker_fill_probability", {})

    # Determine key based on exchange and account type
    if exchange == "lighter":
        key = f"lighter_{account_type}"
    elif exchange == "x10":
        key = "x10"
    else:
        # Default fallback
        return Decimal("0.70")

    # Get probability from dict, or use default
    prob_str = prob_dict.get(key, "0.70")
    return Decimal(prob_str)


def _calculate_expected_value(
    self,
    symbol: str,
    funding: FundingSnapshot,
    notional: Decimal,
    orderbook: OrderbookSnapshot | None,
    fees: dict[str, Decimal],
    price_snapshot: PriceSnapshot | None = None,
    historical_apy: list[Decimal] | None = None,
) -> dict:
    """
    Calculate expected value including fees and slippage.

    NEW PHASE 4 FEATURES:
    - Uses weighted exit cost based on maker fill probability
    - Applies velocity forecast to adjust expected funding rate

    Returns:
        Dict with ev_per_hour, entry_cost, total_ev (for default hold period)
    """
    ts = self.settings.trading

    # === PHASE 4: VELOCITY FORECAST (Slope-based APY adjustment) ===
    # Calculate funding velocity (slope) and adjust expected funding rate
    # IMPORTANT: Work in APY basis to avoid unit mismatch, then convert to hourly
    #
    # Formula:
    #   base_apy = funding.apy (annualized rate)
    #   velocity = Δ(APY)/h from historical data (APY points per hour)
    #   adjusted_apy = base_apy + (velocity * weight)
    #   adjusted_net_rate = adjusted_apy / 8760 (convert back to hourly)
    #
    # This prevents adding APY/h velocity directly to hourly rate (unit mismatch bug).
    base_apy = funding.apy

    velocity_enabled = getattr(ts, "velocity_forecast_enabled", False)
    velocity_adjustment_hourly = Decimal("0")  # Track for logging/debugging

    if velocity_enabled and historical_apy:
        lookback = getattr(ts, "velocity_forecast_lookback_hours", 6)
        velocity_weight = getattr(ts, "velocity_forecast_weight", Decimal("2.0"))

        velocity = _calculate_funding_velocity(historical_apy, lookback)

        # Apply velocity adjustment in APY basis: adjusted_apy = base_apy + (velocity * weight)
        # Positive velocity (increasing funding) = boost
        # Negative velocity (decreasing funding) = penalty
        velocity_adjustment_apy = velocity * velocity_weight
        adjusted_apy = base_apy + velocity_adjustment_apy

        # Clamp: Don't let velocity make a negative rate positive or vice versa
        # This prevents false signals from noise in shallow historical data
        adjusted_apy = max(Decimal("0"), adjusted_apy) if base_apy > 0 else min(Decimal("0"), adjusted_apy)

        # Convert APY back to hourly rate (divide by hours per year)
        HOURS_PER_YEAR = Decimal("8760")
        adjusted_net_rate = adjusted_apy / HOURS_PER_YEAR

        # Track velocity adjustment in hourly units for logging
        velocity_adjustment_hourly = velocity_adjustment_apy / HOURS_PER_YEAR
    else:
        adjusted_net_rate = base_apy / Decimal("8760")

    # Fees (both legs)
    # Lighter maker (free usually, possible rebate), X10 taker
    # We use the fees passed from adapters which know if we are premium/etc.

    # Entry: Leg 1 Maker (Lighter) + Leg 2 Taker (X10)
    entry_fees = (notional * fees["lighter_maker"]) + (notional * fees["x10_taker"])

    # === PHASE 4: WEIGHTED EXIT COST (more accurate than conservative taker/taker) ===
    # Exit: Weighted maker + IOC fallback based on fill probability
    # Lighter: p*maker + (1-p)*taker
    # X10: If coordinated_close_enabled with x10_use_maker, p*maker + (1-p)*taker, else pure taker

    # Get exchange-specific maker fill probabilities (Lighter Premium assumed)
    # TODO: Pass account_type from adapter when available
    p_maker_lighter = _get_maker_fill_probability(ts, exchange="lighter", account_type="premium")
    p_ioc_lighter = Decimal("1") - p_maker_lighter

    p_maker_x10 = _get_maker_fill_probability(ts, exchange="x10")
    p_ioc_x10 = Decimal("1") - p_maker_x10

    # Check if X10 can use maker orders (coordinated_close_x10_use_maker)
    x10_can_make_maker = getattr(ts, "coordinated_close_x10_use_maker", True)
    x10_maker_fee = fees.get("x10_maker", Decimal("0"))

    # Lighter exit: weighted maker + IOC fallback (exchange-specific probability)
    lighter_exit = notional * (p_maker_lighter * fees["lighter_maker"] + p_ioc_lighter * fees["lighter_taker"])

    # X10 exit: depends on coordinated close mode
    if x10_can_make_maker and x10_maker_fee == Decimal("0"):
        # X10 has free maker orders - use weighted (exchange-specific probability)
        x10_exit = notional * (p_maker_x10 * x10_maker_fee + p_ioc_x10 * fees["x10_taker"])
    else:
        # X10 pure taker (no maker option)
        x10_exit = notional * fees["x10_taker"]

    exit_fees = lighter_exit + x10_exit

    total_fees = entry_fees + exit_fees

    # Slippage/Spread (Corrected for Arbitrage Direction)
    # If we enter LongA + ShortB, and PriceA < PriceB, we capture the spread as PROFIT.
    # If PriceA > PriceB, we pay the spread as COST.

    # Determine intended direction based on funding (same logic as _evaluate_symbol)
    lighter_rate = funding.lighter_rate.rate_hourly if funding.lighter_rate else Decimal("0")
    x10_rate = funding.x10_rate.rate_hourly if funding.x10_rate else Decimal("0")

    spread_cost = Decimal("0")

    # USE BID/ASK FROM ORDERBOOK (Real Execution Prices)
    # Cost = (Buy Price Long Leg) - (Sell Price Short Leg)
    # If Buy < Sell, Cost is negative (Profit)
    p_long_exec = Decimal("0")
    p_short_exec = Decimal("0")

    if orderbook:
        if lighter_rate > x10_rate:
            # Strategy: Long X10, Short Lighter
            # Action: Buy X10 @ Ask, Sell Lighter @ Bid
            p_long_exec = orderbook.x10_ask
            p_short_exec = orderbook.lighter_bid
        else:
            # Strategy: Long Lighter, Short X10
            # Action: Buy Lighter @ Ask, Sell X10 @ Bid
            p_long_exec = orderbook.lighter_ask
            p_short_exec = orderbook.x10_bid

    if p_long_exec > 0 and p_short_exec > 0:
        # Precise Spread Cost Calculation
        # Cost = (Ask - Bid) * Qty
        price_diff = p_long_exec - p_short_exec
        # Use execution price for quantity calculation to be precise, or passed notional?
        # notional is mostly approximate USD value.
        # spread_cost approx = (Ask - Bid) * (Notional / Ask)
        spread_cost = (price_diff * notional) / p_long_exec
    else:
        # Fallback: Mark Prices (Only if orderbook missing, which filters should catch)
        if lighter_rate > x10_rate:
            p_long_mark = price_snapshot.x10_price if price_snapshot else Decimal("0")
            p_short_mark = price_snapshot.lighter_price if price_snapshot else Decimal("0")
        else:
            p_long_mark = price_snapshot.lighter_price if price_snapshot else Decimal("0")
            p_short_mark = price_snapshot.x10_price if price_snapshot else Decimal("0")

        if p_long_mark > 0 and p_short_mark > 0:
            price_diff = p_long_mark - p_short_mark
            spread_cost = (price_diff * notional) / p_long_mark
        else:
            # Last Resort Fallback
            spread_cost = notional * (ts.max_spread_filter_percent / Decimal("2"))

    entry_cost = total_fees + spread_cost

    # Funding income per hour (PHASE 4: Use velocity-adjusted rate)
    funding_per_hour = notional * adjusted_net_rate

    # EV horizon should respect the configured minimum hold time.
    # Otherwise EV ranking/filters can be inconsistent with exit rules.
    min_hold_hours = Decimal(str(ts.min_hold_seconds)) / Decimal("3600")
    hold_hours = max(Decimal("1"), min_hold_hours)
    if hold_hours > ts.max_hold_hours:
        hold_hours = ts.max_hold_hours
    total_funding = funding_per_hour * hold_hours

    total_ev = total_funding - entry_cost

    return {
        "entry_cost": entry_cost,
        "total_fees": total_fees,
        "slippage": spread_cost,
        "funding_per_hour": funding_per_hour,
        "ev_per_hour": funding_per_hour - (entry_cost / hold_hours),
        "hold_hours": hold_hours,
        "total_ev": total_ev,
        "velocity_adjustment": velocity_adjustment_hourly,
    }


def _calculate_breakeven_hours(
    self,
    ev_per_hour: Decimal,
    entry_cost: Decimal,
) -> Decimal:
    """Calculate hours to breakeven."""
    if ev_per_hour <= 0:
        return Decimal("999")  # Never breaks even

    return entry_cost / ev_per_hour


def _calculate_liquidity_score(
    self,
    orderbook: OrderbookSnapshot | None,
    qty: Decimal,
) -> Decimal:
    """
    Calculate liquidity score (0-1).

    Higher = more liquid relative to our order size.
    """
    if not orderbook:
        return Decimal("0.5")  # Unknown

    # Average available liquidity at L1
    avg_qty = (
        orderbook.lighter_bid_qty + orderbook.lighter_ask_qty + orderbook.x10_bid_qty + orderbook.x10_ask_qty
    ) / Decimal("4")

    if avg_qty == 0:
        return Decimal("0")

    # Score based on ratio of our size to available liquidity
    ratio = qty / avg_qty

    if ratio < Decimal("0.1"):
        return Decimal("1")  # Tiny order, excellent liquidity
    if ratio < Decimal("0.5"):
        return Decimal("0.8")
    if ratio < Decimal("1"):
        return Decimal("0.5")
    return Decimal("0.2")  # Order larger than L1, poor liquidity
