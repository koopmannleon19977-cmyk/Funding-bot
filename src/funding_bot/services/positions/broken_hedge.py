"""
Broken hedge detection.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.events import AlertEvent, BrokenHedgeDetected
from funding_bot.domain.models import Exchange, Trade, TradeStatus
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def _handle_broken_hedge_if_needed(self, trade: Trade) -> bool:
    """
    Detect "broken hedge" (one exchange position missing) and unwind.

    Returns True if a close was triggered successfully.
    """
    if trade.status != TradeStatus.OPEN or not trade.opened_at:
        return False

    now = datetime.now(UTC)
    if (now - trade.opened_at).total_seconds() < 30:
        return False

    qty_threshold = Decimal("0.0001")

    try:
        # Parallel fetch for speed
        lighter_pos, x10_pos = await asyncio.gather(
            self.lighter.get_position(trade.symbol),
            self.x10.get_position(trade.symbol),
            return_exceptions=True
        )

        # Handle exceptions from gather
        if isinstance(lighter_pos, Exception):
            lighter_pos = None
        if isinstance(x10_pos, Exception):
            x10_pos = None
    except Exception as e:
        logger.warning(
            f"Broken-hedge check skipped for {trade.symbol}: failed to fetch positions ({e})"
        )
        return False

    has_lighter = bool(lighter_pos and lighter_pos.qty > qty_threshold)
    has_x10 = bool(x10_pos and x10_pos.qty > qty_threshold)

    # Either both present (hedged) or both absent (already closed/liquidated).
    if has_lighter == has_x10:
        self._broken_hedge_hits.pop(trade.symbol, None)
        return False

    missing_exchange = Exchange.LIGHTER if not has_lighter else Exchange.X10

    prev = self._broken_hedge_hits.get(trade.symbol)
    if prev:
        prev_missing, hits, last_seen = prev
        if prev_missing != missing_exchange or (now - last_seen).total_seconds() > 120:
            hits = 0
        hits += 1
    else:
        hits = 1

    self._broken_hedge_hits[trade.symbol] = (missing_exchange, hits, now)

    if hits < 2:
        logger.warning(
            f"Potential broken hedge for {trade.symbol}: missing {missing_exchange.value} "
            f"(confirming {hits}/2)"
        )
        return False

    details = {
        "missing_exchange": missing_exchange.value,
        "trade_id": trade.trade_id,
        "lighter_qty": str(lighter_pos.qty) if lighter_pos else "0",
        "x10_qty": str(x10_pos.qty) if x10_pos else "0",
        "lighter_side": getattr(lighter_pos, "side", None).value if lighter_pos else None,
        "x10_side": getattr(x10_pos, "side", None).value if x10_pos else None,
        "lighter_liq": str(lighter_pos.liquidation_price) if (lighter_pos and lighter_pos.liquidation_price) else None,
        "x10_liq": str(x10_pos.liquidation_price) if (x10_pos and x10_pos.liquidation_price) else None,
    }
    logger.critical(f"BROKEN HEDGE confirmed for {trade.symbol}: {details}")

    self._broken_hedge_hits.pop(trade.symbol, None)

    # Determine remaining qty on the orphan leg
    remaining_qty = Decimal("0")
    if has_lighter:
        remaining_qty = lighter_pos.qty if lighter_pos else Decimal("0")
    elif has_x10:
        remaining_qty = x10_pos.qty if x10_pos else Decimal("0")

    # Alert immediately (critical) so user can react before/while unwind happens.
    with contextlib.suppress(Exception):
        await self.event_bus.publish(
            AlertEvent(
                level="CRITICAL",
                message=f"Broken hedge confirmed: {trade.symbol}",
                details=details,
            )
        )

    # Publish BrokenHedgeDetected event to trigger global trading pause
    with contextlib.suppress(Exception):
        await self.event_bus.publish(
            BrokenHedgeDetected(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                missing_exchange=missing_exchange,
                remaining_qty=remaining_qty,
                details=details,
            )
        )

    result = await self.close_trade(
        trade,
        reason=f"broken_hedge_missing_{missing_exchange.value.lower()}",
    )
    return result.success
