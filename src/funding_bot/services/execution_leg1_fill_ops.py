"""
Fill processing helpers for leg1.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from funding_bot.domain.models import Order, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg1_types import Leg1AttemptContext, Leg1Config, Leg1State

logger = get_logger("funding_bot.services.execution")


async def _process_leg1_fill(
    self,
    trade: Trade,
    ctx: Leg1AttemptContext,
    state: Leg1State,
    filled_order: Order | None,
    *,
    initial_pos_qty: Decimal | None,
    pos_tolerance: Decimal,
) -> None:
    if not filled_order or filled_order.filled_qty <= 0:
        return

    fill_increment = filled_order.filled_qty

    # Guard against double-counting fills when we previously assimilated via position
    # delta, or when an exchange reports cumulative filled_qty while we track deltas.
    if ctx.attempt_index > 0 and initial_pos_qty is not None and state.last_pos_delta is not None:
        proposed_total = state.total_filled + fill_increment
        if proposed_total > state.last_pos_delta + pos_tolerance:
            try:
                current_pos = await self.lighter.get_position(trade.symbol)
                current_pos_qty = current_pos.qty if current_pos else Decimal("0")
                current_delta = current_pos_qty - initial_pos_qty
                state.last_pos_delta = current_delta
                if proposed_total > current_delta + pos_tolerance:
                    fill_increment = max(Decimal("0"), current_delta - state.total_filled)
            except Exception as e:
                logger.warning(f"Fill/position reconciliation failed (ignoring): {e}")

    if fill_increment <= 0:
        return

    state.total_filled += fill_increment
    fill_price = (
        filled_order.avg_fill_price if filled_order.avg_fill_price and filled_order.avg_fill_price > 0 else ctx.price
    )
    state.total_notional += fill_increment * fill_price

    # Fees may be cumulative in some adapter paths; pro-rate by the fill increment.
    with contextlib.suppress(Exception):
        if filled_order.fee and filled_order.fee > 0 and filled_order.filled_qty > 0:
            fee_per_unit = filled_order.fee / filled_order.filled_qty
            state.total_fees += fee_per_unit * fill_increment

    # Update cumulative stats in trade object
    trade.leg1.filled_qty = state.total_filled
    trade.leg1.entry_price = state.total_notional / state.total_filled
    trade.leg1.fees = state.total_fees
    await self._update_trade(trade)

    logger.info(f"Leg1 partial fill: {fill_increment:.6f}. Running total: {state.total_filled:.6f}")


async def _ghost_fill_reconcile(
    self,
    trade: Trade,
    ctx: Leg1AttemptContext,
    state: Leg1State,
    *,
    initial_pos_qty: Decimal | None,
) -> None:
    # [Safety Check] Ghost Fill Verification
    # If Order API says "Not Filled" (or partial), but Position API
    # says "Full Position Delta", trust the Position.
    req_check = initial_pos_qty is not None and state.total_filled < trade.target_qty
    if not req_check:
        return

    try:
        # Use market step_size as reconciliation tolerance when available.
        step_size = Decimal("0")
        with contextlib.suppress(Exception):
            mi = await self.lighter.get_market_info(trade.symbol)
            if mi and mi.step_size:
                step_size = mi.step_size

        current_pos = await self.lighter.get_position(trade.symbol)
        current_pos_qty = current_pos.qty if current_pos else Decimal("0")

        # Calculate detected position change
        # (absolute, assuming uni-directional trade opening)
        # Note: initial_pos_qty is guaranteed non-None by req_check above
        assert initial_pos_qty is not None  # Type guard for mypy
        position_delta = current_pos_qty - initial_pos_qty

        # If position grew significantly more than Order API
        # Allow small tolerance for rounding,
        # capture major discrepancies (e.g. 2 vs 5400)
        detect_threshold = Decimal("0.0001")
        if step_size > 0:
            # Detect smaller discrepancies for very small step sizes.
            detect_threshold = min(detect_threshold, step_size / 2)

        if position_delta > state.total_filled + detect_threshold:
            ghost_filled_qty = position_delta - state.total_filled
            warn_threshold = max(Decimal("0.0001"), step_size * 2) if step_size > 0 else Decimal("0.0001")
            if ghost_filled_qty <= warn_threshold:
                logger.info(
                    f"Minor fill reconciliation for {trade.symbol}: "
                    f"Order API={state.total_filled:.4f}, Pos Delta={position_delta:.4f} "
                    f"(+{ghost_filled_qty:.4f})"
                )
            else:
                logger.warning(
                    f"GHOST FILL DETECTED for {trade.symbol}! "
                    f"Order API: {state.total_filled:.4f}, "
                    f"Pos Delta: {position_delta:.4f}. "
                    f"Recovering {ghost_filled_qty:.4f} from Pos."
                )

            # Assimilate Ghost Fill
            state.total_filled += ghost_filled_qty
            # We don't know exact price of ghost fill,
            # assume limit price of this attempt
            state.total_notional += ghost_filled_qty * ctx.price

            # Update Trade
            trade.leg1.filled_qty = state.total_filled
            trade.leg1.entry_price = state.total_notional / state.total_filled
            await self._update_trade(trade)

    except Exception as pg_err:
        logger.warning(f"Failed ghost fill check (position): {pg_err}")


async def _apply_leg1_escalation(
    self,
    trade: Trade,
    state: Leg1State,
    config: Leg1Config,
    *,
    price_fallback: Decimal,
) -> None:
    remaining_qty = trade.target_qty - state.total_filled
    if remaining_qty <= 0:
        return

    taker_filled = await self._place_lighter_taker_ioc(
        symbol=trade.symbol,
        side=trade.leg1.side,
        qty=remaining_qty,
        slippage=config.escalate_slippage,
        reduce_only=False,
        timeout_seconds=config.escalate_fill_timeout,
        purpose="leg1_escalation",
    )
    if taker_filled and taker_filled.filled_qty > 0:
        state.total_filled += taker_filled.filled_qty
        taker_fill_price = (
            taker_filled.avg_fill_price
            if taker_filled.avg_fill_price and taker_filled.avg_fill_price > 0
            else (taker_filled.price if taker_filled.price and taker_filled.price > 0 else price_fallback)
        )
        state.total_notional += taker_filled.filled_qty * taker_fill_price
        state.total_fees += taker_filled.fee

        trade.leg1.order_id = taker_filled.order_id
        trade.leg1.filled_qty = state.total_filled
        trade.leg1.entry_price = state.total_notional / state.total_filled
        trade.leg1.fees = state.total_fees
        await self._update_trade(trade)

        logger.info(
            f"Leg1 taker fill (escalation): {taker_filled.filled_qty:.6f}. Running total: {state.total_filled:.6f}"
        )
