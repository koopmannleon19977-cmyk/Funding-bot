"""
Exit evaluation logic.

Refactored to extract helper functions for better maintainability.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import Position, Side, Trade
from funding_bot.domain.rules import (
    ExitDecision,
    ExitRulesContext,
    evaluate_exit_rules_from_context,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.market_data import OrderbookDepthSnapshot, OrderbookSnapshot
from funding_bot.services.positions.pnl import (
    _calculate_realizable_pnl,
    _calculate_realizable_pnl_depth,
    _calculate_unrealized_pnl,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger(__name__)

# Rate-limit warnings: only log once per symbol to avoid log spam
_lighter_liquidation_warned: set[str] = set()
_x10_liquidation_warned: set[str] = set()


# =============================================================================
# Data Classes for Exit Evaluation
# =============================================================================


@dataclass
class LiquidationData:
    """Liquidation monitoring data for both legs."""

    leg1_position: Position | None = None
    leg2_position: Position | None = None
    leg1_mark_price: Decimal = Decimal("0")
    leg2_mark_price: Decimal = Decimal("0")
    leg1_liq_distance_pct: Decimal = Decimal("-1")  # -1 = sentinel for missing
    leg2_liq_distance_pct: Decimal = Decimal("-1")


@dataclass
class PnLData:
    """PnL calculation results with orderbook context."""

    current_pnl: Decimal = Decimal("0")
    orderbook: OrderbookSnapshot | None = None
    orderbook_depth: OrderbookDepthSnapshot | None = None


@dataclass
class ExitCostEstimate:
    """Estimated costs for closing a position."""

    fee_cost: Decimal = Decimal("0")
    spread_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")


# =============================================================================
# Helper Functions for Liquidation Monitoring
# =============================================================================


async def _fetch_liquidation_data(
    self,
    trade: Trade,
    price_data,
    liq_monitoring_enabled: bool,
) -> LiquidationData:
    """
    Fetch liquidation data for both legs.

    This runs BEFORE the early return to ensure we track liquidation
    proximity even when market data is stale/unavailable.
    """
    result = LiquidationData()

    try:
        # Fetch live positions
        result.leg1_position = await self.lighter.get_position(trade.symbol)
        result.leg2_position = await self.x10.get_position(trade.symbol)

        # Get orderbook for mark price fallback
        ob = self.market_data.get_orderbook(trade.symbol)
        lighter_mid = _get_lighter_mid_price(ob, price_data.lighter_price if price_data else None)
        x10_mid = _get_x10_mid_price(ob, price_data.x10_price if price_data else None)

        # Calculate liquidation distance for Lighter leg
        if result.leg1_position:
            result.leg1_mark_price, result.leg1_liq_distance_pct = _calc_leg_liq_distance(
                result.leg1_position, lighter_mid, "Lighter", trade.symbol, liq_monitoring_enabled
            )

        # Calculate liquidation distance for X10 leg
        if result.leg2_position:
            result.leg2_mark_price, result.leg2_liq_distance_pct = _calc_leg_liq_distance(
                result.leg2_position, x10_mid, "X10", trade.symbol, liq_monitoring_enabled
            )

    except Exception as e:
        logger.warning(f"Failed to fetch position data for liquidation monitoring: {e}")

    return result


def _calc_leg_liq_distance(
    position: Position,
    mid_price: Decimal,
    exchange_name: str,
    symbol: str,
    log_warning: bool,
) -> tuple[Decimal, Decimal]:
    """
    Calculate liquidation distance for a single leg.

    Returns (mark_price, liq_distance_pct).
    """
    warned_set = _lighter_liquidation_warned if exchange_name == "Lighter" else _x10_liquidation_warned

    if not position.liquidation_price or position.liquidation_price <= 0:
        if log_warning and symbol not in warned_set:
            warned_set.add(symbol)
            logger.warning(f"{exchange_name} liquidation_price missing for {symbol}")
        return Decimal("0"), Decimal("-1")

    # Use mark_price from position, fallback to mid-price
    mark_px = position.mark_price if position.mark_price > 0 else mid_price

    if mark_px > 0:
        liq_distance = abs(mark_px - position.liquidation_price) / mark_px
        return mark_px, liq_distance
    elif mid_price > 0:
        liq_distance = abs(mid_price - position.liquidation_price) / mid_price
        return mid_price, liq_distance

    return Decimal("0"), Decimal("-1")


def _log_liquidation_metrics(
    trade: Trade,
    liq_data: LiquidationData,
    liq_monitoring_enabled: bool,
    delta_bound_enabled: bool,
) -> None:
    """Log liquidation distances and delta imbalance for monitoring."""
    try:
        from funding_bot.services.positions.imbalance import log_delta_imbalance

        if liq_monitoring_enabled:
            if liq_data.leg1_liq_distance_pct >= 0:
                logger.info(
                    f"Trade {trade.symbol}: Lighter liquidation distance = {liq_data.leg1_liq_distance_pct:.2%}"
                )
            if liq_data.leg2_liq_distance_pct >= 0:
                logger.info(
                    f"Trade {trade.symbol}: X10 liquidation distance = {liq_data.leg2_liq_distance_pct:.2%}"
                )

        if delta_bound_enabled and liq_data.leg1_position and liq_data.leg2_position:
            log_delta_imbalance(
                trade=trade,
                leg1_position=liq_data.leg1_position,
                leg2_position=liq_data.leg2_position,
                leg1_mark_price=liq_data.leg1_mark_price,
                leg2_mark_price=liq_data.leg2_mark_price,
            )
    except Exception as e:
        logger.warning(f"Failed to log liquidation/delta metrics: {e}")


# =============================================================================
# Helper Functions for PnL Calculation
# =============================================================================


async def _fetch_pnl_with_orderbook(
    self,
    trade: Trade,
    price_data,
    early_tp_enabled: bool,
    settings,
) -> PnLData:
    """
    Fetch PnL with appropriate orderbook data.

    For early_tp: uses fresh depth data (20 levels) for accurate exit costs.
    Otherwise: uses cached L1 data.
    """
    result = PnLData()

    if early_tp_enabled:
        result = await _fetch_fresh_pnl_for_early_tp(self, trade, price_data, settings)
    else:
        # Normal loop - use cached L1
        result.orderbook = self.market_data.get_orderbook(trade.symbol)
        if result.orderbook and result.orderbook.lighter_bid > 0 and result.orderbook.x10_bid > 0:
            result.current_pnl = await _calculate_realizable_pnl(self, trade, result.orderbook)
        else:
            result.current_pnl = await _calculate_unrealized_pnl(self, trade, price_data.mid_price)

    return result


async def _fetch_fresh_pnl_for_early_tp(
    self,
    trade: Trade,
    price_data,
    settings,
) -> PnLData:
    """Fetch fresh orderbook depth and calculate PnL for early take-profit."""
    result = PnLData()

    try:
        # Use DEPTH (20 levels) to account for slippage
        result.orderbook_depth = await self.market_data.get_fresh_orderbook_depth(trade.symbol, levels=20)

        # Freshness check
        now = datetime.now(UTC)
        max_age = float(getattr(settings.websocket, "orderbook_l1_fallback_max_age_seconds", 5.0))
        max_age = max(0.5, min(max_age, 60.0))

        lighter_age = (
            (now - result.orderbook_depth.lighter_updated).total_seconds()
            if result.orderbook_depth.lighter_updated else 999.0
        )
        x10_age = (
            (now - result.orderbook_depth.x10_updated).total_seconds()
            if result.orderbook_depth.x10_updated else 999.0
        )

        if lighter_age > max_age or x10_age > max_age:
            logger.warning(
                f"Orderbook depth too stale for {trade.symbol} early-TP: "
                f"lighter_age={lighter_age:.1f}s, x10_age={x10_age:.1f}s (max={max_age}s). "
                "Falling back to L1."
            )
            raise ValueError("Stale depth data")

        result.current_pnl = await _calculate_realizable_pnl_depth(self, trade, result.orderbook_depth)

        # Build L1 snapshot from depth top-of-book
        result.orderbook = _build_l1_from_depth(result.orderbook_depth, trade.symbol)

    except Exception as e:
        logger.warning(f"Fresh depth fetch failed for {trade.symbol}, falling back to L1: {e}")
        result = await _fetch_l1_fallback(self, trade, price_data)

    return result


def _build_l1_from_depth(depth: OrderbookDepthSnapshot, symbol: str) -> OrderbookSnapshot:
    """Build L1 snapshot from depth top-of-book."""
    lighter_bid, lighter_bid_qty = depth.lighter_bids[0] if depth.lighter_bids else (Decimal("0"), Decimal("0"))
    lighter_ask, lighter_ask_qty = depth.lighter_asks[0] if depth.lighter_asks else (Decimal("0"), Decimal("0"))
    x10_bid, x10_bid_qty = depth.x10_bids[0] if depth.x10_bids else (Decimal("0"), Decimal("0"))
    x10_ask, x10_ask_qty = depth.x10_asks[0] if depth.x10_asks else (Decimal("0"), Decimal("0"))

    return OrderbookSnapshot(
        symbol=symbol,
        lighter_bid=lighter_bid,
        lighter_ask=lighter_ask,
        x10_bid=x10_bid,
        x10_ask=x10_ask,
        lighter_bid_qty=lighter_bid_qty,
        lighter_ask_qty=lighter_ask_qty,
        x10_bid_qty=x10_bid_qty,
        x10_ask_qty=x10_ask_qty,
        lighter_updated=depth.lighter_updated,
        x10_updated=depth.x10_updated,
    )


async def _fetch_l1_fallback(self, trade: Trade, price_data) -> PnLData:
    """Fallback to L1 when depth fetch fails."""
    result = PnLData()

    try:
        result.orderbook = await self.market_data.get_fresh_orderbook(trade.symbol)
        result.current_pnl = await _calculate_realizable_pnl(self, trade, result.orderbook)
    except Exception as ex:
        logger.warning(f"Fresh L1 fetch failed for {trade.symbol}, using cache: {ex}")
        result.orderbook = self.market_data.get_orderbook(trade.symbol)
        if result.orderbook and result.orderbook.lighter_bid > 0 and result.orderbook.x10_bid > 0:
            result.current_pnl = await _calculate_realizable_pnl(self, trade, result.orderbook)
        else:
            result.current_pnl = await _calculate_unrealized_pnl(self, trade, price_data.mid_price)

    return result


# =============================================================================
# Helper Functions for Exit Cost Calculation
# =============================================================================


def _calculate_exit_costs(
    self,
    trade: Trade,
    price_data,
    orderbook: OrderbookSnapshot | None,
) -> ExitCostEstimate:
    """Calculate estimated exit costs (fees + spread)."""
    result = ExitCostEstimate()

    try:
        fees = self.market_data.get_fee_schedule(trade.symbol)
        price_proxy = price_data.lighter_price if price_data.lighter_price > 0 else price_data.mid_price

        if price_proxy <= 0:
            return result

        # Venue-aware fee calculation
        leg1_value = trade.leg1.filled_qty * price_proxy
        leg2_value = trade.leg2.filled_qty * price_proxy

        leg1_taker_fee = fees.get("lighter_taker", fees.get("taker_fee_lighter", Decimal("0.0002")))
        leg2_taker_fee = fees.get("x10_taker", fees.get("taker_fee_x10", Decimal("0.000225")))

        result.fee_cost = (leg1_value * leg1_taker_fee) + (leg2_value * leg2_taker_fee)

        # Spread cost
        ob = orderbook or self.market_data.get_orderbook(trade.symbol)
        if ob and ob.lighter_bid > 0 and ob.x10_bid > 0:
            l_spread = ob.lighter_ask - ob.lighter_bid
            x_spread = ob.x10_ask - ob.x10_bid
            result.spread_cost = ((l_spread / 2) + (x_spread / 2)) * trade.leg1.filled_qty

        result.total_cost = result.fee_cost + result.spread_cost

    except Exception as e:
        logger.warning(f"Failed to estimate exit costs for {trade.symbol}: {e}")

    return result


def _calculate_early_tp_execution_buffer(
    trade: Trade,
    orderbook: OrderbookSnapshot | None,
    settings,
) -> Decimal:
    """Calculate extra buffer for early-TP execution."""
    if not orderbook:
        return Decimal("0")

    ts = settings.trading
    early_tp_enabled = getattr(ts, "early_take_profit_enabled", False)
    if not early_tp_enabled:
        return Decimal("0")

    fast_close_enabled = bool(getattr(ts, "early_tp_fast_close_enabled", True))
    taker_direct = bool(getattr(ts, "early_tp_fast_close_use_taker_directly", False))

    if not (fast_close_enabled and taker_direct):
        return Decimal("0")

    es = getattr(settings, "execution", None)
    x10_slippage = _safe_decimal(getattr(es, "x10_close_slippage", None), Decimal("0"))
    lighter_slippage = _safe_decimal(getattr(es, "leg1_escalate_to_taker_slippage", None), Decimal("0"))
    x10_slippage = max(Decimal("0"), x10_slippage)
    lighter_slippage = max(Decimal("0"), lighter_slippage)

    l_close_side = trade.leg1.side.inverse()
    x_close_side = trade.leg2.side.inverse()
    l_px = orderbook.lighter_ask if l_close_side == Side.BUY else orderbook.lighter_bid
    x_px = orderbook.x10_ask if x_close_side == Side.BUY else orderbook.x10_bid

    if l_px > 0 and x_px > 0 and trade.leg1.filled_qty > 0 and trade.leg2.filled_qty > 0:
        return (trade.leg1.filled_qty * l_px * lighter_slippage) + (trade.leg2.filled_qty * x_px * x10_slippage)

    return Decimal("0")


# =============================================================================
# Helper Functions for Funding Calculation
# =============================================================================


def _calculate_accrued_funding(
    trade: Trade,
    lighter_rate: Decimal,
    x10_rate: Decimal,
) -> Decimal:
    """Calculate accrued funding since last update."""
    if not trade.last_funding_update:
        return Decimal("0")

    now = datetime.now(UTC)
    delta = now - trade.last_funding_update
    hours_since_last = Decimal(str(delta.total_seconds() / 3600))

    # Net rate based on direction
    net_rate = x10_rate - lighter_rate if trade.leg1.side == Side.BUY else lighter_rate - x10_rate

    return net_rate * trade.target_notional_usd * hours_since_last


# =============================================================================
# Persistence Helpers
# =============================================================================


async def _persist_trade_snapshot(
    self,
    trade: Trade,
    funding_data,
    lighter_rate: Decimal,
    x10_rate: Decimal,
) -> None:
    """Persist latest metrics for monitoring (throttled)."""
    now = datetime.now(UTC)
    trade.current_apy = funding_data.apy
    trade.current_lighter_rate = lighter_rate
    trade.current_x10_rate = x10_rate
    trade.last_eval_at = now

    last_persist = self._trade_snapshot_last.get(trade.trade_id)
    should_persist = last_persist is None or (now - last_persist).total_seconds() >= 60

    if should_persist:
        self._trade_snapshot_last[trade.trade_id] = now
        with contextlib.suppress(Exception):
            await self.store.update_trade(
                trade_id=trade.trade_id,
                updates={
                    "current_apy": trade.current_apy,
                    "current_lighter_rate": trade.current_lighter_rate,
                    "current_x10_rate": trade.current_x10_rate,
                    "last_eval_at": trade.last_eval_at,
                },
            )


async def _update_high_water_mark(self, trade: Trade, total_pnl: Decimal) -> None:
    """Update high water mark if PnL exceeds previous."""
    if total_pnl > trade.high_water_mark:
        trade.high_water_mark = total_pnl
        with contextlib.suppress(Exception):
            await self.store.update_trade(
                trade_id=trade.trade_id,
                updates={"high_water_mark": trade.high_water_mark},
            )


# =============================================================================
# Context Building Helper
# =============================================================================


def _build_exit_rules_context(
    trade: Trade,
    ts,
    total_pnl: Decimal,
    price_pnl: Decimal,
    funding_data,
    lighter_rate: Decimal,
    x10_rate: Decimal,
    exit_costs: ExitCostEstimate,
    early_tp_buffer: Decimal,
    best_opportunity_apy: Decimal,
    historical_context: dict[str, Any],
    liq_data: LiquidationData,
    orderbook: OrderbookSnapshot | None,
) -> ExitRulesContext:
    """Build ExitRulesContext with all parameters."""
    return ExitRulesContext(
        # Core parameters
        trade=trade,
        current_pnl=total_pnl,
        current_apy=funding_data.apy,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        price_pnl=price_pnl,
        # Time constraints
        min_hold_seconds=ts.min_hold_seconds,
        max_hold_hours=ts.max_hold_hours,
        # Profit targets
        profit_target_usd=ts.min_profit_exit_usd,
        funding_flip_hours=ts.funding_flip_hours_threshold,
        high_water_mark=trade.high_water_mark,
        estimated_exit_cost_usd=exit_costs.total_cost,
        # Opportunity cost
        best_opportunity_apy=best_opportunity_apy,
        opportunity_cost_diff=ts.opportunity_cost_apy_diff,
        # Early exit settings
        early_take_profit_enabled=getattr(ts, "early_take_profit_enabled", False),
        early_take_profit_net_usd=getattr(ts, "early_take_profit_net_usd", Decimal("0")),
        early_take_profit_slippage_multiple=getattr(ts, "early_take_profit_slippage_multiple", Decimal("1.5")),
        early_take_profit_execution_buffer_usd=early_tp_buffer,
        early_edge_exit_enabled=getattr(ts, "early_edge_exit_enabled", False),
        early_edge_exit_min_age_seconds=int(getattr(ts, "early_edge_exit_min_age_seconds", 3600)),
        # Net EV exit settings
        exit_ev_enabled=getattr(ts, "exit_ev_enabled", False),
        exit_ev_horizon_hours=getattr(ts, "exit_ev_horizon_hours", Decimal("12.0")),
        exit_ev_exit_cost_multiple=getattr(ts, "exit_ev_exit_cost_multiple", Decimal("1.4")),
        exit_ev_skip_profit_target_when_edge_good=getattr(ts, "exit_ev_skip_profit_target_when_edge_good", True),
        exit_ev_skip_opportunity_cost_when_edge_good=getattr(ts, "exit_ev_skip_opportunity_cost_when_edge_good", True),
        # Historical context
        historical_crash_count=historical_context.get("crash_count", 0),
        historical_avg_recovery_minutes=historical_context.get("avg_recovery_minutes", 0),
        current_crash_depth_pct=historical_context.get("current_crash_depth"),
        predicted_recovery_p50_minutes=historical_context.get("predicted_recovery_p50"),
        max_hold_extend_on_recovery_enabled=getattr(ts, "max_hold_extend_on_recovery_enabled", False),
        # Z-Score exit
        z_score_exit_enabled=getattr(ts, "z_score_exit_enabled", False),
        z_score_exit_threshold=getattr(ts, "z_score_exit_threshold", Decimal("-2.0")),
        z_score_emergency_threshold=getattr(ts, "z_score_emergency_threshold", Decimal("-3.0")),
        z_score_exit_min_age_hours=getattr(ts, "z_score_exit_min_age_hours", 168),
        historical_apy=historical_context.get("recent_apy_history", []),
        # Yield vs cost exit
        yield_cost_exit_enabled=getattr(ts, "yield_cost_exit_enabled", False),
        yield_cost_max_hours=getattr(ts, "yield_cost_max_hours", Decimal("24.0")),
        # Basis convergence exit
        basis_convergence_exit_enabled=getattr(ts, "basis_convergence_exit_enabled", False),
        basis_convergence_threshold_pct=getattr(ts, "basis_convergence_threshold_pct", Decimal("0.0005")),
        basis_convergence_ratio=getattr(ts, "basis_convergence_ratio", Decimal("0.20")),
        basis_convergence_min_profit_usd=getattr(ts, "basis_convergence_min_profit_usd", Decimal("0.50")),
        current_spread_pct=_calculate_cross_exchange_spread(orderbook) if orderbook else None,
        entry_spread_pct=getattr(trade, "entry_spread", Decimal("0")),
        # Risk parameters
        liquidation_distance_monitoring_enabled=getattr(ts, "liquidation_distance_monitoring_enabled", False),
        liquidation_distance_min_pct=getattr(ts, "liquidation_distance_min_pct", Decimal("0.10")),
        leg1_liq_distance_pct=liq_data.leg1_liq_distance_pct,
        leg2_liq_distance_pct=liq_data.leg2_liq_distance_pct,
        leg1_mark_price=liq_data.leg1_mark_price if liq_data.leg1_mark_price > 0 else None,
        leg2_mark_price=liq_data.leg2_mark_price if liq_data.leg2_mark_price > 0 else None,
        delta_bound_enabled=getattr(ts, "delta_bound_enabled", True),
        delta_bound_max_delta_pct=getattr(ts, "delta_bound_max_delta_pct", Decimal("0.03")),
        # Rebalance parameters
        rebalance_enabled=getattr(ts, "rebalance_enabled", True),
        rebalance_min_delta_pct=getattr(ts, "rebalance_min_delta_pct", Decimal("0.01")),
        rebalance_max_delta_pct=getattr(ts, "rebalance_max_delta_pct", Decimal("0.03")),
        # ATR trailing stop
        atr_trailing_enabled=getattr(ts, "atr_trailing_enabled", True),
        atr_period=getattr(ts, "atr_period", 14),
        atr_multiplier=getattr(ts, "atr_multiplier", Decimal("2.0")),
        atr_min_activation_usd=getattr(ts, "atr_min_activation_usd", Decimal("3.0")),
        # Funding velocity exit
        funding_velocity_exit_enabled=getattr(ts, "funding_velocity_exit_enabled", True),
        velocity_threshold_hourly=getattr(ts, "velocity_threshold_hourly", Decimal("-0.0015")),
        acceleration_threshold=getattr(ts, "acceleration_threshold", Decimal("-0.0008")),
        velocity_lookback_hours=getattr(ts, "velocity_lookback_hours", 6),
    )


# =============================================================================
# Original Helper Functions
# =============================================================================


def _calculate_cross_exchange_spread(ob: OrderbookSnapshot) -> Decimal | None:
    """
    Calculate cross-exchange price spread for basis convergence detection.

    Returns the absolute price difference between exchanges as a percentage of mid price.
    e.g., 0.001 = 0.1% spread
    """
    if not ob:
        return None

    # Calculate mid prices for each exchange using helper functions
    lighter_mid = _get_lighter_mid_price(ob)
    x10_mid = _get_x10_mid_price(ob)

    if lighter_mid <= 0 or x10_mid <= 0:
        return None

    # Overall mid price
    overall_mid = (lighter_mid + x10_mid) / 2

    # Cross-exchange spread as percentage
    spread_pct = abs(lighter_mid - x10_mid) / overall_mid if overall_mid > 0 else Decimal("0")
    return spread_pct


def _get_lighter_mid_price(ob: OrderbookSnapshot | None, fallback_price: Decimal | None = None) -> Decimal:
    """
    Calculate Lighter mid-price from orderbook with fallback.

    Args:
        ob: Orderbook snapshot (may be None)
        fallback_price: Fallback price from price_data.lighter_price

    Returns:
        Mid-price (bid + ask) / 2, or fallback if orderbook unavailable/invalid
    """
    if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
        return (ob.lighter_bid + ob.lighter_ask) / 2
    return fallback_price if fallback_price is not None else Decimal("0")


def _get_x10_mid_price(ob: OrderbookSnapshot | None, fallback_price: Decimal | None = None) -> Decimal:
    """
    Calculate X10 mid-price from orderbook with fallback.

    Args:
        ob: Orderbook snapshot (may be None)
        fallback_price: Fallback price from price_data.x10_price

    Returns:
        Mid-price (bid + ask) / 2, or fallback if orderbook unavailable/invalid
    """
    if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
        return (ob.x10_bid + ob.x10_ask) / 2
    return fallback_price if fallback_price is not None else Decimal("0")


async def _evaluate_exit(self, trade: Trade, best_opportunity_apy: Decimal = Decimal("0")) -> ExitDecision:
    """
    Evaluate exit rules for a trade.

    Refactored to use helper functions for better maintainability.
    """
    ts = self.settings.trading

    # Get current market data
    price_data = self.market_data.get_price(trade.symbol)
    funding_data = self.market_data.get_funding(trade.symbol)

    # Check feature flags
    liq_monitoring_enabled = getattr(ts, "liquidation_distance_monitoring_enabled", False)
    delta_bound_enabled = getattr(ts, "delta_bound_enabled", True)
    early_tp_enabled = getattr(ts, "early_take_profit_enabled", False)

    # Step 1: Fetch liquidation data (runs even without market data for safety)
    liq_data = LiquidationData()
    if liq_monitoring_enabled or delta_bound_enabled:
        liq_data = await _fetch_liquidation_data(self, trade, price_data, liq_monitoring_enabled)

    # Early return if no market data
    if not price_data or not funding_data:
        return ExitDecision(False, "No market data")

    # Step 2: Log liquidation metrics (after early return)
    if liq_monitoring_enabled or delta_bound_enabled:
        _log_liquidation_metrics(trade, liq_data, liq_monitoring_enabled, delta_bound_enabled)

    # Step 3: Fetch PnL with appropriate orderbook data
    pnl_data = await _fetch_pnl_with_orderbook(self, trade, price_data, early_tp_enabled, self.settings)

    # Step 4: Calculate exit costs
    exit_costs = _calculate_exit_costs(self, trade, price_data, pnl_data.orderbook)
    current_pnl = pnl_data.current_pnl - exit_costs.fee_cost

    # Step 5: Get funding rates
    lighter_rate = funding_data.lighter_rate.rate_hourly if funding_data.lighter_rate else Decimal("0")
    x10_rate = funding_data.x10_rate.rate_hourly if funding_data.x10_rate else Decimal("0")

    # Step 6: Persist trade snapshot (throttled)
    await _persist_trade_snapshot(self, trade, funding_data, lighter_rate, x10_rate)

    # Validate entry prices
    if trade.leg1.entry_price <= Decimal("0") or trade.leg2.entry_price <= Decimal("0"):
        logger.warning(
            f"Invalid entry prices for {trade.symbol}: "
            f"leg1.entry_price={trade.leg1.entry_price}, leg2.entry_price={trade.leg2.entry_price}"
        )

    # Step 7: Calculate accrued funding
    accrued_funding = _calculate_accrued_funding(trade, lighter_rate, x10_rate)

    # Step 8: Calculate PnL components
    price_pnl = current_pnl
    total_pnl = price_pnl + trade.funding_collected + accrued_funding

    # Step 9: Calculate early-TP execution buffer
    early_tp_buffer = _calculate_early_tp_execution_buffer(trade, pnl_data.orderbook, self.settings)

    # Step 10: Update high water mark
    await _update_high_water_mark(self, trade, total_pnl)

    # Step 11: Get historical context
    historical_context = await self._get_historical_context(trade.symbol, funding_data.apy)

    # Step 12: Build and evaluate exit rules context
    ctx = _build_exit_rules_context(
        trade=trade,
        ts=ts,
        total_pnl=total_pnl,
        price_pnl=price_pnl,
        funding_data=funding_data,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        exit_costs=exit_costs,
        early_tp_buffer=early_tp_buffer,
        best_opportunity_apy=best_opportunity_apy,
        historical_context=historical_context,
        liq_data=liq_data,
        orderbook=pnl_data.orderbook,
    )

    return evaluate_exit_rules_from_context(ctx)
