"""
Attempt loop for leg1 execution.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.errors import InsufficientBalanceError, Leg1FailedError, Leg1HedgeEvaporatedError
from funding_bot.domain.models import ExecutionState, Opportunity, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg1_fill_ops import (
    _apply_leg1_escalation,
    _ghost_fill_reconcile,
    _process_leg1_fill,
)
from funding_bot.services.execution_leg1_order_ops import (
    _cancel_lighter_order_if_active,
    _place_or_modify_leg1_order,
    _wait_for_leg1_fill_with_guard,
)
from funding_bot.services.execution_leg1_precheck import _pre_attempt_position_check
from funding_bot.services.execution_leg1_pricing import _compute_leg1_pricing
from funding_bot.services.execution_leg1_recording import _record_leg1_attempt
from funding_bot.services.execution_leg1_types import Leg1Config, Leg1State

if TYPE_CHECKING:
    from funding_bot.services.execution_leg1_types import Leg1AttemptContext

logger = get_logger("funding_bot.services.execution")


# ---------------------------------------------------------------------------
# Helper Functions for Leg1 Attempt Loop
# ---------------------------------------------------------------------------


async def _handle_maker_to_taker_escalation(
    self,
    trade: Trade,
    state: Leg1State,
    config: Leg1Config,
    ctx: "Leg1AttemptContext",
    attempt_timeout: float,
    maker_wait_timeout: float,
) -> tuple[bool, int]:
    """
    Handle maker-to-taker escalation when maker order doesn't fill in time.

    Returns (cancel_success, cancel_attempts).
    """
    do_escalate = (
        config.escalate_enabled
        and maker_wait_timeout < float(attempt_timeout)
        and (trade.target_qty - state.total_filled) > 0
    )

    if not do_escalate:
        return True, 0

    cancel_success, cancel_attempts = await _cancel_lighter_order_if_active(
        self, trade, state.active_order_id
    )

    if not cancel_success:
        state.cancel_failures += 1
        logger.error(
            f"CRITICAL: Could not verify cancellation of order "
            f"{state.active_order_id}. Possible zombie."
        )
    else:
        await _apply_leg1_escalation(
            self,
            trade,
            state,
            config,
            price_fallback=ctx.price,
        )

    return cancel_success, cancel_attempts


async def _handle_cancel_or_defer_modify(
    self,
    trade: Trade,
    state: Leg1State,
    config: Leg1Config,
    attempt: int,
) -> tuple[bool, int]:
    """
    Handle cancel of unfilled portion or defer for modify_order on next attempt.

    Returns (cancel_success, cancel_attempts).
    """
    should_cancel = True
    state.use_modify_next = False

    if (
        attempt < config.max_attempts - 1
        and hasattr(self.lighter, "modify_order")
        and state.active_order_id
    ):
        state.use_modify_next = True
        should_cancel = False
        logger.info(
            f"Deferring cancel for {state.active_order_id} to use modify_order next attempt"
        )

    if not should_cancel:
        return True, 0

    cancel_success, cancel_attempts = await _cancel_lighter_order_if_active(
        self, trade, state.active_order_id
    )

    if not cancel_success:
        state.cancel_failures += 1
        logger.error(
            f"CRITICAL: Could not verify cancellation of order "
            f"{state.active_order_id}. Possible zombie."
        )

    return cancel_success, cancel_attempts


async def _handle_insufficient_balance_error(
    self,
    trade: Trade,
    ctx: "Leg1AttemptContext",
    config: Leg1Config,
    state: Leg1State,
    attempt: int,
    attempt_id: str | None,
    error: InsufficientBalanceError,
) -> None:
    """Handle InsufficientBalanceError - abort immediately, no point retrying."""
    state.place_failures += 1
    logger.error(f"Leg1 attempt {attempt + 1} failed (insufficient balance/margin): {error}")
    await _record_leg1_attempt(
        self,
        trade,
        ctx,
        config,
        state,
        attempt_id=attempt_id,
        order_id=state.active_order_id,
        wait_seconds=None,
        filled_order=None,
        cancel_success=None,
        cancel_attempts=0,
        error=str(error),
        error_code=getattr(error, "error_code", "INSUFFICIENT_BALANCE"),
        details=getattr(error, "details", None),
        include_smart=True,
    )
    trade.execution_state = ExecutionState.ABORTED
    raise Leg1FailedError(str(error), symbol=trade.symbol) from error


async def _handle_hedge_evaporated_error(
    self,
    trade: Trade,
    ctx: "Leg1AttemptContext",
    config: Leg1Config,
    state: Leg1State,
    attempt: int,
    attempt_id: str | None,
    error: Leg1HedgeEvaporatedError,
) -> None:
    """Handle Leg1HedgeEvaporatedError - cancel order, rollback if needed."""
    state.place_failures += 1
    logger.error(f"Leg1 attempt {attempt + 1} failed (hedge liquidity evaporated): {error}")

    # Best-effort: ensure the active maker order is cancelled
    with contextlib.suppress(Exception):
        await _cancel_lighter_order_if_active(self, trade, state.active_order_id)

    # If we have any exposure on Lighter, record it
    with contextlib.suppress(Exception):
        pos = await self.lighter.get_position(trade.symbol)
        if pos and pos.qty and pos.qty > 0:
            trade.leg1.filled_qty = pos.qty

    await _record_leg1_attempt(
        self,
        trade,
        ctx,
        config,
        state,
        attempt_id=attempt_id,
        order_id=state.active_order_id,
        wait_seconds=None,
        filled_order=None,
        cancel_success=None,
        cancel_attempts=0,
        error=str(error),
        error_code=getattr(error, "error_code", "LEG1_HEDGE_EVAPORATED"),
        include_smart=True,
    )

    if trade.leg1.filled_qty and trade.leg1.filled_qty > 0:
        await self._rollback(trade, str(error))
    else:
        trade.execution_state = ExecutionState.ABORTED
        await self._update_trade(trade)

    raise Leg1FailedError(str(error), symbol=trade.symbol) from error


async def _handle_generic_error(
    self,
    trade: Trade,
    ctx: "Leg1AttemptContext",
    config: Leg1Config,
    state: Leg1State,
    attempt: int,
    attempt_id: str | None,
    error: Exception,
) -> None:
    """Handle generic exception - record and retry if attempts remaining."""
    state.place_failures += 1
    logger.error(f"Leg1 attempt {attempt + 1} failed: {error}")
    await _record_leg1_attempt(
        self,
        trade,
        ctx,
        config,
        state,
        attempt_id=attempt_id,
        order_id=state.active_order_id,
        wait_seconds=None,
        filled_order=None,
        cancel_success=None,
        cancel_attempts=0,
        error=str(error),
        include_smart=True,
    )
    if attempt < config.max_attempts - 1:
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Main Leg1 Attempt Loop (Orchestrator)
# ---------------------------------------------------------------------------


async def _execute_leg1_attempts(
    self,
    trade: Trade,
    opp: Opportunity,
    config: Leg1Config,
    state: Leg1State,
    *,
    attempt_id: str | None = None,
) -> None:
    """
    Leg1 execution attempt loop with maker-to-taker escalation.

    Orchestrates:
    1. Pre-attempt position check (safety)
    2. Pricing computation
    3. Order placement/modification
    4. Fill waiting with hedge guard
    5. Fill processing and ghost fill reconciliation
    6. Maker-to-taker escalation if needed
    7. Cancel or defer-modify handling
    """
    for attempt in range(config.max_attempts):
        # 1. [CRITICAL SAFETY] Check actual exchange position BEFORE placing new order
        await _pre_attempt_position_check(self, trade, opp, config, state, attempt)

        # 2. Check if already filled
        remaining_qty = trade.target_qty - state.total_filled
        if remaining_qty <= 0:
            state.success = True
            break

        # 3. Compute pricing
        ctx, price_data = _compute_leg1_pricing(self, trade, config, state, attempt)
        if ctx is None or price_data is None:
            await asyncio.sleep(1.0)
            continue

        logger.info(
            f"Leg1 execution attempt {attempt + 1}/{config.max_attempts} "
            f"(Remaining: {ctx.remaining_qty:.6f}, "
            f"Aggressiveness: {ctx.aggressiveness:.2f}) for {trade.symbol}"
        )

        attempt_timeout = (
            config.attempt_timeouts[attempt]
            if attempt < len(config.attempt_timeouts)
            else config.attempt_timeouts[-1]
        )

        try:
            # 4. Place or modify order
            order, _modified = await _place_or_modify_leg1_order(self, trade, ctx, state)

            # 5. Calculate maker wait timeout
            maker_wait_timeout = float(attempt_timeout)
            if (
                config.escalate_enabled
                and config.escalate_after_seconds > 0
                and maker_wait_timeout > config.escalate_after_seconds
            ):
                maker_wait_timeout = config.escalate_after_seconds

            # 6. Wait for fill with hedge guard
            filled_order, wait_seconds, active_order_id = await _wait_for_leg1_fill_with_guard(
                self, trade, opp, order, maker_wait_timeout
            )
            state.active_order_id = active_order_id

            # 7. Process fill
            await _process_leg1_fill(
                self, trade, ctx, state, filled_order,
                initial_pos_qty=config.initial_pos_qty,
                pos_tolerance=config.pos_tolerance,
            )

            # 8. Ghost fill reconciliation
            await _ghost_fill_reconcile(
                self, trade, ctx, state, initial_pos_qty=config.initial_pos_qty
            )

            # 9. Check success after processing
            if state.total_filled >= trade.target_qty * Decimal("0.999"):
                await _record_leg1_attempt(
                    self, trade, ctx, config, state,
                    attempt_id=attempt_id, order_id=state.active_order_id,
                    wait_seconds=wait_seconds, filled_order=filled_order,
                    cancel_success=None, cancel_attempts=0, include_smart=True,
                )
                state.success = True
                break

            # 10. Maker-to-taker escalation
            escalate_cancel_success, escalate_cancel_attempts = await _handle_maker_to_taker_escalation(
                self, trade, state, config, ctx, attempt_timeout, maker_wait_timeout
            )

            # Check success after escalation
            did_escalate = (
                config.escalate_enabled
                and maker_wait_timeout < float(attempt_timeout)
                and (trade.target_qty - state.total_filled) > 0
            )
            if state.total_filled >= trade.target_qty * Decimal("0.999"):
                state.success = True
                await _record_leg1_attempt(
                    self, trade, ctx, config, state,
                    attempt_id=attempt_id, order_id=state.active_order_id,
                    wait_seconds=wait_seconds, filled_order=filled_order,
                    cancel_success=escalate_cancel_success, cancel_attempts=escalate_cancel_attempts,
                    escalated_to_taker=True, include_smart=False,
                )
                break

            # 11. Cancel unfilled portion or defer for modify
            cancel_success, cancel_attempts = await _handle_cancel_or_defer_modify(
                self, trade, state, config, attempt
            )

            # 12. Record attempt
            await _record_leg1_attempt(
                self, trade, ctx, config, state,
                attempt_id=attempt_id, order_id=state.active_order_id,
                wait_seconds=wait_seconds, filled_order=filled_order,
                cancel_success=cancel_success, cancel_attempts=cancel_attempts,
                escalated_to_taker=did_escalate, include_smart=True,
            )

        except InsufficientBalanceError as e:
            await _handle_insufficient_balance_error(
                self, trade, ctx, config, state, attempt, attempt_id, e
            )

        except Leg1HedgeEvaporatedError as e:
            await _handle_hedge_evaporated_error(
                self, trade, ctx, config, state, attempt, attempt_id, e
            )

        except Exception as e:
            await _handle_generic_error(
                self, trade, ctx, config, state, attempt, attempt_id, e
            )
