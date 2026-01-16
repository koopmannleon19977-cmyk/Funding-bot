"""
KPI/attempt recording helpers for leg1.
"""

from __future__ import annotations

from funding_bot.domain.models import Trade
from funding_bot.services.execution_leg1_types import Leg1AttemptContext, Leg1Config, Leg1State


async def _record_leg1_attempt(
    self,
    trade: Trade,
    ctx: Leg1AttemptContext,
    config: Leg1Config,
    state: Leg1State,
    *,
    attempt_id: str | None,
    order_id: str | None,
    wait_seconds: object | None,
    filled_order: object | None,
    cancel_success: bool | None,
    cancel_attempts: int,
    escalated_to_taker: bool | None = None,
    error: str | None = None,
    error_code: str | None = None,
    details: object | None = None,
    include_smart: bool = True,
) -> None:
    entry: dict[str, object] = {
        "i": ctx.attempt_index + 1,
        "is_final": ctx.is_final_attempt,
        "aggr": str(ctx.aggressiveness),
        "capped_aggr": str(ctx.capped_aggr),
        "tick": str(ctx.tick),
        "best_bid": str(ctx.best_bid),
        "best_ask": str(ctx.best_ask),
        "side": trade.leg1.side.value,
        "price": str(ctx.price),
        "tif": ctx.time_in_force.value,
        "qty": str(ctx.remaining_qty),
        "order_id": order_id,
        "wait_seconds": None if wait_seconds is None else str(wait_seconds),
        "filled_qty": "0" if not filled_order else str(filled_order.filled_qty),
        "avg_fill_price": "0" if not filled_order else str(filled_order.avg_fill_price),
        "status": None if not filled_order else filled_order.status.value,
        "cancel_success": cancel_success,
        "cancel_attempts": cancel_attempts,
    }

    if include_smart:
        entry["smart_pricing"] = {
            "enabled": config.smart_enabled,
            "l1_qty": str(ctx.l1_qty),
            "l1_util": None if ctx.l1_util is None else str(ctx.l1_util),
            "aggr_floor": str(ctx.smart_aggr_floor),
            "l1_util_trigger": str(config.smart_l1_util_trigger),
            "maker_floor_setting": str(config.smart_maker_floor),
        }

    if escalated_to_taker is not None:
        entry["escalated_to_taker"] = escalated_to_taker
    if error is not None:
        entry["error"] = error
    if error_code is not None:
        entry["error_code"] = error_code
    if details is not None:
        entry["details"] = details

    state.leg1_attempts.append(entry)
    if len(state.leg1_attempts) > 8:
        state.leg1_attempts = state.leg1_attempts[-8:]

    await self._kpi_update_attempt(
        attempt_id,
        {
            "data": {
                "leg1_attempts": state.leg1_attempts,
                "leg1_cancel_failures": state.cancel_failures,
                "leg1_place_failures": state.place_failures,
                "leg1_total_filled_qty": str(state.total_filled),
                "leg1_total_fees": str(state.total_fees),
            }
        },
    )
