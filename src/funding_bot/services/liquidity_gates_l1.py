"""
L1 depth gate logic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from funding_bot.domain.models import Side
from funding_bot.services.market_data import OrderbookSnapshot

from .liquidity_gates_types import L1DepthCapResult, L1DepthGateResult


def _side_price_qty(
    ob: OrderbookSnapshot,
    *,
    exchange: str,
    side: Side,
) -> tuple[Decimal, Decimal]:
    """Get L1 price and quantity for a given exchange and side."""
    if exchange.upper() == "LIGHTER":
        if side == Side.BUY:
            return ob.lighter_ask, ob.lighter_ask_qty
        return ob.lighter_bid, ob.lighter_bid_qty
    else:
        if side == Side.BUY:
            return ob.x10_ask, ob.x10_ask_qty
        return ob.x10_bid, ob.x10_bid_qty


def _check_single_exchange_l1(
    ob: OrderbookSnapshot,
    exchange: str,
    side: Side,
    target_qty: Decimal,
    required_notional: Decimal,
    max_l1_qty_utilization: Decimal | None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Check L1 depth compliance for a single exchange.

    Extracted common logic used by check_l1_depth_for_entry and check_x10_l1_compliance.
    """
    price, qty = _side_price_qty(ob, exchange=exchange, side=side)

    # Missing L1 data
    if price <= 0 or qty <= 0:
        return False, f"missing {exchange} L1 {side.value}", {
            "exchange": exchange,
            "side": side.value,
            "l1_price": str(price),
            "l1_qty": str(qty),
            "l1_notional_usd": "0",
            "utilization": None,
        }

    l1_notional = price * qty
    utilization: Decimal | None = target_qty / qty if qty > 0 else None

    # Notional check
    if required_notional > 0 and l1_notional < required_notional:
        return False, f"{exchange} L1 notional too low", {
            "exchange": exchange,
            "side": side.value,
            "l1_price": str(price),
            "l1_qty": str(qty),
            "l1_notional_usd": str(l1_notional),
            "utilization": None if utilization is None else str(utilization),
        }

    # Utilization check
    if (
        max_l1_qty_utilization
        and max_l1_qty_utilization < 1
        and utilization is not None
        and utilization > max_l1_qty_utilization
    ):
        return False, f"{exchange} L1 utilization too high", {
            "exchange": exchange,
            "side": side.value,
            "l1_price": str(price),
            "l1_qty": str(qty),
            "l1_notional_usd": str(l1_notional),
            "utilization": str(utilization),
        }

    # Passed
    return True, "", {
        "exchange": exchange,
        "side": side.value,
        "l1_price": str(price),
        "l1_qty": str(qty),
        "l1_notional_usd": str(l1_notional),
        "utilization": None if utilization is None else str(utilization),
    }


def _compute_required_notional(
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    target_notional_usd: Decimal,
) -> Decimal:
    """Compute the required L1 notional based on config parameters."""
    required = Decimal("0")
    if min_l1_notional_usd and min_l1_notional_usd > 0:
        required = min_l1_notional_usd
    if min_l1_notional_multiple and min_l1_notional_multiple > 0:
        required = max(required, target_notional_usd * min_l1_notional_multiple)
    return required


