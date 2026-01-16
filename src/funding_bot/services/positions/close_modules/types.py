"""
Type definitions for close operations.

Contains dataclasses and configuration types used throughout the close module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from funding_bot.domain.models import Order, Side


@dataclass
class CloseConfig:
    """Configuration for Lighter close operations."""

    max_attempts: int
    attempt_timeout: float
    total_timeout: float
    use_fast_close: bool = False
    use_taker_directly: bool = False


@dataclass
class GridStepConfig:
    """Configuration for grid step close operation."""

    enabled: bool
    grid_step: Decimal
    num_levels: int
    qty_per_level: Decimal


@dataclass
class CloseResult:
    """Result of a Lighter maker close attempt."""

    success: bool
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")
    order_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class CloseAttemptResult:
    """Result of a single Lighter close attempt."""

    filled: bool
    order: Order | None = None
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    error: str | None = None


@dataclass
class GridStepCloseResult:
    """Result of a grid step close operation."""

    success: bool
    total_filled_qty: Decimal = Decimal("0")
    weighted_avg_price: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")
    order_ids: list[str] = field(default_factory=list)
    cancelled_orders: int = 0
    error: str | None = None


@dataclass
class TakerResult:
    """Result of a Lighter taker fallback close."""

    success: bool
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    order_id: str | None = None
    error: str | None = None


@dataclass
class CoordinatedCloseContext:
    """Context for coordinated dual-leg close."""

    trade_id: str
    symbol: str
    leg1_close_qty: Decimal
    leg2_close_qty: Decimal
    leg1_close_side: Side
    leg2_close_side: Side
    leg1_price: Decimal
    leg2_price: Decimal
    lighter_use_maker: bool
    x10_use_maker: bool
    maker_timeout: float
    escalate_to_ioc: bool
    lighter_tick: Decimal = Decimal("0.01")
    x10_tick: Decimal = Decimal("0.01")
