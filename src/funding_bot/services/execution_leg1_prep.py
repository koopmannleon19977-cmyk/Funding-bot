"""
Prep helpers for leg1 execution.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from funding_bot.domain.models import ExecutionState, Opportunity, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_helpers import _compute_attempt_timeouts, _safe_bool
from funding_bot.services.execution_leg1_types import Leg1Config, Leg1State
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _prepare_leg1_execution(
    self,
    trade: Trade,
    opp: Opportunity,
    *,
    attempt_id: str | None = None,
) -> tuple[Leg1Config, Leg1State]:
    es = self.settings.execution
    smart_enabled = bool(getattr(es, "smart_pricing_enabled", False))
    smart_l1_util_trigger = _safe_decimal(
        getattr(es, "smart_pricing_l1_utilization_trigger", None),
        default=Decimal("0.7"),
    )
    smart_maker_floor = _safe_decimal(
        getattr(es, "smart_pricing_maker_aggressiveness_floor", None),
        default=Decimal("0"),
    )
    if smart_l1_util_trigger <= 0:
        smart_l1_util_trigger = Decimal("0.7")
    if smart_maker_floor < 0:
        smart_maker_floor = Decimal("0")
    if smart_maker_floor > 1:
        smart_maker_floor = Decimal("1")

    # Update state
    trade.execution_state = ExecutionState.LEG1_SUBMITTED
    await self._update_trade(trade)

    # Ensure per-symbol Lighter orderbook WS is running (best-effort).
    with contextlib.suppress(Exception):
        await self.lighter.ensure_orderbook_ws(trade.symbol, timeout=2.0)  # type: ignore[attr-defined]

    # Chase loop
    max_attempts = es.maker_order_max_retries
    if max_attempts < 1:
        max_attempts = 1

    # Distribute total timeout across attempts with a configurable schedule.
    attempt_timeouts = _compute_attempt_timeouts(
        total_seconds=float(es.maker_order_timeout_seconds),
        attempts=max_attempts,
        min_per_attempt_seconds=float(
            _safe_decimal(getattr(es, "maker_attempt_timeout_min_seconds", None), Decimal("5.0"))
        ),
        schedule=str(getattr(es, "maker_attempt_timeout_schedule", "equal")),
    )
    max_aggr_setting = _safe_decimal(getattr(es, "maker_max_aggressiveness", None), Decimal("0.5"))
    force_post_only_setting = _safe_bool(getattr(es, "maker_force_post_only", None), False)
    escalate_enabled = _safe_bool(getattr(es, "leg1_escalate_to_taker_enabled", None), False)
    escalate_after_seconds = float(
        _safe_decimal(getattr(es, "leg1_escalate_to_taker_after_seconds", None), Decimal("0"))
    )
    escalate_slippage = _safe_decimal(getattr(es, "leg1_escalate_to_taker_slippage", None), Decimal("0"))
    escalate_fill_timeout = float(
        _safe_decimal(getattr(es, "leg1_escalate_to_taker_fill_timeout_seconds", None), Decimal("6.0"))
    )
    await self._kpi_update_attempt(
        attempt_id,
        {
            "data": {
                "leg1_policy": {
                    "max_attempts": max_attempts,
                    "total_timeout_seconds": str(es.maker_order_timeout_seconds),
                    "attempt_timeouts": [str(Decimal(str(x))) for x in attempt_timeouts],
                    "max_aggressiveness": str(max_aggr_setting),
                    "force_post_only": force_post_only_setting,
                    "escalate_to_taker_enabled": escalate_enabled,
                    "escalate_after_seconds": str(Decimal(str(max(0.0, escalate_after_seconds)))),
                    "escalate_slippage": str(escalate_slippage),
                    "escalate_fill_timeout_seconds": str(Decimal(str(max(0.0, escalate_fill_timeout)))),
                    "timeout_schedule": str(getattr(es, "maker_attempt_timeout_schedule", "equal")),
                }
            }
        },
    )

    # [Safety] Snapshot initial position to detect "Ghost Fills"
    initial_pos_qty: Decimal | None = None
    try:
        initial_pos = await self.lighter.get_position(trade.symbol)
        initial_pos_qty = initial_pos.qty if initial_pos else Decimal("0")
    except Exception as e:
        logger.warning(f"Failed to snapshot initial position for {trade.symbol}: {e}")

    # Best-effort tolerance for position-delta reconciliation (avoid overcounting fills).
    pos_tolerance = Decimal("0.0001")
    with contextlib.suppress(Exception):
        mi = await self.lighter.get_market_info(trade.symbol)
        if mi and getattr(mi, "step_size", None):
            step_size = mi.step_size
            if isinstance(step_size, Decimal) and step_size > 0:
                pos_tolerance = max(pos_tolerance, step_size * 2)

    config = Leg1Config(
        smart_enabled=smart_enabled,
        smart_l1_util_trigger=smart_l1_util_trigger,
        smart_maker_floor=smart_maker_floor,
        max_attempts=max_attempts,
        attempt_timeouts=attempt_timeouts,
        max_aggr_setting=max_aggr_setting,
        force_post_only_setting=force_post_only_setting,
        escalate_enabled=escalate_enabled,
        escalate_after_seconds=escalate_after_seconds,
        escalate_slippage=escalate_slippage,
        escalate_fill_timeout=escalate_fill_timeout,
        initial_pos_qty=initial_pos_qty,
        pos_tolerance=pos_tolerance,
    )
    state = Leg1State()
    state.total_fees = trade.leg1.fees  # Initialize with existing fees (fix for rollback re-entry)
    return config, state
