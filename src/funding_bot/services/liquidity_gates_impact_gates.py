"""
Depth impact-based gate checks and sizing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from funding_bot.domain.models import Side
from funding_bot.services.market_data import OrderbookDepthSnapshot

from .liquidity_gates_impact_core import _available_within_price_impact, _depth_levels_for
from .liquidity_gates_types import L1DepthCapResult, L1DepthGateResult


def calculate_depth_cap_by_impact(
    ob: OrderbookDepthSnapshot,
    *,
    lighter_side: Side,
    x10_side: Side,
    mid_price: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
    max_price_impact_pct: Decimal,
    safety_buffer: Decimal | None = None,
) -> L1DepthCapResult:
    """
    Like `calculate_l1_depth_cap`, but uses cumulative depth within a max price impact window.
    """
    if mid_price <= 0:
        return L1DepthCapResult(False, "invalid mid_price", metrics={})

    inf = Decimal("1e18")

    buffer = safety_buffer
    if buffer is None or buffer <= 0 or buffer >= 1:
        buffer = None

    def _cap_for(exchange: str, side: Side) -> tuple[bool, str, Decimal, dict[str, Any]]:
        levels = _depth_levels_for(ob, exchange=exchange, side=side)
        avail_qty, avail_notional, best_price, used_levels = _available_within_price_impact(
            levels,
            side=side,
            max_price_impact_pct=max_price_impact_pct,
        )

        if avail_qty <= 0 or avail_notional <= 0:
            return (
                False,
                f"missing {exchange} depth {side.value}",
                Decimal("0"),
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                },
            )

        if min_l1_notional_usd and min_l1_notional_usd > 0 and avail_notional < min_l1_notional_usd:
            return (
                False,
                f"{exchange} depth notional too low",
                Decimal("0"),
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "min_l1_notional_usd": str(min_l1_notional_usd),
                    "levels_used": used_levels,
                },
            )

        cap_by_mult = inf
        if min_l1_notional_multiple and min_l1_notional_multiple > 0:
            cap_by_mult = avail_notional / min_l1_notional_multiple

        cap_by_util = inf
        if max_l1_qty_utilization and Decimal("0") < max_l1_qty_utilization < 1:
            cap_by_util = avail_qty * max_l1_qty_utilization * mid_price

        cap_notional = min(avail_notional, cap_by_mult, cap_by_util)
        if buffer is not None:
            cap_notional = cap_notional * buffer

        return (
            True,
            "",
            max(Decimal("0"), cap_notional),
            {
                "exchange": exchange,
                "side": side.value,
                "best_price": str(best_price),
                "available_qty": str(avail_qty),
                "available_notional_usd": str(avail_notional),
                "levels_used": used_levels,
                "impact": {
                    "max_price_impact_pct": str(max_price_impact_pct),
                },
                "caps": {
                    "by_available_notional": str(avail_notional),
                    "by_min_l1_notional_multiple": str(cap_by_mult),
                    "by_max_l1_qty_utilization": str(cap_by_util),
                    "buffer": None if buffer is None else str(buffer),
                },
            },
        )

    ok_l, reason_l, cap_l, m_l = _cap_for("LIGHTER", lighter_side)
    ok_x, reason_x, cap_x, m_x = _cap_for("X10", x10_side)

    passed = ok_l and ok_x
    reason = reason_l if not ok_l else reason_x

    cap_notional = min(cap_l, cap_x) if passed else Decimal("0")
    cap_qty = cap_notional / mid_price if passed and mid_price > 0 else Decimal("0")

    metrics: dict[str, Any] = {
        "mid_price": str(mid_price),
        "max_notional_usd": str(cap_notional),
        "max_qty": str(cap_qty),
        "thresholds": {
            "min_l1_notional_usd": str(min_l1_notional_usd),
            "min_l1_notional_multiple": str(min_l1_notional_multiple),
            "max_l1_qty_utilization": str(max_l1_qty_utilization),
            "max_price_impact_pct": str(max_price_impact_pct),
            "safety_buffer": None if buffer is None else str(buffer),
        },
        "lighter": m_l,
        "x10": m_x,
    }

    return L1DepthCapResult(
        passed=passed,
        reason=reason,
        max_qty=cap_qty,
        max_notional_usd=cap_notional,
        metrics=metrics,
    )


def check_depth_for_entry_by_impact(
    ob: OrderbookDepthSnapshot,
    *,
    lighter_side: Side,
    x10_side: Side,
    target_qty: Decimal,
    target_notional_usd: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
    max_price_impact_pct: Decimal,
) -> L1DepthGateResult:
    """
    Like `check_l1_depth_for_entry`, but uses cumulative depth within a max price impact window.
    """
    if target_qty <= 0 or target_notional_usd <= 0:
        return L1DepthGateResult(False, "invalid target size", metrics={})

    required_notional = Decimal("0")
    if min_l1_notional_usd and min_l1_notional_usd > 0:
        required_notional = min_l1_notional_usd
    if min_l1_notional_multiple and min_l1_notional_multiple > 0:
        required_notional = max(required_notional, target_notional_usd * min_l1_notional_multiple)

    def _one(exchange: str, side: Side) -> tuple[bool, str, dict[str, Any]]:
        levels = _depth_levels_for(ob, exchange=exchange, side=side)
        avail_qty, avail_notional, best_price, used_levels = _available_within_price_impact(
            levels,
            side=side,
            max_price_impact_pct=max_price_impact_pct,
        )

        utilization: Decimal | None = None
        if avail_qty > 0:
            utilization = target_qty / avail_qty

        if avail_qty <= 0 or avail_notional <= 0:
            return (
                False,
                f"missing {exchange} depth {side.value}",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "utilization": None if utilization is None else str(utilization),
                },
            )

        if required_notional > 0 and avail_notional < required_notional:
            return (
                False,
                f"{exchange} depth notional too low",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "required_notional_usd": str(required_notional),
                    "utilization": None if utilization is None else str(utilization),
                },
            )

        utilization_exceeded = (
            max_l1_qty_utilization
            and max_l1_qty_utilization < 1
            and utilization is not None
            and utilization > max_l1_qty_utilization
        )
        if utilization_exceeded:
            return (
                False,
                f"{exchange} depth utilization too high",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "utilization": str(utilization),
                },
            )

        return (
            True,
            "",
            {
                "exchange": exchange,
                "side": side.value,
                "best_price": str(best_price),
                "available_qty": str(avail_qty),
                "available_notional_usd": str(avail_notional),
                "levels_used": used_levels,
                "utilization": None if utilization is None else str(utilization),
            },
        )

    ok_l, reason_l, m_l = _one("LIGHTER", lighter_side)
    ok_x, reason_x, m_x = _one("X10", x10_side)

    passed = ok_l and ok_x
    reason = reason_l if not ok_l else reason_x

    metrics: dict[str, Any] = {
        "required_notional_usd": str(required_notional),
        "target_qty": str(target_qty),
        "target_notional_usd": str(target_notional_usd),
        "thresholds": {
            "min_l1_notional_usd": str(min_l1_notional_usd),
            "min_l1_notional_multiple": str(min_l1_notional_multiple),
            "max_l1_qty_utilization": str(max_l1_qty_utilization),
            "max_price_impact_pct": str(max_price_impact_pct),
        },
        "lighter": m_l,
        "x10": m_x,
    }
    return L1DepthGateResult(passed=passed, reason=reason, metrics=metrics)


def check_x10_depth_compliance_by_impact(
    ob: OrderbookDepthSnapshot,
    *,
    x10_side: Side,
    target_qty: Decimal,
    target_notional_usd: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
    max_price_impact_pct: Decimal,
) -> L1DepthGateResult:
    """
    Check ONLY X10 depth compliance (impact-based).
    Used for Post-Leg1 Guard where we are already filled on Lighter.
    """
    if target_qty <= 0 or target_notional_usd <= 0:
        return L1DepthGateResult(False, "invalid target size", metrics={})

    required_notional = Decimal("0")
    if min_l1_notional_usd and min_l1_notional_usd > 0:
        required_notional = min_l1_notional_usd
    if min_l1_notional_multiple and min_l1_notional_multiple > 0:
        required_notional = max(required_notional, target_notional_usd * min_l1_notional_multiple)

    def _one(exchange: str, side: Side) -> tuple[bool, str, dict[str, Any]]:
        levels = _depth_levels_for(ob, exchange=exchange, side=side)
        avail_qty, avail_notional, best_price, used_levels = _available_within_price_impact(
            levels,
            side=side,
            max_price_impact_pct=max_price_impact_pct,
        )

        utilization: Decimal | None = None
        if avail_qty > 0:
            utilization = target_qty / avail_qty

        if avail_qty <= 0 or avail_notional <= 0:
            return (
                False,
                f"missing {exchange} depth {side.value}",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "utilization": None if utilization is None else str(utilization),
                },
            )

        if required_notional > 0 and avail_notional < required_notional:
            return (
                False,
                f"{exchange} depth notional too low",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "required_notional_usd": str(required_notional),
                    "utilization": None if utilization is None else str(utilization),
                },
            )

        utilization_exceeded = (
            max_l1_qty_utilization
            and max_l1_qty_utilization < 1
            and utilization is not None
            and utilization > max_l1_qty_utilization
        )
        if utilization_exceeded:
            return (
                False,
                f"{exchange} depth utilization too high",
                {
                    "exchange": exchange,
                    "side": side.value,
                    "best_price": str(best_price),
                    "available_qty": str(avail_qty),
                    "available_notional_usd": str(avail_notional),
                    "levels_used": used_levels,
                    "utilization": str(utilization),
                },
            )

        return (
            True,
            "",
            {
                "exchange": exchange,
                "side": side.value,
                "best_price": str(best_price),
                "available_qty": str(avail_qty),
                "available_notional_usd": str(avail_notional),
                "levels_used": used_levels,
                "utilization": None if utilization is None else str(utilization),
            },
        )

    ok_x, reason_x, m_x = _one("X10", x10_side)

    metrics = {
        "required_notional_usd": str(required_notional),
        "target_qty": str(target_qty),
        "target_notional_usd": str(target_notional_usd),
        "thresholds": {
            "min_l1_notional_usd": str(min_l1_notional_usd),
            "min_l1_notional_multiple": str(min_l1_notional_multiple),
            "max_l1_qty_utilization": str(max_l1_qty_utilization),
            "max_price_impact_pct": str(max_price_impact_pct),
        },
        "lighter": None,
        "x10": m_x,
    }
    return L1DepthGateResult(passed=ok_x, reason=reason_x, metrics=metrics)
