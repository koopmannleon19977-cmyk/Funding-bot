"""
Lighter Local Orderbook Implementation.

Maintains a local in-memory orderbook synchronized via WebSocket updates.
Logic mirrors `.tmp/perp-dex-tools/exchanges/lighter_custom_websocket.py` but uses
Decimal for precision and integrates with the Clean Architecture.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = logging.getLogger(__name__)

# Tolerance period for nonce gaps after initial WS connection (in seconds)
# Lighter WS doesn't send an initial snapshot, so the first message often has a gap.
_INITIAL_SYNC_TOLERANCE_SECONDS = 10.0


class LocalOrderbook:
    """
    Maintains a local copy of the orderbook for a specific symbol/market.
    Handles snapshots, incremental updates, and integrity checks.
    """

    def __init__(self, symbol: str, market_id: int):
        self.symbol = symbol
        self.market_id = market_id

        # Core storage: Dict[price: Decimal, size: Decimal]
        # We rely on Python's robust dicts; sorting happens on read.
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

        # Continuity tracking (Reference: .tmp/lighter_ws.html)
        self.last_nonce: int | None = None
        self.last_offset: int | None = None

        self.snapshot_loaded = False
        self._updated_at: float = 0.0
        self._update_counter: int = 0

        # Track connection start for initial sync tolerance
        self._connection_start = time.monotonic()
        self._initial_nonce_gap_logged = False  # Prevent duplicate logs during initial sync

    def apply_snapshot(self, data: dict[str, Any]) -> None:
        """
        Apply a full snapshot (subscribed/order_book).
        Clears existing state and repopulates.
        """
        self.bids.clear()
        self.asks.clear()

        # Update metadata
        self.last_nonce = int(data.get("nonce", 0)) if data.get("nonce") is not None else None
        self.last_offset = int(data.get("offset", 0)) if data.get("offset") is not None else None

        # Populate
        raw_bids = data.get("bids", []) or []
        raw_asks = data.get("asks", []) or []

        self._apply_batch(self.bids, raw_bids)
        self._apply_batch(self.asks, raw_asks)

        self.snapshot_loaded = True
        logger.debug(
            f"[{self.symbol}] Snapshot applied: {len(self.bids)} bids, {len(self.asks)} asks (nonce={self.last_nonce})"
        )

    def apply_update(self, data: dict[str, Any]) -> None:
        """
        Apply an incremental update (update/order_book).
        Checks continuity (nonce, offset) before applying.
        Raises ExchangeError if a gap is detected (caller should resync).
        """
        if not self.snapshot_loaded:
            return

        # Continuity Checks
        new_nonce = int(data.get("nonce", 0)) if data.get("nonce") is not None else None
        begin_nonce = int(data.get("begin_nonce", 0)) if data.get("begin_nonce") is not None else None
        new_offset = int(data.get("offset", 0)) if data.get("offset") is not None else None

        # 1. Nonce Chain Check (Graceful - trigger resync instead of exception)
        if self.last_nonce is not None and begin_nonce is not None and begin_nonce != self.last_nonce:
            # Check if we're in initial sync tolerance period
            connection_age = time.monotonic() - self._connection_start
            if connection_age <= _INITIAL_SYNC_TOLERANCE_SECONDS:
                # Initial sync gap - expected, log only once per connection
                if not self._initial_nonce_gap_logged:
                    logger.info(
                        f"[{self.symbol}] Initial nonce gap (expected during sync): "
                        f"begin_nonce={begin_nonce} != last_nonce={self.last_nonce}. "
                        f"Accepting and resetting nonce chain."
                    )
                    self._initial_nonce_gap_logged = True
                # Accept the message and reset nonce chain to prevent future gaps
                # This resets our last_nonce to the server's begin_nonce
                self.last_nonce = begin_nonce
            else:
                # Real gap after stable connection - log as warning and resync
                logger.warning(
                    f"Lighter WS Integrity Error: Orderbook Gap (Nonce): "
                    f"begin_nonce={begin_nonce} != last_nonce={self.last_nonce}. Resynchronizing..."
                )
                self.snapshot_loaded = False  # Trigger resync on next snapshot
                return  # Don't apply partial update with gap

        # 2. Offset Chain Check (Weak/Secondary)
        # Offsets usually increment by 1, but sometimes by more?
        # .tmp logic: "if new_offset < expected: Gap".
        if self.last_offset is not None and new_offset is not None:
            if new_offset <= self.last_offset:
                # Duplicate or old message?
                logger.debug(f"[{self.symbol}] Ignoring old/duplicate offset: {new_offset} <= {self.last_offset}")
                return
            elif new_offset > self.last_offset + 1:
                # NOTE: Lighter offsets are not a strict +1 sequence in practice; large jumps are common,
                # especially when the server aggregates updates. The authoritative continuity check is
                # the nonce chain (`begin_nonce == last_nonce`).
                logger.debug(
                    f"[{self.symbol}] Non-fatal orderbook offset jump: offset={new_offset} "
                    f"jumped from last_offset={self.last_offset}"
                )

        # Apply Updates
        raw_bids = data.get("bids", []) or []
        raw_asks = data.get("asks", []) or []

        self._apply_batch(self.bids, raw_bids)
        self._apply_batch(self.asks, raw_asks)

        # Update pointers
        if new_nonce is not None:
            self.last_nonce = new_nonce
        if new_offset is not None:
            self.last_offset = new_offset

        # Cleanup (Prevent memory leak for deep levels)
        # Optimized: Only cleanup every 100 updates to save CPU
        self._update_counter += 1
        if self._update_counter >= 100:
            self._cleanup_levels()
            # Post-update Integrity Check (Crossed Book) - Throttled
            self._check_integrity()
            self._update_counter = 0

    def _apply_batch(self, book_side: dict[Decimal, Decimal], updates: list[Any]) -> None:
        """Helper to apply a list of updates {price, size} to a side."""
        for u in updates:
            try:
                # Format is usually {'price': '...', 'size': '...'}
                # But SDK/WS might send different keys?
                # .tmp handles dict key errors.
                if not isinstance(u, dict):
                    continue

                price_raw = u.get("price")
                size_raw = u.get("size")

                if price_raw is None or size_raw is None:
                    continue

                price = _safe_decimal(price_raw)
                size = _safe_decimal(size_raw)

                if size == 0:
                    book_side.pop(price, None)
                else:
                    book_side[price] = size
            except Exception:
                continue

    def _check_integrity(self) -> None:
        """
        Check if Best Bid >= Best Ask (Crossed Book).
        If crossed, it usually means we missed an update or processed out of order.
        """
        if not self.bids or not self.asks:
            return

        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())

        # Use epsilon tolerance to avoid false positives when bid == ask (locked book)
        epsilon = Decimal("0.0000001")  # 7 decimal places precision
        if best_bid > best_ask + epsilon:
            logger.warning(
                f"[{self.symbol}] Crossed Orderbook detected! Bid={best_bid} > Ask={best_ask}. Triggering resync..."
            )
            self.snapshot_loaded = False  # Trigger resync instead of exception

    def _cleanup_levels(self, max_levels: int = 200) -> None:
        """Keep only top N levels to restrict memory usage."""
        if len(self.bids) > max_levels:
            # Keep highest bids
            sorted_bids = sorted(self.bids.keys(), reverse=True)[:max_levels]
            # Rebuild dict? Or pop others? Rebuild is often cleaner/faster for bulk drop.
            self.bids = {p: self.bids[p] for p in sorted_bids}

        if len(self.asks) > max_levels:
            # Keep lowest asks
            sorted_asks = sorted(self.asks.keys())[:max_levels]
            self.asks = {p: self.asks[p] for p in sorted_asks}

        # Cleanup counter to avoid sorting on every update
        self._update_counter = 0

    def get_l1(self) -> dict[str, Decimal]:
        """Get Best Bid/Ask and sizes (Raw)."""
        return self.get_effective_l1(min_notional=Decimal("0"))

    def get_effective_l1(self, min_notional: Decimal = Decimal("10")) -> dict[str, Decimal]:
        """
        Get 'Smart' Best Bid/Ask that ignores dust orders smaller than min_notional.
        This prevents pricing decisions based on $1 front-running orders.
        Reference: .tmp/lighter_custom_websocket.py (Smart L1 ~ $400 threshold)
        """
        best_bid = Decimal("0")
        best_ask = Decimal("0")
        bid_qty = Decimal("0")
        ask_qty = Decimal("0")

        # Find best bid > min_notional
        if self.bids:
            # Sort desc (highest price first)
            for price in sorted(self.bids.keys(), reverse=True):
                qty = self.bids[price]
                if (price * qty) >= min_notional:
                    best_bid = price
                    bid_qty = qty
                    break

            # Fallback if all are dust: use raw top
            if best_bid == 0 and self.bids:
                best_bid = max(self.bids.keys())
                bid_qty = self.bids[best_bid]

        # Find best ask > min_notional
        if self.asks:
            # Sort asc (lowest price first)
            for price in sorted(self.asks.keys()):
                qty = self.asks[price]
                if (price * qty) >= min_notional:
                    best_ask = price
                    ask_qty = qty
                    break

            # Fallback
            if best_ask == 0 and self.asks:
                best_ask = min(self.asks.keys())
                ask_qty = self.asks[best_ask]

        return {"best_bid": best_bid, "best_ask": best_ask, "bid_qty": bid_qty, "ask_qty": ask_qty}

    def get_depth(self, limit: int = 20) -> dict[str, list[tuple[Decimal, Decimal]]]:
        """Get depth (bids/asks sorted)."""
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:limit]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:limit]
        return {"bids": bids, "asks": asks}
