"""
Utilities for position management.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def _round_price_to_tick(price: Decimal, tick: Decimal, *, rounding: str) -> Decimal:
    if tick <= 0:
        return price
    if price <= 0:
        return price
    q = price / tick
    if rounding == "ceil":
        return q.to_integral_value(rounding=ROUND_CEILING) * tick
    return q.to_integral_value(rounding=ROUND_FLOOR) * tick