def calculate_l1_depth_cap(
    ob: OrderbookSnapshot,
    *,
    lighter_side: Side,
    x10_side: Side,
    mid_price: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
    safety_buffer: Decimal | None = None,
) -> L1DepthCapResult:
    """
    Compute the maximum target size that would pass `check_l1_depth_for_entry`.

    This lets the bot "size down" to what the hedge book can actually support,
    instead of rejecting opportunities outright (or, worse, triggering rollbacks).
    """
    if mid_price <= 0:
        return L1DepthCapResult(False, "invalid mid_price", metrics={})

    # Avoid Decimal('Infinity') leaking into logs/DB; use a huge sentinel.
    inf = Decimal("1e18")

    buffer = safety_buffer
    if buffer is None or buffer <= 0 or buffer >= 1:
        buffer = None

    def _cap_for(exchange: str, side: Side) -> tuple[bool, str, Decimal, dict[str, Any]]:
        price, qty = _side_price_qty(ob, exchange=exchange, side=side)
        if price <= 0 or qty <= 0:
            return (
                False,
                f"missing {exchange} L1 {side.value}",
                Decimal("0"),
                {
                    "exchange": exchange,
                    "side": side.value,
                    "l1_price": str(price),
                    "l1_qty": str(qty),
                    "l1_notional_usd": "0",
                },
            )

        l1_notional = price * qty
        if min_l1_notional_usd and min_l1_notional_usd > 0 and l1_notional < min_l1_notional_usd:
            return (
                False,
                f"{exchange} L1 notional too low",
                Decimal("0"),
                {
                    "exchange": exchange,
                    "side": side.value,
                    "l1_price": str(price),
                    "l1_qty": str(qty),
                    "l1_notional_usd": str(l1_notional),
                    "min_l1_notional_usd": str(min_l1_notional_usd),
                },
            )

        cap_by_mult = inf
        if min_l1_notional_multiple and min_l1_notional_multiple > 0:
            cap_by_mult = l1_notional / min_l1_notional_multiple

        cap_by_util = inf
        if max_l1_qty_utilization and Decimal("0") < max_l1_qty_utilization < 1:
            cap_by_util = qty * max_l1_qty_utilization * mid_price

        cap_notional = min(cap_by_mult, cap_by_util)
        if buffer is not None:
            cap_notional = cap_notional * buffer

        return (
            True,
            "",
            max(Decimal("0"), cap_notional),
            {
                "exchange": exchange,
                "side": side.value,
                "l1_price": str(price),
                "l1_qty": str(qty),
                "l1_notional_usd": str(l1_notional),
                "caps": {
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


def check_l1_depth_for_entry(
    ob: OrderbookSnapshot,
    *,
    lighter_side: Side,
    x10_side: Side,
    target_qty: Decimal,
    target_notional_usd: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
) -> L1DepthGateResult:
    """
    L1 depth gate for a two-leg entry:
    - Lighter is maker leg
    - X10 is hedge leg (more sensitive to thin book)

    We gate on:
    1) L1 side notional >= required_notional
    2) utilization = target_qty / l1_qty <= max_l1_qty_utilization (if enabled)
    """
    if target_qty <= 0 or target_notional_usd <= 0:
        return L1DepthGateResult(False, "invalid target size", metrics={})

    required_notional = _compute_required_notional(
        min_l1_notional_usd, min_l1_notional_multiple, target_notional_usd
    )

    # Check both exchanges using shared helper
    ok_l, reason_l, m_l = _check_single_exchange_l1(
        ob, "LIGHTER", lighter_side, target_qty, required_notional, max_l1_qty_utilization
    )
    ok_x, reason_x, m_x = _check_single_exchange_l1(
        ob, "X10", x10_side, target_qty, required_notional, max_l1_qty_utilization
    )

    passed = ok_l and ok_x
    reason = reason_l if not ok_l else reason_x

    metrics = {
        "required_notional_usd": str(required_notional),
        "target_qty": str(target_qty),
        "target_notional_usd": str(target_notional_usd),
        "lighter": m_l,
        "x10": m_x,
    }
    return L1DepthGateResult(passed=passed, reason=reason, metrics=metrics)


def check_x10_l1_compliance(
    ob: OrderbookSnapshot,
    *,
    x10_side: Side,
    target_qty: Decimal,
    target_notional_usd: Decimal,
    min_l1_notional_usd: Decimal,
    min_l1_notional_multiple: Decimal,
    max_l1_qty_utilization: Decimal,
) -> L1DepthGateResult:
    """
    Check ONLY X10 L1 depth compliance.
    Used for Post-Leg1 Guard where we are already filled on Lighter.
    """
    if target_qty <= 0 or target_notional_usd <= 0:
        return L1DepthGateResult(False, "invalid target size", metrics={})

    required_notional = _compute_required_notional(
        min_l1_notional_usd, min_l1_notional_multiple, target_notional_usd
    )

    # Check only X10 using shared helper
    ok_x, reason_x, m_x = _check_single_exchange_l1(
        ob, "X10", x10_side, target_qty, required_notional, max_l1_qty_utilization
    )

    metrics = {
        "required_notional_usd": str(required_notional),
        "target_qty": str(target_qty),
        "target_notional_usd": str(target_notional_usd),
        "lighter": None,  # Explicitly None
        "x10": m_x,
    }
    return L1DepthGateResult(passed=ok_x, reason=reason_x, metrics=metrics)
