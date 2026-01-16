"""
Shared types for leg1 execution helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from funding_bot.domain.models import TimeInForce


@dataclass(slots=True)
class Leg1Config:
    smart_enabled: bool
    smart_l1_util_trigger: Decimal
    smart_maker_floor: Decimal
    max_attempts: int
    attempt_timeouts: list[float]
    max_aggr_setting: Decimal
    force_post_only_setting: bool
    escalate_enabled: bool
    escalate_after_seconds: float
    escalate_slippage: Decimal
    escalate_fill_timeout: float
    initial_pos_qty: Decimal | None
    pos_tolerance: Decimal


@dataclass(slots=True)
class Leg1State:
    total_filled: Decimal = Decimal("0")
    total_notional: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    success: bool = False
    leg1_attempts: list[dict[str, object]] = field(default_factory=list)
    cancel_failures: int = 0
    place_failures: int = 0
    use_modify_next: bool = False
    active_order_id: str | None = None
    last_pos_delta: Decimal | None = None


@dataclass(slots=True)
class Leg1AttemptContext:
    attempt_index: int
    is_final_attempt: bool
    remaining_qty: Decimal
    aggressiveness: Decimal
    capped_aggr: Decimal
    tick: Decimal
    best_bid: Decimal
    best_ask: Decimal
    price: Decimal
    time_in_force: TimeInForce
    smart_aggr_floor: Decimal
    l1_qty: Decimal
    l1_util: Decimal | None
