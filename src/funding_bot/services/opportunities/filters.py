"""
Filter pipeline for opportunities.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.services.market_data import FundingSnapshot, OrderbookSnapshot, PriceSnapshot
from funding_bot.services.opportunities.types import FilterResult


def _apply_filters(
    self,
    symbol: str,
    price: PriceSnapshot,
    funding: FundingSnapshot,
    orderbook: OrderbookSnapshot | None,
) -> FilterResult:
    """Apply filter pipeline."""
    ts = self.settings.trading

    # 1. Blacklist
    if symbol in ts.blacklist_symbols:
        return FilterResult(False, "blacklisted")

    # 2. TradFi/FX check (optional)
    if self._is_tradfi_or_fx(symbol):
        return FilterResult(False, "tradfi/fx symbol")

    # 3. Price data check
    if price.mid_price == 0:
        return FilterResult(False, "no price data")

    # 4. Staleness check (moved up)
    if price.is_stale(max_age_seconds=60.0):
        return FilterResult(False, "stale price data")

    # 5. Minimum APY (moved up before spread check)
    if funding.apy < ts.min_apy_filter:
        return FilterResult(False, f"APY too low: {funding.apy:.2%}")

    # 6. Orderbook availability (required for spread calculation)
    if not orderbook:
        return FilterResult(False, "no orderbook data")

    # 7. Determine trade direction (needed for entry spread calculation)
    lighter_rate = funding.lighter_rate.rate_hourly if funding.lighter_rate else Decimal("0")
    x10_rate = funding.x10_rate.rate_hourly if funding.x10_rate else Decimal("0")

    if lighter_rate > x10_rate:
        # Plan: Long X10 (Hedge), Short Lighter (Maker)
        if orderbook.x10_ask <= 0:
            return FilterResult(False, "no X10 Ask liquidity for hedge")
    else:
        # Plan: Long Lighter (Maker), Short X10 (Hedge)
        if orderbook.x10_bid <= 0:
            return FilterResult(False, "no X10 Bid liquidity for hedge")

    # NOTE: Entry Spread Check is intentionally NOT done here!
    # Reason: Orderbook data in cache is often incomplete (especially for Lighter).
    # The REAL entry spread check happens in ExecutionEngine._execute_impl()
    # AFTER fetching fresh orderbook data (get_fresh_orderbook).
    # This prevents false rejections due to stale/missing orderbook data.
    #
    # See: execution.py lines 167-201 for the authoritative spread check.

    return FilterResult(True)


def _is_tradfi_or_fx(self, symbol: str) -> bool:
    """Check if symbol is TradFi or FX (not crypto)."""
    tradfi_indicators = [
        "XAU",
        "XAG",
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "SPY",
        "QQQ",
        "DJI",
        "NDX",
    ]

    symbol_upper = symbol.upper()
    return any(indicator in symbol_upper and "USD-" not in symbol_upper for indicator in tradfi_indicators)
