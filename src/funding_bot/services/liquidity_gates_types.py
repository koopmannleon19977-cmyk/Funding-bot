"""
Shared types for liquidity/depth gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class L1DepthGateResult:
    passed: bool
    reason: str = ""
    metrics: dict[str, Any] | None = None


@dataclass(frozen=True)
class L1DepthCapResult:
    """
    Maximum size (qty + notional) that would pass the configured L1 depth gates.

    Note: Some constraints are size-independent (e.g., `min_l1_notional_usd`). If those
    fail, `passed` will be False and cap values will be 0.
    """

    passed: bool
    reason: str = ""
    max_qty: Decimal = Decimal("0")
    max_notional_usd: Decimal = Decimal("0")
    metrics: dict[str, Any] | None = None


@dataclass(frozen=True)
class DepthVWAPResult:
    ok: bool
    vwap: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    best_price: Decimal = Decimal("0")
    used_levels: int = 0
    window_min_price: Decimal = Decimal("0")
    window_max_price: Decimal = Decimal("0")
    reason: str = ""
