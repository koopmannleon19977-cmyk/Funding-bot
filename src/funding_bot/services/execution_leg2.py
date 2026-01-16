"""
Execution flow helpers (moved from ExecutionEngine).

Refactored to extract helper functions for better maintainability.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from decimal import Decimal

from funding_bot.domain.errors import Leg2FailedError
from funding_bot.domain.events import LegFilled
from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Opportunity,
    OrderRequest,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg2_helpers import _leg2_get_base_price
from funding_bot.services.liquidity_gates import estimate_taker_vwap_price_by_impact
from funding_bot.services.market_data import OrderbookDepthSnapshot
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


# =============================================================================
# Configuration Data Classes
# =============================================================================


@dataclass
class Leg2Config:
    """Configuration for Leg2 (hedge) execution."""

    base_slippage: Decimal
    max_attempts: int
    fill_timeout: float
    retry_delay: float
    slippage_step: Decimal
    max_slippage: Decimal
    tick_size: Decimal
    # Smart Pricing
    smart_enabled: bool
    smart_levels: int
    smart_impact: Decimal
    smart_l1_util_trigger: Decimal
    # Dynamic Timing
    dyn_enabled: bool
    dyn_min_delay: float
    dyn_max_delay: float
    dyn_low_vol: Decimal
    dyn_high_vol: Decimal


@dataclass
class Leg2AttemptState:
    """Mutable state tracked across retry attempts."""

    total_filled: Decimal = Decimal("0")
    weighted_price_sum: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")
    prev_base_price: Decimal | None = None
    hedge_latency_recorded: bool = False


# =============================================================================
# Configuration Helpers
# =============================================================================


def _load_leg2_config(settings, x10_info, ob, trade: Trade) -> Leg2Config:
    """Load all Leg2 configuration from settings."""
    es = settings.execution
    ts = settings.trading

    tick = x10_info.tick_size if x10_info else Decimal("0.01")

    base_slippage = _safe_decimal(
        getattr(es, "taker_order_slippage", None),
        default=Decimal("0"),
    )

    raw_max_attempts = getattr(es, "hedge_ioc_max_attempts", 1)
    try:
        max_attempts = max(1, int(raw_max_attempts))
    except Exception:
        max_attempts = 1

    fill_timeout = float(_safe_decimal(
        getattr(es, "hedge_ioc_fill_timeout_seconds", None),
        default=Decimal("8.0"),
    ))

    retry_delay = float(_safe_decimal(
        getattr(es, "hedge_ioc_retry_delay_seconds", None),
        default=Decimal("0.2"),
    ))

    slippage_step = _safe_decimal(
        getattr(es, "hedge_ioc_slippage_step", None),
        default=Decimal("0"),
    )

    max_slippage = _safe_decimal(
        getattr(es, "hedge_ioc_max_slippage", None),
        default=base_slippage,
    )

    # Apply profitability-aware slippage adjustment
    max_slippage = _adjust_slippage_for_leg1_spread(
        trade, ob, base_slippage, max_slippage
    )

    # Smart Pricing config
    smart_enabled = bool(getattr(es, "smart_pricing_enabled", False))
    smart_levels = int(
        getattr(es, "smart_pricing_depth_levels", 0)
        or getattr(ts, "depth_gate_levels", 20)
        or 20
    )
    smart_levels = max(1, min(smart_levels, 250))

    smart_impact = _safe_decimal(
        getattr(es, "smart_pricing_max_price_impact_percent", None),
        default=_safe_decimal(
            getattr(ts, "depth_gate_max_price_impact_percent", None),
            default=Decimal("0.0015"),
        ),
    )

    smart_l1_util_trigger = _safe_decimal(
        getattr(es, "smart_pricing_l1_utilization_trigger", None),
        default=_safe_decimal(
            getattr(ts, "max_l1_qty_utilization", None),
            default=Decimal("0.7"),
        ),
    )
    if smart_l1_util_trigger <= 0:
        smart_l1_util_trigger = Decimal("0.7")

    # Dynamic timing config
    dyn_enabled = bool(getattr(es, "hedge_dynamic_timing_enabled", True))
    dyn_min_delay = float(_safe_decimal(
        getattr(es, "hedge_dynamic_timing_min_retry_delay_seconds", None),
        default=Decimal("0.05"),
    ))
    dyn_max_delay = float(_safe_decimal(
        getattr(es, "hedge_dynamic_timing_max_retry_delay_seconds", None),
        default=Decimal("0.8"),
    ))
    dyn_low_vol = _safe_decimal(
        getattr(es, "hedge_dynamic_timing_low_volatility_pct", None),
        default=Decimal("0.0002"),
    )
    dyn_high_vol = _safe_decimal(
        getattr(es, "hedge_dynamic_timing_high_volatility_pct", None),
        default=Decimal("0.0010"),
    )

    return Leg2Config(
        base_slippage=base_slippage,
        max_attempts=max_attempts,
        fill_timeout=fill_timeout,
        retry_delay=retry_delay,
        slippage_step=slippage_step,
        max_slippage=max_slippage,
        tick_size=tick,
        smart_enabled=smart_enabled,
        smart_levels=smart_levels,
        smart_impact=smart_impact,
        smart_l1_util_trigger=smart_l1_util_trigger,
        dyn_enabled=dyn_enabled,
        dyn_min_delay=dyn_min_delay,
        dyn_max_delay=dyn_max_delay,
        dyn_low_vol=dyn_low_vol,
        dyn_high_vol=dyn_high_vol,
    )


def _adjust_slippage_for_leg1_spread(
    trade: Trade,
    ob,
    base_slippage: Decimal,
    max_slippage: Decimal,
) -> Decimal:
    """
    Adjust max slippage based on Leg1's realized spread.

    If Leg1 got a favorable spread (below mid), we can afford more hedge slippage.
    """
    leg1_entry = trade.leg1.entry_price
    if not (leg1_entry and leg1_entry > 0 and ob):
        return max(base_slippage, max_slippage)

    l_bid = _safe_decimal(getattr(ob, "lighter_bid", 0), default=Decimal("0"))
    l_ask = _safe_decimal(getattr(ob, "lighter_ask", 0), default=Decimal("0"))

    if not (l_bid > 0 and l_ask > 0 and l_bid < l_ask):
        return max(base_slippage, max_slippage)

    mid_price = (l_bid + l_ask) / 2
    if mid_price <= 0:
        return max(base_slippage, max_slippage)

    # Calculate realized spread: negative = we got BETTER than mid
    if trade.leg1.side == Side.BUY:
        realized_spread = (leg1_entry - mid_price) / mid_price
    else:
        realized_spread = (mid_price - leg1_entry) / mid_price

    # If spread is negative (favorable), increase max_slippage
    if realized_spread < 0:
        spread_bonus = abs(realized_spread) * Decimal("0.8")
        old_max = max_slippage
        max_slippage = max_slippage + spread_bonus
        logger.info(
            f"Dynamic Slippage Cap: Leg1 realized_spread={realized_spread:.4%} "
            f"(favorable). Adjusting max_slippage: {old_max:.4%} -> {max_slippage:.4%}"
        )

    return max(base_slippage, max_slippage)


# =============================================================================
# Dynamic Timing Helpers
# =============================================================================


def _calculate_dynamic_retry_delay(
    cfg: Leg2Config,
    base_price: Decimal,
    prev_base_price: Decimal | None,
    cached_ob,
) -> float:
    """Calculate dynamic retry delay based on volatility and spread."""
    if not cfg.dyn_enabled or cfg.retry_delay <= 0:
        return cfg.retry_delay

    # Volatility proxy: change in base price between attempts
    vol = Decimal("0")
    if prev_base_price and prev_base_price > 0:
        vol = abs(base_price - prev_base_price) / prev_base_price

    # Spread proxy from cached orderbook
    spread = Decimal("0")
    if cached_ob and cached_ob.x10_bid > 0 and cached_ob.x10_ask > 0 and cached_ob.x10_bid < cached_ob.x10_ask:
        mid = (cached_ob.x10_bid + cached_ob.x10_ask) / 2
        if mid > 0:
            spread = (cached_ob.x10_ask - cached_ob.x10_bid) / mid

    # Convert vol into a multiplier: lower delay on high vol, higher delay on low vol
    factor = Decimal("1")
    if cfg.dyn_high_vol > 0 and vol >= cfg.dyn_high_vol:
        factor = Decimal("0.5")
    elif cfg.dyn_low_vol > 0 and vol <= cfg.dyn_low_vol:
        factor = Decimal("1.5")
    elif cfg.dyn_high_vol > cfg.dyn_low_vol and cfg.dyn_high_vol > 0:
        # Linear interpolation between 1.5 -> 0.5
        t = (vol - cfg.dyn_low_vol) / (cfg.dyn_high_vol - cfg.dyn_low_vol)
        t = max(Decimal("0"), min(Decimal("1"), t))
        factor = Decimal("1.5") - t

    # If spread is wide, avoid hammering the book
    if spread > Decimal("0.0015"):
        factor = max(factor, Decimal("1.2"))

    return float(max(
        Decimal(str(cfg.dyn_min_delay)),
        min(Decimal(str(cfg.dyn_max_delay)), Decimal(str(cfg.retry_delay)) * factor)
    ))


# =============================================================================
# Price Calculation Helpers
# =============================================================================


async def _apply_smart_pricing(
    self,
    cfg: Leg2Config,
    trade: Trade,
    x10_symbol: str,
    remaining_qty: Decimal,
    l1_qty: Decimal,
    base_price: Decimal,
) -> tuple[Decimal, bool]:
    """
    Apply smart pricing using depth-VWAP if L1 is too thin.

    Returns (price, smart_used).
    """
    if not cfg.smart_enabled:
        return base_price, False

    util = remaining_qty / l1_qty if l1_qty > 0 else Decimal("1e18")
    if util <= cfg.smart_l1_util_trigger:
        return base_price, False

    try:
        depth = await self.x10.get_orderbook_depth(x10_symbol, depth=cfg.smart_levels)
        if not isinstance(depth, dict):
            raise TypeError("x10.get_orderbook_depth returned non-dict")

        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        depth_ob = OrderbookDepthSnapshot(
            symbol=trade.symbol,
            x10_bids=bids,
            x10_asks=asks,
        )

        vwap_price, _ = estimate_taker_vwap_price_by_impact(
            depth_ob,
            exchange="X10",
            side=trade.leg2.side,
            target_qty=remaining_qty,
            max_price_impact_pct=cfg.smart_impact,
        )

        if vwap_price and vwap_price > 0:
            return vwap_price, True

    except Exception as e:
        logger.debug(f"SmartPricing depth VWAP failed (X10 {trade.symbol}): {e}")

    return base_price, False


def _calculate_limit_price(
    cfg: Leg2Config,
    base_price: Decimal,
    side: Side,
    attempt: int,
) -> Decimal:
    """Calculate the limit price with slippage and tick rounding."""
    slippage = min(
        cfg.base_slippage + (cfg.slippage_step * Decimal(attempt)),
        cfg.max_slippage
    )

    if side == Side.BUY:
        price = base_price * (Decimal("1") + slippage)
    else:
        price = base_price * (Decimal("1") - slippage)

    # Tick rounding
    if cfg.tick_size > 0:
        if side == Side.BUY:
            price = (price / cfg.tick_size).quantize(
                Decimal("1"), rounding="ROUND_CEILING"
            ) * cfg.tick_size
        else:
            price = (price / cfg.tick_size).quantize(
                Decimal("1"), rounding="ROUND_FLOOR"
            ) * cfg.tick_size

    return price


# =============================================================================
# Latency Recording Helper
# =============================================================================


async def _record_hedge_latency(
    self,
    trade: Trade,
    attempt_id: str | None,
    t_submit0: float,
    t_submit1: float,
    leg1_filled_monotonic: float | None,
) -> None:
    """Record hedge latency metrics."""
    place_ack_ms = Decimal(str(max(0.0, (t_submit1 - t_submit0) * 1000.0)))
    metrics: dict[str, str] = {"leg2_place_ack_ms": str(place_ack_ms)}

    if leg1_filled_monotonic is not None:
        leg1_to_submit_ms = Decimal(str(max(0.0, (t_submit0 - leg1_filled_monotonic) * 1000.0)))
        leg1_to_ack_ms = Decimal(str(max(0.0, (t_submit1 - leg1_filled_monotonic) * 1000.0)))
        metrics.update({
            "leg1_to_leg2_submit_ms": str(leg1_to_submit_ms),
            "leg1_to_leg2_place_ack_ms": str(leg1_to_ack_ms),
        })
        logger.info(
            f"Hedge latency {trade.symbol}: "
            f"leg1->leg2_submit={float(leg1_to_submit_ms):.0f}ms "
            f"leg1->leg2_ack={float(leg1_to_ack_ms):.0f}ms "
            f"(place_ack={float(place_ack_ms):.0f}ms)"
        )

        kpi_update = {
            "data": {"hedge_latency": metrics},
            "leg2_place_ack_ms": place_ack_ms,
            "leg1_to_leg2_submit_ms": leg1_to_submit_ms,
            "leg1_to_leg2_ack_ms": leg1_to_ack_ms,
        }
    else:
        logger.info(
            f"Hedge latency {trade.symbol}: "
            f"place_ack={float(place_ack_ms):.0f}ms (no leg1 timestamp)"
        )
        kpi_update = {
            "data": {"hedge_latency": metrics},
            "leg2_place_ack_ms": place_ack_ms,
        }

    await self._kpi_update_attempt(attempt_id, kpi_update)

# =============================================================================
# Fill Processing Helpers
# =============================================================================


@dataclass
class FillResult:
    """Result of processing a fill."""

    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    status_info: str
    should_cancel: bool


def _process_fill_result(
    filled_order,
    trade: Trade,
    order_id: str,
    attempt: int,
    max_attempts: int,
) -> FillResult:
    """Process the result of waiting for an order fill."""
    filled_qty = filled_order.filled_qty if filled_order else Decimal("0")
    avg_price = filled_order.avg_fill_price if filled_order else Decimal("0")
    fee = filled_order.fee if filled_order else Decimal("0")
    status_info = f"status={filled_order.status}" if filled_order else "order not found"

    # [Rule 7] Execution Integrity: check if we need to cancel active remainder
    should_cancel = bool(filled_order and filled_order.is_active)
    if should_cancel:
        logger.warning(
            f"Leg2 order {order_id} active after wait ({filled_order.status}). "
            f"Will cancel remainder (Rule 7)."
        )

    if not filled_order or filled_qty <= 0:
        logger.warning(
            f"Leg2 IOC not filled (attempt {attempt + 1}/{max_attempts}): {status_info}"
        )

    return FillResult(
        filled_qty=filled_qty,
        avg_price=avg_price,
        fee=fee,
        status_info=status_info,
        should_cancel=should_cancel,
    )


async def _cancel_active_order(self, symbol: str, order_id: str) -> None:
    """Cancel an active order to prevent lost orders."""
    try:
        await self.x10.cancel_order(symbol, order_id)
    except Exception as cx:
        logger.error(f"Leg2 force-cancel failed: {cx}")


async def _fetch_base_price_with_fallback(
    self,
    trade: Trade,
    x10_symbol: str,
    ob,
    use_fresh: bool,
) -> tuple[Decimal, Decimal]:
    """Fetch base price with fallback to fresh quote if needed."""
    try:
        base_price, l1_qty, _ = await _leg2_get_base_price(
            self,
            symbol=trade.symbol,
            side=trade.leg2.side,
            x10_symbol=x10_symbol,
            ob=ob,
            use_fresh=use_fresh,
        )
    except Exception as e:
        if not ob:
            raise Leg2FailedError(
                f"Failed to fetch X10 L1 for hedge: {e}",
                symbol=trade.symbol,
            ) from e
        base_price, l1_qty, _ = await _leg2_get_base_price(
            self,
            symbol=trade.symbol,
            side=trade.leg2.side,
            x10_symbol=x10_symbol,
            ob=ob,
            use_fresh=False,
        )

    # Fallback: try fresh L1 if cached is missing/partial
    if base_price <= 0 and not use_fresh:
        with contextlib.suppress(Exception):
            base_price, l1_qty, _ = await _leg2_get_base_price(
                self,
                symbol=trade.symbol,
                side=trade.leg2.side,
                x10_symbol=x10_symbol,
                ob=ob,
                use_fresh=True,
            )

    if base_price <= 0:
        side_label = "Ask" if trade.leg2.side == Side.BUY else "Bid"
        raise Leg2FailedError(f"No X10 {side_label} liquidity (<=0)", symbol=trade.symbol)

    return base_price, l1_qty


def _handle_partial_fill(
    self,
    trade: Trade,
    total_filled: Decimal,
    desired_qty: Decimal,
    remaining_qty: Decimal,
    avg_price: Decimal,
    base_price: Decimal,
) -> tuple[bool, bool]:
    """
    Handle partial fill logic.

    Returns (should_break, should_continue):
    - (True, False) = break out of loop (within tolerance)
    - (False, True) = continue to next attempt
    - (False, False) = raise error (can't continue)
    """
    price_for_notional = avg_price if avg_price > 0 else base_price
    remaining_notional = remaining_qty * price_for_notional

    microfill_max_unhedged = _safe_decimal(
        getattr(self.settings.execution, "microfill_max_unhedged_usd", None),
        default=Decimal("0"),
    )

    if remaining_notional <= microfill_max_unhedged:
        logger.warning(
            f"Leg2 under-hedged remainder within tolerance: "
            f"{remaining_qty:.6f} {trade.symbol} (~${remaining_notional:.2f})."
        )
        return True, False

    logger.warning(
        f"Leg2 partial hedge: {total_filled:.6f}/{desired_qty:.6f}. "
        f"Will retry remainder {remaining_qty:.6f}..."
    )
    return False, True


# =============================================================================
# Main Execution Function
# =============================================================================


async def _execute_leg2(
    self,
    trade: Trade,
    opp: Opportunity,
    *,
    desired_qty: Decimal | None = None,
    leg1_filled_monotonic: float | None = None,
    attempt_id: str | None = None,
) -> None:
    """Execute Leg 2 (X10 - Hedge) using Smart Limit IOC.

    The X10 SDK places LIMIT orders (including IOC). We hedge by submitting a marketable
    LIMIT IOC with a slippage cap. If the IOC cancels without fill, we retry quickly using
    a fresh X10 L1 snapshot and slightly wider caps before triggering a rollback.
    """
    # Update state
    trade.execution_state = ExecutionState.LEG2_SUBMITTED
    await self._update_trade(trade)

    order_type = OrderType.LIMIT
    tif = TimeInForce.IOC

    # Market info and X10 symbol
    x10_info = self.market_data.get_market_info(trade.symbol, Exchange.X10)
    x10_symbol = self.market_data.get_x10_symbol(trade.symbol)
    ob = self.market_data.get_orderbook(trade.symbol)

    # Load all configuration using helper
    cfg = _load_leg2_config(self.settings, x10_info, ob, trade)

    # Determine quantity to hedge
    desired_qty = trade.leg1.filled_qty if desired_qty is None else desired_qty
    if desired_qty <= 0:
        raise Leg2FailedError("Leg1 filled qty is 0; cannot hedge", symbol=trade.symbol)

    # Skip if already hedged (e.g. salvage flow)
    if trade.leg2.filled_qty and trade.leg2.filled_qty >= desired_qty and trade.leg2.entry_price > 0:
        logger.info(
            f"Leg2 already hedged for {trade.symbol}: "
            f"{trade.leg2.filled_qty:.6f}/{desired_qty:.6f}"
        )
        return

    # Initialize attempt state
    state = Leg2AttemptState()

    try:
        remaining_qty = desired_qty
        for attempt in range(cfg.max_attempts):
            # Get base price (use fresh quote on retries)
            base_price, l1_qty = await _fetch_base_price_with_fallback(
                self, trade, x10_symbol, ob, use_fresh=(attempt > 0)
            )

            # Calculate dynamic retry delay based on volatility
            cached_ob = self.market_data.get_orderbook(trade.symbol)
            dynamic_retry_delay = _calculate_dynamic_retry_delay(
                cfg, base_price, state.prev_base_price, cached_ob
            )

            # Apply smart pricing if L1 is thin
            base_price, smart_used = await _apply_smart_pricing(
                self, cfg, trade, x10_symbol, remaining_qty, l1_qty, base_price
            )

            # Calculate limit price with slippage
            price = _calculate_limit_price(cfg, base_price, trade.leg2.side, attempt)

            logger.info(
                f"Placing X10 hedge (attempt {attempt + 1}/{cfg.max_attempts}): "
                f"{trade.leg2.side.value} {remaining_qty} {trade.symbol} @ {price:.6f} "
                f"(Base: {base_price:.6f}, Smart: {smart_used}, RetryDelay: {dynamic_retry_delay:.3f}s)"
            )

            # Submit order
            request = OrderRequest(
                symbol=trade.symbol,
                exchange=Exchange.X10,
                side=trade.leg2.side,
                qty=remaining_qty,
                order_type=order_type,
                price=price,
                time_in_force=tif,
            )

            t_submit0 = time.monotonic()
            order = await self.x10.place_order(request)
            t_submit1 = time.monotonic()
            trade.leg2.order_id = order.order_id
            logger.info(f"Leg2 order placed: {order.order_id} ({order_type} IOC @ {price:.6f})")

            # Record hedge latency (first attempt only)
            if not state.hedge_latency_recorded:
                state.hedge_latency_recorded = True
                await _record_hedge_latency(
                    self, trade, attempt_id, t_submit0, t_submit1, leg1_filled_monotonic
                )

            # Wait for fill
            filled_order = await self._wait_for_fill(
                self.x10, trade.symbol, order.order_id, timeout=cfg.fill_timeout
            )

            # Handle fill result
            fill_result = _process_fill_result(
                filled_order, trade, order.order_id, attempt, cfg.max_attempts
            )

            # Cancel remainder if order still active (Rule 7)
            if fill_result.should_cancel:
                await _cancel_active_order(self, trade.symbol, order.order_id)

            # Handle no fill
            if fill_result.filled_qty <= 0:
                if attempt < cfg.max_attempts - 1:
                    await asyncio.sleep(dynamic_retry_delay)
                    state.prev_base_price = base_price
                    continue
                raise Leg2FailedError(
                    f"Hedge order not filled: {fill_result.status_info} (IOC expired/empty)",
                    symbol=trade.symbol,
                )

            # Accumulate fills
            state.total_filled += fill_result.filled_qty
            state.weighted_price_sum += fill_result.filled_qty * fill_result.avg_price
            state.total_fee += fill_result.fee

            # Publish fill event
            await self.event_bus.publish(
                LegFilled(
                    trade_id=trade.trade_id,
                    symbol=trade.symbol,
                    exchange=Exchange.X10,
                    side=trade.leg2.side,
                    order_id=filled_order.order_id,
                    qty=fill_result.filled_qty,
                    price=fill_result.avg_price,
                    fee=fill_result.fee,
                )
            )

            logger.info(f"Leg2 fill: {fill_result.filled_qty:.6f} @ {fill_result.avg_price:.6f}")

            # Check if fully hedged
            if state.total_filled >= desired_qty:
                break

            # Handle partial fill
            remaining_qty = desired_qty - state.total_filled
            should_break, should_continue = _handle_partial_fill(
                self, trade, state.total_filled, desired_qty, remaining_qty,
                fill_result.avg_price, base_price
            )
            if should_break:
                break
            if should_continue and attempt < cfg.max_attempts - 1:
                await asyncio.sleep(dynamic_retry_delay)
                state.prev_base_price = base_price
                continue
            if not should_break and not should_continue:
                raise Leg2FailedError(
                    f"Hedge partial fill too low: {state.total_filled}/{desired_qty}",
                    symbol=trade.symbol,
                )

        if state.total_filled <= 0:
            raise Leg2FailedError("Hedge order not filled after retries", symbol=trade.symbol)

        # Finalize trade with accumulated values
        trade.leg2.filled_qty = state.total_filled
        trade.leg2.entry_price = (
            (state.weighted_price_sum / state.total_filled)
            if state.total_filled > 0
            else Decimal("0")
        )
        trade.leg2.fees = (trade.leg2.fees or Decimal("0")) + state.total_fee

        logger.info(
            f"Leg2 finalized: {trade.leg2.filled_qty:.6f} @ {trade.leg2.entry_price:.6f} "
            f"(fee=${trade.leg2.fees:.6f})"
        )

    except Exception as e:
        logger.error(f"Leg2 failed, initiating rollback: {e}")
        await self._rollback(trade, str(e))
        raise
