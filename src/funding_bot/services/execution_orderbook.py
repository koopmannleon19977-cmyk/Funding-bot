"""
Best-effort X10 orderbook helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.observability.logging import get_logger
from funding_bot.services.market_data import OrderbookDepthSnapshot, OrderbookSnapshot
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _get_best_effort_x10_depth_snapshot(
    self,
    symbol: str,
    *,
    levels: int,
    max_age_seconds: float,
) -> OrderbookDepthSnapshot:
    x10_symbol = self.market_data.get_x10_symbol(symbol)
    depth_levels = max(1, int(levels or 1))

    depth = await self.x10.get_orderbook_depth(x10_symbol, depth=depth_levels)  # type: ignore[attr-defined]
    if not isinstance(depth, dict):
        raise TypeError("x10.get_orderbook_depth returned non-dict")

    bids_raw = depth.get("bids") or []
    asks_raw = depth.get("asks") or []

    def _normalize(rows: list[object]) -> list[tuple[Decimal, Decimal]]:
        cleaned: list[tuple[Decimal, Decimal]] = []
        for row in list(rows)[:depth_levels]:
            price = None
            qty = None
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                price, qty = row[0], row[1]
            elif isinstance(row, dict):
                price = row.get("price")
                qty = row.get("qty") or row.get("size")
            else:
                price = getattr(row, "price", None)
                qty = getattr(row, "qty", None) or getattr(row, "size", None)

            if price is None or qty is None:
                continue
            p = price if isinstance(price, Decimal) else _safe_decimal(price)
            q = qty if isinstance(qty, Decimal) else _safe_decimal(qty)
            if p > 0 and q > 0:
                cleaned.append((p, q))
        return cleaned

    bids = _normalize(bids_raw)
    asks = _normalize(asks_raw)
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    now = datetime.now(UTC)
    updated_at = depth.get("updated_at")
    if isinstance(updated_at, datetime) and max_age_seconds > 0:
        age = (now - updated_at).total_seconds()
        if age > max_age_seconds:
            raise RuntimeError(f"X10 depth cache stale ({age:.2f}s)")
    elif not isinstance(updated_at, datetime):
        updated_at = now

    if not bids or not asks:
        raise RuntimeError("missing X10 depth")

    return OrderbookDepthSnapshot(
        symbol=symbol,
        x10_bids=bids,
        x10_asks=asks,
        x10_updated=updated_at,
    )


async def _get_best_effort_x10_orderbook(
    self,
    symbol: str,
    *,
    max_age_seconds: float,
) -> OrderbookSnapshot:
    try:
        depth = await self._get_best_effort_x10_depth_snapshot(
            symbol,
            levels=1,
            max_age_seconds=max_age_seconds,
        )
        best_bid, bid_qty = depth.x10_bids[0]
        best_ask, ask_qty = depth.x10_asks[0]
        return OrderbookSnapshot(
            symbol=symbol,
            lighter_bid=Decimal("0"),
            lighter_ask=Decimal("0"),
            x10_bid=best_bid,
            x10_ask=best_ask,
            lighter_bid_qty=Decimal("0"),
            lighter_ask_qty=Decimal("0"),
            x10_bid_qty=bid_qty,
            x10_ask_qty=ask_qty,
            lighter_updated=None,
            x10_updated=depth.x10_updated,
        )
    except Exception as e:
        logger.debug(f"X10 depth L1 fallback for {symbol}: {e}")

    x10_symbol = self.market_data.get_x10_symbol(symbol)
    try:
        l1 = await self.x10.get_orderbook_l1(x10_symbol)
        if not isinstance(l1, dict):
            raise TypeError("x10.get_orderbook_l1 returned non-dict")
        best_bid = _safe_decimal(l1.get("best_bid"))
        best_ask = _safe_decimal(l1.get("best_ask"))
        bid_qty = _safe_decimal(l1.get("bid_qty"))
        ask_qty = _safe_decimal(l1.get("ask_qty"))
        now = datetime.now(UTC)
        if best_bid > 0 and best_ask > 0 and bid_qty > 0 and ask_qty > 0:
            return OrderbookSnapshot(
                symbol=symbol,
                lighter_bid=Decimal("0"),
                lighter_ask=Decimal("0"),
                x10_bid=best_bid,
                x10_ask=best_ask,
                lighter_bid_qty=Decimal("0"),
                lighter_ask_qty=Decimal("0"),
                x10_bid_qty=bid_qty,
                x10_ask_qty=ask_qty,
                lighter_updated=None,
                x10_updated=now,
            )
    except Exception as e:
        logger.debug(f"X10 L1 fetch failed for {symbol}: {e}")

    cached = self.market_data.get_orderbook(symbol)
    if cached and cached.x10_bid > 0 and cached.x10_ask > 0:
        return cached

    raise RuntimeError(f"No X10 orderbook snapshot for {symbol}")
