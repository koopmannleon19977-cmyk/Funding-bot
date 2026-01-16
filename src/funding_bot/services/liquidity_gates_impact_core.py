"""
Depth impact-based helpers (VWAP + spread estimation).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from funding_bot.domain.models import Side
from funding_bot.services.market_data import OrderbookDepthSnapshot

from .liquidity_gates_types import DepthVWAPResult


def _depth_levels_for(
    ob: OrderbookDepthSnapshot,
    *,
    exchange: str,
    side: Side,
) -> list[tuple[Decimal, Decimal]]:
    if exchange.upper() == "LIGHTER":
        return ob.lighter_asks if side == Side.BUY else ob.lighter_bids
    return ob.x10_asks if side == Side.BUY else ob.x10_bids


def _available_within_price_impact(
    levels: list[tuple[Decimal, Decimal]],
    *,
    side: Side,
    max_price_impact_pct: Decimal,
) -> tuple[Decimal, Decimal, Decimal, int]:
    """
    Compute available qty/notional within a max price impact window around the best level.

    For BUY (taker), we consume asks up to best_ask * (1 + impact).
    For SELL (taker), we consume bids down to best_bid * (1 - impact).
    """
    if not levels:
        return Decimal("0"), Decimal("0"), Decimal("0"), 0

    best_price = levels[0][0]
    if best_price <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0"), 0

    impact = max_price_impact_pct
    if impact < 0:
        impact = Decimal("0")

    max_price = best_price * (Decimal("1") + impact) if side == Side.BUY else best_price
    min_price = best_price * (Decimal("1") - impact) if side == Side.SELL else best_price

    qty_sum = Decimal("0")
    notional_sum = Decimal("0")
    used_levels = 0

    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue

        if side == Side.BUY:
            if price > max_price:
                break
        else:
            if price < min_price:
                break

        qty_sum += qty
        notional_sum += price * qty
        used_levels += 1

    return qty_sum, notional_sum, best_price, used_levels


def _vwap_for_target_qty_within_price_impact(
    levels: list[tuple[Decimal, Decimal]],
    *,
    side: Side,
    target_qty: Decimal,
    max_price_impact_pct: Decimal,
) -> DepthVWAPResult:
    """
    Compute a taker VWAP for `target_qty` within a max price impact window.

    For BUY (taker), we consume asks up to best_ask * (1 + impact).
    For SELL (taker), we consume bids down to best_bid * (1 - impact).
    """
    if target_qty <= 0:
        return DepthVWAPResult(ok=False, reason="invalid target_qty")
    if not levels:
        return DepthVWAPResult(ok=False, reason="missing levels")

    best_price = levels[0][0]
    if best_price <= 0:
        return DepthVWAPResult(ok=False, reason="invalid best_price")

    impact = max_price_impact_pct
    if impact < 0:
        impact = Decimal("0")

    window_max = best_price * (Decimal("1") + impact) if side == Side.BUY else best_price
    window_min = best_price * (Decimal("1") - impact) if side == Side.SELL else best_price

    filled_qty = Decimal("0")
    notional = Decimal("0")
    used_levels = 0

    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue

        if side == Side.BUY:
            if price > window_max:
                break
        else:
            if price < window_min:
                break

        take = min(qty, target_qty - filled_qty)
        if take <= 0:
            break

        filled_qty += take
        notional += price * take
        used_levels += 1

        if filled_qty >= target_qty:
            break

    if filled_qty <= 0:
        return DepthVWAPResult(
            ok=False,
            best_price=best_price,
            used_levels=used_levels,
            window_min_price=window_min,
            window_max_price=window_max,
            reason="no fillable depth in window",
        )

    if filled_qty < target_qty:
        return DepthVWAPResult(
            ok=False,
            best_price=best_price,
            filled_qty=filled_qty,
            used_levels=used_levels,
            window_min_price=window_min,
            window_max_price=window_max,
            reason="insufficient depth in window",
        )

    vwap = notional / filled_qty if filled_qty > 0 else Decimal("0")
    return DepthVWAPResult(
        ok=bool(vwap > 0),
        vwap=vwap,
        filled_qty=filled_qty,
        best_price=best_price,
        used_levels=used_levels,
        window_min_price=window_min,
        window_max_price=window_max,
    )


def estimate_taker_vwap_price_by_impact(
    ob: OrderbookDepthSnapshot,
    *,
    exchange: str,
    side: Side,
    target_qty: Decimal,
    max_price_impact_pct: Decimal,
) -> tuple[Decimal | None, dict[str, Any]]:
    """
    Estimate the taker fill VWAP for `target_qty` using depth within `max_price_impact_pct`.

    Returns (price_or_none, metrics).
    """
    levels = _depth_levels_for(ob, exchange=exchange, side=side)
    vwap = _vwap_for_target_qty_within_price_impact(
        levels,
        side=side,
        target_qty=target_qty,
        max_price_impact_pct=max_price_impact_pct,
    )
    metrics: dict[str, Any] = {
        "exchange": exchange,
        "side": side.value,
        "target_qty": str(target_qty),
        "best_price": str(vwap.best_price),
        "vwap": str(vwap.vwap),
        "filled_qty": str(vwap.filled_qty),
        "used_levels": int(vwap.used_levels),
        "window": {
            "min_price": str(vwap.window_min_price),
            "max_price": str(vwap.window_max_price),
            "max_price_impact_pct": str(max_price_impact_pct),
        },
        "ok": bool(vwap.ok),
        "reason": vwap.reason,
    }
    return (vwap.vwap if vwap.ok else None), metrics


def estimate_entry_spread_pct_by_impact(
    ob: OrderbookDepthSnapshot,
    *,
    long_exchange: str,
    target_qty: Decimal,
    max_price_impact_pct: Decimal,
) -> tuple[Decimal, dict[str, Any]]:
    """
    Depth-aware entry spread estimate (taker/taker worst-case).

    Mirrors `OrderbookSnapshot.entry_spread_pct`, but uses depth VWAP instead of L1 only.
    """
    long_ex = str(long_exchange or "").upper()

    if long_ex == "X10":
        buy_ex, sell_ex = "X10", "LIGHTER"
    else:
        buy_ex, sell_ex = "LIGHTER", "X10"

    buy_price, buy_m = estimate_taker_vwap_price_by_impact(
        ob,
        exchange=buy_ex,
        side=Side.BUY,
        target_qty=target_qty,
        max_price_impact_pct=max_price_impact_pct,
    )
    sell_price, sell_m = estimate_taker_vwap_price_by_impact(
        ob,
        exchange=sell_ex,
        side=Side.SELL,
        target_qty=target_qty,
        max_price_impact_pct=max_price_impact_pct,
    )

    if not buy_price or not sell_price or buy_price <= 0 or sell_price <= 0:
        return Decimal("1.0"), {
            "ok": False,
            "reason": "missing depth vwap",
            "long_exchange": long_exchange,
            "buy": buy_m,
            "sell": sell_m,
        }

    mid = (buy_price + sell_price) / 2 if (buy_price + sell_price) > 0 else Decimal("0")
    if mid <= 0:
        return Decimal("1.0"), {
            "ok": False,
            "reason": "invalid mid",
            "long_exchange": long_exchange,
            "buy": buy_m,
            "sell": sell_m,
        }

    spread = (buy_price - sell_price) / mid
    return spread, {
        "ok": True,
        "long_exchange": long_exchange,
        "buy_price": str(buy_price),
        "sell_price": str(sell_price),
        "mid_price": str(mid),
        "spread_pct": str(spread),
        "buy": buy_m,
        "sell": sell_m,
    }
