"""
Fresh fetch methods for MarketDataService.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.services.market_data.models import (
    FundingSnapshot,
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration Dataclass
# ---------------------------------------------------------------------------


@dataclass
class OrderbookFetchConfig:
    """
    Configuration for orderbook fetch with retry settings.

    Controls retry behavior and staleness thresholds for fetching
    L1 and depth orderbook data from exchanges.

    Attributes:
        attempts: Number of retry attempts before giving up.
        base_delay: Base delay in seconds between retries (multiplied by attempt #).
        fallback_max_age: Max age in seconds for data to be considered valid.
    """

    attempts: int = 3
    base_delay: float = 0.3
    fallback_max_age: float = 10.0


def _load_orderbook_fetch_config(settings: Any) -> OrderbookFetchConfig:
    """
    Load orderbook fetch configuration from settings.

    Extracts retry parameters from websocket settings with clamping
    to prevent extreme values.

    Args:
        settings: Application settings with websocket configuration.

    Returns:
        OrderbookFetchConfig with validated parameters.
    """
    ws = settings.websocket
    attempts = int(getattr(ws, "orderbook_l1_retry_attempts", 3) or 3)
    attempts = max(1, min(attempts, 10))
    base_delay = float(getattr(ws, "orderbook_l1_retry_delay_seconds", Decimal("0.3")) or Decimal("0.3"))
    base_delay = max(0.0, min(base_delay, 5.0))
    fallback_max_age = float(getattr(ws, "orderbook_l1_fallback_max_age_seconds", Decimal("10.0")) or Decimal("10.0"))
    fallback_max_age = max(0.0, min(fallback_max_age, 120.0))

    return OrderbookFetchConfig(
        attempts=attempts,
        base_delay=base_delay,
        fallback_max_age=fallback_max_age,
    )


# ---------------------------------------------------------------------------
# Shared Helper Functions for Orderbook Processing
# ---------------------------------------------------------------------------


def _safe_decimal(book: dict, key: str) -> Decimal:
    """
    Safely extract a Decimal value from a book dict.

    Handles missing keys, None values, and invalid strings gracefully.

    Args:
        book: Dictionary containing orderbook data.
        key: Key to extract value for.

    Returns:
        Decimal value, or Decimal("0") if extraction fails.
    """
    v = book.get(key, Decimal("0"))
    if isinstance(v, Decimal):
        return v
    with contextlib.suppress(Exception):
        return Decimal(str(v))
    return Decimal("0")


def _book_has_depth(book: dict) -> bool:
    """
    Check if a single-exchange book has valid depth.

    Validates that bid < ask (non-inverted spread) and both
    bid and ask have positive quantities.

    Args:
        book: Dictionary with best_bid, best_ask, bid_qty, ask_qty.

    Returns:
        True if book has valid tradeable depth.
    """
    bid = _safe_decimal(book, "best_bid")
    ask = _safe_decimal(book, "best_ask")
    bid_qty = _safe_decimal(book, "bid_qty")
    ask_qty = _safe_decimal(book, "ask_qty")
    if bid <= 0 or ask <= 0 or bid_qty <= 0 or ask_qty <= 0:
        return False
    return bid < ask


def _has_any_price_data(book: dict) -> bool:
    """
    Return True if book has any valid price (bid OR ask).

    Less strict than _book_has_depth - only requires one side
    to have a positive price. Useful for partial data scenarios.

    Args:
        book: Dictionary with best_bid and/or best_ask.

    Returns:
        True if at least one price is positive.
    """
    bid = _safe_decimal(book, "best_bid")
    ask = _safe_decimal(book, "best_ask")
    return bid > 0 or ask > 0


def _merge_exchange_book(
    *,
    book: dict,
    prev_bid: Decimal,
    prev_ask: Decimal,
    prev_bid_qty: Decimal,
    prev_ask_qty: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    Merge fresh book data with previous snapshot.

    Prevents data poisoning by falling back to previous values when
    fresh data is zero/invalid. Also detects inverted spreads.

    Args:
        book: Fresh orderbook data dictionary.
        prev_bid: Previous best bid price.
        prev_ask: Previous best ask price.
        prev_bid_qty: Previous bid quantity.
        prev_ask_qty: Previous ask quantity.

    Returns:
        Tuple of (bid, ask, bid_qty, ask_qty) with fallbacks applied.
    """
    bid = _safe_decimal(book, "best_bid")
    ask = _safe_decimal(book, "best_ask")
    bid_qty = _safe_decimal(book, "bid_qty")
    ask_qty = _safe_decimal(book, "ask_qty")

    if bid <= 0:
        bid = prev_bid
    if ask <= 0:
        ask = prev_ask
    if bid_qty <= 0:
        bid_qty = prev_bid_qty
    if ask_qty <= 0:
        ask_qty = prev_ask_qty

    # If spread is inverted, fall back to previous
    if bid > 0 and ask > 0 and bid >= ask:
        bid, ask, bid_qty, ask_qty = prev_bid, prev_ask, prev_bid_qty, prev_ask_qty

    return bid, ask, bid_qty, ask_qty


def _snapshot_depth_ok(ob: OrderbookSnapshot, now: datetime, fallback_max_age: float) -> bool:
    """
    Check if snapshot has valid depth for opportunity scanning.

    Relaxed validation for low-volume symbols:
    - Accepts one-sided depth (bid OR ask, not both required)
    - Requires at least SOME price data from both exchanges
    - For arbitrage: ability to BUY on one exchange, SELL on the other
    """
    # For opportunity scanning, accept if AT LEAST ONE side has depth on BOTH exchanges
    lighter_has_depth = ob.lighter_bid_qty > 0 or ob.lighter_ask_qty > 0
    x10_has_depth = ob.x10_bid_qty > 0 or ob.x10_ask_qty > 0

    if not lighter_has_depth or not x10_has_depth:
        return False

    # Check tradeable directions
    can_long_lighter = ob.lighter_ask > 0 and ob.x10_bid > 0
    can_long_x10 = ob.x10_ask > 0 and ob.lighter_bid > 0

    if not can_long_lighter and not can_long_x10:
        return False

    # Validate spread only for sides that have data
    if ob.lighter_bid > 0 and ob.lighter_ask > 0 and ob.lighter_bid >= ob.lighter_ask:
        return False
    if ob.x10_bid > 0 and ob.x10_ask > 0 and ob.x10_bid >= ob.x10_ask:
        return False

    # Require timestamps from both exchanges
    if not ob.lighter_updated or not ob.x10_updated:
        return False

    # Check data freshness
    if fallback_max_age > 0:
        if (now - ob.lighter_updated).total_seconds() > fallback_max_age:
            return False
        if (now - ob.x10_updated).total_seconds() > fallback_max_age:
            return False

    return True


def _log_validation_failure(
    symbol: str,
    l_bid: Decimal,
    l_ask: Decimal,
    l_bq: Decimal,
    l_aq: Decimal,
    x_bid: Decimal,
    x_ask: Decimal,
    x_bq: Decimal,
    x_aq: Decimal,
    lighter_updated: datetime | None,
    x10_updated: datetime | None,
    now: datetime,
    fallback_max_age: float,
) -> None:
    """Log detailed validation failure reasons."""
    lighter_has_depth = l_bq > 0 or l_aq > 0
    x10_has_depth = x_bq > 0 or x_aq > 0
    can_long_lighter = l_ask > 0 and x_bid > 0
    can_long_x10 = x_ask > 0 and l_bid > 0

    fail_reasons = []
    if not lighter_has_depth:
        fail_reasons.append("lighter_no_depth")
    if not x10_has_depth:
        fail_reasons.append("x10_no_depth")
    if not can_long_lighter and not can_long_x10:
        fail_reasons.append("no_tradeable_direction")
    if l_bid > 0 and l_ask > 0 and l_bid >= l_ask:
        fail_reasons.append("lighter_inverted_spread")
    if x_bid > 0 and x_ask > 0 and x_bid >= x_ask:
        fail_reasons.append("x10_inverted_spread")
    if not lighter_updated or not x10_updated:
        fail_reasons.append("missing_timestamp")
    if lighter_updated and (now - lighter_updated).total_seconds() > fallback_max_age:
        fail_reasons.append(f"lighter_stale({(now - lighter_updated).total_seconds():.1f}s)")
    if x10_updated and (now - x10_updated).total_seconds() > fallback_max_age:
        fail_reasons.append(f"x10_stale({(now - x10_updated).total_seconds():.1f}s)")

    logger.debug(f"[{symbol}] Validation failed: {', '.join(fail_reasons) or 'unknown'}")


def _try_use_stale_cache(
    cache: dict,
    symbol: str,
    max_stale_age: float,
    cache_type: str,
    attempts: int,
) -> OrderbookSnapshot | OrderbookDepthSnapshot | None:
    """Try to use cached data if available and not too stale."""
    cached = cache.get(symbol)
    if not cached:
        return None

    cached_age_lighter = (
        (datetime.now(UTC) - cached.lighter_updated).total_seconds()
        if cached.lighter_updated else float("inf")
    )
    cached_age_x10 = (
        (datetime.now(UTC) - cached.x10_updated).total_seconds()
        if cached.x10_updated else float("inf")
    )
    max_cached_age = max(cached_age_lighter, cached_age_x10)

    if max_cached_age < max_stale_age:
        logger.warning(
            f"[{symbol}] Using stale {cache_type} data (age={max_cached_age:.1f}s, "
            f"max_allowed={max_stale_age:.1f}s) after {attempts} failed attempts"
        )
        return cached

    return None


# ---------------------------------------------------------------------------
# Depth Orderbook Helper Functions
# ---------------------------------------------------------------------------


def _normalize_depth_book(
    book: dict, depth_levels: int
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    """
    Normalize raw orderbook depth data to (price, qty) tuples.

    Converts raw orderbook data to standardized format, filters invalid
    entries, and sorts by price (bids descending, asks ascending).

    Args:
        book: Raw orderbook data with "bids" and "asks" lists.
        depth_levels: Maximum number of levels to include.

    Returns:
        Tuple of (bids, asks) where each is a list of (price, qty) tuples.
    """
    bids_raw = book.get("bids") or []
    asks_raw = book.get("asks") or []

    bids: list[tuple[Decimal, Decimal]] = []
    asks: list[tuple[Decimal, Decimal]] = []

    for p, q in list(bids_raw)[:depth_levels]:
        price = p if isinstance(p, Decimal) else Decimal(str(p))
        qty = q if isinstance(q, Decimal) else Decimal(str(q))
        if price > 0 and qty > 0:
            bids.append((price, qty))

    for p, q in list(asks_raw)[:depth_levels]:
        price = p if isinstance(p, Decimal) else Decimal(str(p))
        qty = q if isinstance(q, Decimal) else Decimal(str(q))
        if price > 0 and qty > 0:
            asks.append((price, qty))

    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return bids, asks


def _is_depth_ok(bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]) -> bool:
    """
    Check if depth data is valid (has bids and asks with correct spread).

    Args:
        bids: List of (price, qty) tuples for bid side.
        asks: List of (price, qty) tuples for ask side.

    Returns:
        True if both sides exist and best_bid < best_ask.
    """
    if not bids or not asks:
        return False
    best_bid, best_ask = bids[0][0], asks[0][0]
    return best_bid > 0 and best_ask > 0 and best_bid < best_ask


async def _fetch_depth_with_fallback(
    exchange_obj: ExchangePort, sym: str, depth_levels: int
) -> dict[str, list[tuple[Decimal, Decimal]]]:
    """
    Fetch depth from adapter; fall back to L1 if not available.

    Tries to get full depth data first; if adapter doesn't support it,
    falls back to L1 data formatted as single-level depth.

    Args:
        exchange_obj: Exchange adapter implementing ExchangePort.
        sym: Trading symbol.
        depth_levels: Number of depth levels to request.

    Returns:
        Dict with "bids" and "asks" lists of (price, qty) tuples.
    """
    if hasattr(exchange_obj, "get_orderbook_depth"):
        return await exchange_obj.get_orderbook_depth(sym, depth_levels)  # type: ignore[attr-defined]
    l1 = await exchange_obj.get_orderbook_l1(sym)
    best_bid = Decimal(str(l1.get("best_bid", "0")))
    best_ask = Decimal(str(l1.get("best_ask", "0")))
    bid_qty = Decimal(str(l1.get("bid_qty", "0")))
    ask_qty = Decimal(str(l1.get("ask_qty", "0")))
    bids = [(best_bid, bid_qty)] if best_bid > 0 and bid_qty > 0 else []
    asks = [(best_ask, ask_qty)] if best_ask > 0 and ask_qty > 0 else []
    return {"bids": bids, "asks": asks}


# ---------------------------------------------------------------------------
# Fresh Fetch Methods (Orchestrators)
# ---------------------------------------------------------------------------


async def get_fresh_price(self, symbol: str) -> PriceSnapshot:
    """Get fresh price data (forces API fetch)."""
    x10_symbol = self.get_x10_symbol(symbol)

    # Explicitly fetch fresh prices from adapters
    # This bypasses the service cache and forces adapter to get latest data
    lighter_price, x10_price = await asyncio.gather(
        self.lighter.get_mark_price(symbol),
        self.x10.get_mark_price(x10_symbol),
        return_exceptions=True
    )

    # Handle exceptions
    if isinstance(lighter_price, Exception):
        logger.warning(f"Failed to get fresh Lighter price for {symbol}: {lighter_price}")
        lighter_price = self._prices.get(symbol, PriceSnapshot(symbol)).lighter_price

    if isinstance(x10_price, Exception):
        logger.warning(f"Failed to get fresh X10 price for {symbol}: {x10_price}")
        x10_price = self._prices.get(symbol, PriceSnapshot(symbol)).x10_price

    # Update snapshot
    now = datetime.now(UTC)
    snapshot = PriceSnapshot(
        symbol=symbol,
        lighter_price=lighter_price or Decimal("0"),
        x10_price=x10_price or Decimal("0"),
        lighter_updated=now,
        x10_updated=now,
    )
    self._prices[symbol] = snapshot
    return snapshot


async def get_fresh_funding(self, symbol: str) -> FundingSnapshot:
    """Get fresh funding data."""
    await self._refresh_symbol(symbol)
    return self._funding.get(symbol) or FundingSnapshot(symbol=symbol)


async def get_fresh_orderbook(self, symbol: str) -> OrderbookSnapshot:
    """
    Get fresh L1 orderbook data from both exchanges.

    Uses retry logic with exponential backoff and graceful degradation
    for low-volume symbols with partial orderbook data.
    """
    x10_symbol = self.get_x10_symbol(symbol)
    config = _load_orderbook_fetch_config(self.settings)

    last_lighter_err: str | None = None
    last_x10_err: str | None = None
    prev = self._orderbooks.get(symbol) or OrderbookSnapshot(symbol=symbol)

    for attempt in range(config.attempts):
        now = datetime.now(UTC)

        # Fetch from both exchanges in parallel
        lighter_book_raw, x10_book_raw = await asyncio.gather(
            self.lighter.get_orderbook_l1(symbol),
            self.x10.get_orderbook_l1(x10_symbol),
            return_exceptions=True,
        )

        lighter_book: dict = {}
        x10_book: dict = {}

        if isinstance(lighter_book_raw, Exception):
            last_lighter_err = str(lighter_book_raw)
        elif isinstance(lighter_book_raw, dict):
            lighter_book = lighter_book_raw

        if isinstance(x10_book_raw, Exception):
            last_x10_err = str(x10_book_raw)
        elif isinstance(x10_book_raw, dict):
            x10_book = x10_book_raw

        # Merge with previous snapshot to avoid poisoning with partial/zero books
        l_bid, l_ask, l_bq, l_aq = _merge_exchange_book(
            book=lighter_book,
            prev_bid=prev.lighter_bid,
            prev_ask=prev.lighter_ask,
            prev_bid_qty=prev.lighter_bid_qty,
            prev_ask_qty=prev.lighter_ask_qty,
        )
        x_bid, x_ask, x_bq, x_aq = _merge_exchange_book(
            book=x10_book,
            prev_bid=prev.x10_bid,
            prev_ask=prev.x10_ask,
            prev_bid_qty=prev.x10_bid_qty,
            prev_ask_qty=prev.x10_ask_qty,
        )

        # Update timestamps if we received any valid price data
        lighter_updated = prev.lighter_updated
        x10_updated = prev.x10_updated

        if _book_has_depth(lighter_book) or _has_any_price_data(lighter_book):
            lighter_updated = now
        if _book_has_depth(x10_book) or _has_any_price_data(x10_book):
            x10_updated = now

        snapshot = OrderbookSnapshot(
            symbol=symbol,
            lighter_bid=l_bid,
            lighter_ask=l_ask,
            x10_bid=x_bid,
            x10_ask=x_ask,
            lighter_bid_qty=l_bq,
            lighter_ask_qty=l_aq,
            x10_bid_qty=x_bq,
            x10_ask_qty=x_aq,
            lighter_updated=lighter_updated,
            x10_updated=x10_updated,
        )
        prev = snapshot

        # Debug diagnostics
        if logger.isEnabledFor(10):
            logger.debug(
                f"[{symbol}] Orderbook attempt {attempt + 1}/{config.attempts}: "
                f"lighter(bid={l_bid}, ask={l_ask}, bq={l_bq}, aq={l_aq}) "
                f"x10(bid={x_bid}, ask={x_ask}, bq={x_bq}, aq={x_aq})"
            )

        # Check if snapshot is valid
        if _snapshot_depth_ok(snapshot, now, config.fallback_max_age):
            self._orderbooks[symbol] = snapshot
            return snapshot

        # Log validation failure on last attempt or debug mode
        if attempt == config.attempts - 1 or logger.isEnabledFor(10):
            _log_validation_failure(
                symbol, l_bid, l_ask, l_bq, l_aq, x_bid, x_ask, x_bq, x_aq,
                lighter_updated, x10_updated, now, config.fallback_max_age
            )

        # Retry with backoff
        if attempt < config.attempts - 1 and config.base_delay > 0:
            await asyncio.sleep(config.base_delay * (attempt + 1))

    # Graceful degradation: try stale cache
    max_stale_age = config.fallback_max_age * 3
    stale = _try_use_stale_cache(
        self._orderbooks, symbol, max_stale_age, "orderbook", config.attempts
    )
    if stale:
        return stale  # type: ignore[return-value]

    # Return partial data if available (normal for low-volume symbols)
    if prev and (prev.lighter_bid > 0 or prev.x10_bid > 0):
        logger.debug(
            f"[{symbol}] Using partial orderbook data after {config.attempts} attempts "
            f"(lighter_err={last_lighter_err}, x10_err={last_x10_err}). "
            f"This is expected for low-volume symbols."
        )
        self._orderbooks[symbol] = prev
        return prev

    # Truly no data
    logger.warning(
        f"Failed to fetch fresh data for candidate {symbol}: "
        f"No depth-valid orderbook snapshot after retries "
        f"(symbol={symbol}, attempts={config.attempts}, "
        f"fallback_max_age_s={config.fallback_max_age}, "
        f"lighter_err={last_lighter_err}, x10_err={last_x10_err})"
    )
    return OrderbookSnapshot(symbol=symbol)


async def get_fresh_orderbook_depth(self, symbol: str, *, levels: int) -> OrderbookDepthSnapshot:
    """
    Get top-N orderbook levels from both exchanges.

    Used for slippage/impact-aware depth gating beyond L1.
    """
    x10_symbol = self.get_x10_symbol(symbol)
    config = _load_orderbook_fetch_config(self.settings)

    # Clamp depth_levels to valid range
    depth_levels = max(1, min(int(levels or 1), 250))

    prev = self._orderbook_depth.get(symbol) or OrderbookDepthSnapshot(symbol=symbol)
    last_lighter_err: str | None = None
    last_x10_err: str | None = None

    for attempt in range(config.attempts):
        now = datetime.now(UTC)

        # Fetch depth from both exchanges in parallel
        lighter_raw, x10_raw = await asyncio.gather(
            _fetch_depth_with_fallback(self.lighter, symbol, depth_levels),
            _fetch_depth_with_fallback(self.x10, x10_symbol, depth_levels),
            return_exceptions=True,
        )

        lighter_book: dict = {}
        x10_book: dict = {}

        if isinstance(lighter_raw, Exception):
            last_lighter_err = str(lighter_raw)
        else:
            lighter_book = lighter_raw

        if isinstance(x10_raw, Exception):
            last_x10_err = str(x10_raw)
        else:
            x10_book = x10_raw

        # Normalize books
        l_bids, l_asks = _normalize_depth_book(lighter_book, depth_levels) if lighter_book else ([], [])
        x_bids, x_asks = _normalize_depth_book(x10_book, depth_levels) if x10_book else ([], [])

        # Handle timestamps
        lighter_updated = prev.lighter_updated
        x10_updated = prev.x10_updated

        if isinstance(lighter_book, dict) and lighter_book.get("updated_at"):
            lighter_updated = lighter_book["updated_at"]
        elif _is_depth_ok(l_bids, l_asks):
            lighter_updated = now

        if isinstance(x10_book, dict) and x10_book.get("updated_at"):
            x10_updated = x10_book["updated_at"]
        elif _is_depth_ok(x_bids, x_asks):
            x10_updated = now

        snapshot = OrderbookDepthSnapshot(
            symbol=symbol,
            lighter_bids=(l_bids or prev.lighter_bids),
            lighter_asks=(l_asks or prev.lighter_asks),
            x10_bids=(x_bids or prev.x10_bids),
            x10_asks=(x_asks or prev.x10_asks),
            lighter_updated=lighter_updated,
            x10_updated=x10_updated,
        )

        # Validate snapshot
        ok = _is_depth_ok(snapshot.lighter_bids, snapshot.lighter_asks) and _is_depth_ok(
            snapshot.x10_bids, snapshot.x10_asks
        )
        if ok and config.fallback_max_age > 0:
            if not snapshot.lighter_updated or not snapshot.x10_updated:
                ok = False
            elif (now - snapshot.lighter_updated).total_seconds() > config.fallback_max_age:
                ok = False
            elif (now - snapshot.x10_updated).total_seconds() > config.fallback_max_age:
                ok = False

        if ok:
            self._orderbook_depth[symbol] = snapshot
            return snapshot

        prev = snapshot
        if attempt < config.attempts - 1 and config.base_delay > 0:
            await asyncio.sleep(config.base_delay * (attempt + 1))

    # Graceful degradation: try stale cache (only if has both sides)
    max_stale_age = config.fallback_max_age * 3
    cached = self._orderbook_depth.get(symbol)
    if cached and cached.lighter_bids and cached.x10_bids:
        stale = _try_use_stale_cache(
            self._orderbook_depth, symbol, max_stale_age, "depth", config.attempts
        )
        if stale:
            return stale  # type: ignore[return-value]

    # Return partial data if available
    if prev and (prev.lighter_bids or prev.x10_bids):
        logger.debug(
            f"[{symbol}] Using partial depth data after {config.attempts} attempts. "
            f"Low-volume symbols often have limited orderbook depth."
        )
        self._orderbook_depth[symbol] = prev
        return prev

    logger.warning(
        f"[{symbol}] No depth-valid orderbook depth after retries "
        f"(attempts={config.attempts}, levels={depth_levels}, "
        f"lighter_err={last_lighter_err}, x10_err={last_x10_err})"
    )
    return OrderbookDepthSnapshot(symbol=symbol)
