"""
Helpers for Leg2 execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import Side
from funding_bot.services.market_data import OrderbookSnapshot
from funding_bot.utils.decimals import safe_decimal as _safe_decimal


async def _leg2_get_base_price(
    self,
    *,
    symbol: str,
    side: Side,
    x10_symbol: str,
    ob: OrderbookSnapshot | None,
    use_fresh: bool,
) -> tuple[Decimal, Decimal, str]:
    if not use_fresh and ob:
        if side == Side.BUY:
            return (
                _safe_decimal(getattr(ob, "x10_ask", 0)),
                _safe_decimal(getattr(ob, "x10_ask_qty", 0)),
                "cache_init",
            )
        return (
            _safe_decimal(getattr(ob, "x10_bid", 0)),
            _safe_decimal(getattr(ob, "x10_bid_qty", 0)),
            "cache_init",
        )

    # Prefer WS-fed MarketData cache if available; fall back to adapter REST.
    cached = self.market_data.get_orderbook(symbol)
    if (
        cached
        and cached.x10_bid > 0
        and cached.x10_ask > 0
        and cached.x10_bid < cached.x10_ask
        and cached.x10_updated
    ):
        age = (datetime.now(UTC) - cached.x10_updated).total_seconds()
        if age <= 1.0:
            if side == Side.BUY:
                return (
                    _safe_decimal(getattr(cached, "x10_ask", 0)),
                    _safe_decimal(getattr(cached, "x10_ask_qty", 0)),
                    "cache_ws_hot",
                )
            return (
                _safe_decimal(getattr(cached, "x10_bid", 0)),
                _safe_decimal(getattr(cached, "x10_bid_qty", 0)),
                "cache_ws_hot",
            )

    book = await self.x10.get_orderbook_l1(x10_symbol)
    if not isinstance(book, dict):
        return Decimal("0"), Decimal("0"), "rest_l1_invalid"
    if side == Side.BUY:
        return (
            _safe_decimal(book.get("best_ask", 0)),
            _safe_decimal(book.get("ask_qty", 0)),
            "rest_l1",
        )
    return (
        _safe_decimal(book.get("best_bid", 0)),
        _safe_decimal(book.get("bid_qty", 0)),
        "rest_l1",
    )
