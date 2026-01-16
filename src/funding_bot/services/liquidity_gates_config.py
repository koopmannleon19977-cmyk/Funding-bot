"""
Configuration helpers for liquidity/depth gates.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.utils.decimals import safe_decimal


def get_depth_gate_config(ts: object) -> tuple[bool, int, Decimal]:
    """
    Resolve depth gate config from settings-like objects.
    """
    mode = str(getattr(ts, "depth_gate_mode", "L1") or "L1").strip().upper()
    use_impact = mode in {"IMPACT", "L2", "DEPTH", "VWAP"}

    levels = int(getattr(ts, "depth_gate_levels", 20) or 20)
    # Max supported today:
    # - Lighter SDK order_book_orders(limit=...) returns up to ~250 levels/side
    # - X10 snapshot depth returns up to ~200 levels/side
    # Adapters clamp further if needed; this cap is here to prevent pathological configs.
    levels = max(1, min(levels, 250))

    impact = safe_decimal(
        getattr(ts, "depth_gate_max_price_impact_percent", None),
        Decimal("0.0015"),
    )
    if impact < 0:
        impact = Decimal("0")
    if impact > Decimal("0.05"):
        impact = Decimal("0.05")

    return use_impact, levels, impact
