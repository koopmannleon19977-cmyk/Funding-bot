"""
Adapter cache hydration for MarketDataService.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.services.market_data.models import OrderbookSnapshot, PriceSnapshot


def _hydrate_symbol_from_adapter_cache(self, symbol: str) -> None:
    """
    Best-effort fast-path hydration from adapter caches.

    This enables "Turbo mode" when adapters keep their caches hot via WS streams:
    reading from these dicts is effectively free and avoids waiting for refresh_all().
    """
    if not symbol:
        return

    x10_symbol = self._symbol_map_to_x10.get(symbol, f"{symbol}-USD")
    now = datetime.now(UTC)

    prev_price = self._prices.get(symbol) or PriceSnapshot(symbol=symbol)
    prev_ob = self._orderbooks.get(symbol) or OrderbookSnapshot(symbol=symbol)

    lighter_price = getattr(self.lighter, "_price_cache", {}).get(symbol, Decimal("0"))
    x10_price = getattr(self.x10, "_price_cache", {}).get(x10_symbol, Decimal("0"))

    if lighter_price == Decimal("0") and x10_price > Decimal("0"):
        lighter_price = x10_price

    lighter_price_ts = getattr(self.lighter, "_price_updated_at", {}).get(symbol)
    x10_price_ts = getattr(self.x10, "_price_updated_at", {}).get(x10_symbol)

    self._prices[symbol] = PriceSnapshot(
        symbol=symbol,
        lighter_price=lighter_price or prev_price.lighter_price,
        x10_price=x10_price or prev_price.x10_price,
        lighter_updated=lighter_price_ts or prev_price.lighter_updated,
        x10_updated=x10_price_ts or prev_price.x10_updated,
    )

    lighter_book = getattr(self.lighter, "_orderbook_cache", {}).get(symbol, {})
    x10_book = getattr(self.x10, "_orderbook_cache", {}).get(x10_symbol, {})

    def _d(book: dict, key: str) -> Decimal:
        v = book.get(key, Decimal("0"))
        if isinstance(v, Decimal):
            return v
        with contextlib.suppress(Exception):
            return Decimal(str(v))
        return Decimal("0")

    l_bid = _d(lighter_book, "best_bid") or prev_ob.lighter_bid
    l_ask = _d(lighter_book, "best_ask") or prev_ob.lighter_ask
    x_bid = _d(x10_book, "best_bid") or prev_ob.x10_bid
    x_ask = _d(x10_book, "best_ask") or prev_ob.x10_ask
    l_bq = _d(lighter_book, "bid_qty") or prev_ob.lighter_bid_qty
    l_aq = _d(lighter_book, "ask_qty") or prev_ob.lighter_ask_qty
    x_bq = _d(x10_book, "bid_qty") or prev_ob.x10_bid_qty
    x_aq = _d(x10_book, "ask_qty") or prev_ob.x10_ask_qty

    lighter_ob_ts = getattr(self.lighter, "_orderbook_updated_at", {}).get(symbol)
    x10_ob_ts = getattr(self.x10, "_orderbook_updated_at", {}).get(x10_symbol)

    # If we got fresh depth-valid values but no timestamps, use "now" as a fallback.
    if not lighter_ob_ts and l_bid > 0 and l_ask > 0 and l_bid < l_ask:
        lighter_ob_ts = now
    if not x10_ob_ts and x_bid > 0 and x_ask > 0 and x_bid < x_ask:
        x10_ob_ts = now

    self._orderbooks[symbol] = OrderbookSnapshot(
        symbol=symbol,
        lighter_bid=l_bid,
        lighter_ask=l_ask,
        x10_bid=x_bid,
        x10_ask=x_ask,
        lighter_bid_qty=l_bq,
        lighter_ask_qty=l_aq,
        x10_bid_qty=x_bq,
        x10_ask_qty=x_aq,
        lighter_updated=lighter_ob_ts or prev_ob.lighter_updated,
        x10_updated=x10_ob_ts or prev_ob.x10_updated,
    )
