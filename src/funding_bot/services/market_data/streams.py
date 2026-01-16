"""
Market data streaming helpers.
"""

from __future__ import annotations

import asyncio
import contextlib

from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort

logger = get_logger(__name__)


async def _start_market_data_streams(self) -> None:
    """Start WS orderbook streams for near-real-time L1 (best-effort)."""
    ws = self.settings.websocket
    if not getattr(ws, "market_data_streams_enabled", False):
        return

    # `orderbook_stream_depth` is best-effort and provider-specific:
    # - X10 supports full book (no depth param) OR `depth=1` best bid/ask (snapshots)
    # - Lighter ignores this (subscription is by `order_book_ids`)
    depth_setting = int(getattr(ws, "orderbook_stream_depth", 1) or 1)
    depth: int | None = None if depth_setting <= 0 else max(1, min(depth_setting, 50))

    # Lighter uses base symbol format ("ETH"), X10 uses ("ETH-USD").
    # Safety: only start streams for an explicit allowlist. Starting streams for every common
    # symbol can spawn dozens of per-symbol WS connections and trigger Lighter rate limits.
    raw_symbols = list(getattr(ws, "market_data_streams_symbols", []) or [])
    normalized: list[str] = []
    for s in raw_symbols:
        try:
            sym = str(s).strip()
        except Exception:
            continue
        if not sym:
            continue
        normalized.append(sym.replace("-USD", ""))

    max_symbols = int(getattr(ws, "market_data_streams_max_symbols", 0) or 0)
    if max_symbols > 0:
        normalized = normalized[: max_symbols]

    lighter_symbols = sorted(set(normalized) & set(self._common_symbols))
    if not lighter_symbols:
        logger.info(
            "market_data_streams_enabled=true but no valid symbols configured; "
            "skipping MarketDataService streams (use on-demand per-symbol WS)."
        )
        return

    async def _start_one(exchange: ExchangePort, symbols: list[str] | None) -> None:
        with contextlib.suppress(Exception):
            await exchange.subscribe_orderbook_l1(symbols, depth=depth)

    # Start Lighter orderbook streams (on-demand per-symbol WS under the hood).
    #
    # NOTE: We intentionally do NOT start X10 orderbooks WS here.
    # Extended/X10 orderbooks stream is stable when subscribing per-market (single symbol),
    # but subscribing to the ALL-markets stream is very high throughput and tends to disconnect.
    # We rely on REST batch refresh for X10 market data and enable per-market WS only on-demand.
    await _start_one(self.lighter, lighter_symbols)


def _schedule_lighter_mark_price_trickle(self) -> None:
    """Schedule a small trickle of real Lighter mark prices without blocking refresh_all()."""
    if self._lighter_trickle_task and not self._lighter_trickle_task.done():
        return

    symbols = list(self._common_symbols)
    if not symbols:
        return

    per_cycle = min(10, len(symbols))
    start = self._lighter_price_rr_offset % len(symbols)
    chosen = [symbols[(start + i) % len(symbols)] for i in range(per_cycle)]
    self._lighter_price_rr_offset = (start + per_cycle) % len(symbols)

    self._lighter_trickle_task = asyncio.create_task(
        self._run_lighter_mark_price_trickle(chosen),
        name="lighter_mark_price_trickle",
    )


async def _run_lighter_mark_price_trickle(self, symbols: list[str]) -> None:
    """Background task to refresh a few Lighter mark prices (Premium-friendly)."""
    sem = asyncio.Semaphore(3)

    async def one(sym: str) -> None:
        async with sem:
            try:
                # get_mark_price() also populates a minimal L1 cache via orderBookOrders(limit=1)
                await asyncio.wait_for(self.lighter.get_mark_price(sym), timeout=3.0)
            except Exception as e:
                logger.debug(f"Trickle mark_price failed for {sym}: {e}")

    try:
        await asyncio.gather(*(one(sym) for sym in symbols), return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"Lighter mark price trickle failed: {e}")
