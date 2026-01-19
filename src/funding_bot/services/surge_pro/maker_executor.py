"""
Maker order executor for Surge Pro strategy.

Handles POST_ONLY order placement, fill monitoring, repricing, and taker fallback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.config.settings import SurgeProSettings

logger = get_logger(__name__)


@dataclass
class FillResult:
    """Result of a fill attempt."""

    filled: bool
    order: Order | None
    fill_price: Decimal | None
    fill_time_ms: float | None
    used_taker_fallback: bool = False


class MakerExecutor:
    """Executes maker orders with fill monitoring and taker fallback."""

    def __init__(self, *, settings: SurgeProSettings, x10):
        self.settings = settings
        self.x10 = x10

    def calc_maker_price(
        self,
        side: Side,
        best_bid: Decimal,
        best_ask: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        """Calculate aggressive maker price (1 tick better than best)."""
        if side == Side.BUY:
            # Place just above best bid for fill priority
            return best_bid + tick_size
        else:
            # Place just below best ask for fill priority
            return best_ask - tick_size

    async def place_maker_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        """Place a POST_ONLY maker order."""
        request = OrderRequest(
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )

        logger.info(
            "Placing maker order: symbol=%s side=%s qty=%s price=%s",
            symbol,
            side.value,
            qty,
            price,
        )

        return await self.x10.place_order(request)

    async def wait_for_fill(
        self,
        order: Order,
        timeout_s: float,
    ) -> FillResult:
        """Wait for order to fill within timeout."""
        start = time.monotonic()
        deadline = start + timeout_s

        while time.monotonic() < deadline:
            try:
                updated = await self.x10.get_order(order.symbol, order.order_id)
                if updated and updated.status == OrderStatus.FILLED:
                    fill_time = (time.monotonic() - start) * 1000
                    return FillResult(
                        filled=True,
                        order=updated,
                        fill_price=updated.avg_fill_price,
                        fill_time_ms=fill_time,
                    )
                if updated and updated.status.is_terminal():
                    # Cancelled, rejected, etc.
                    break
            except Exception as e:
                logger.debug("Fill check error: %s", e)

            await asyncio.sleep(0.1)

        return FillResult(filled=False, order=order, fill_price=None, fill_time_ms=None)

    async def cancel_order(self, order: Order) -> bool:
        """Cancel an unfilled order."""
        try:
            await self.x10.cancel_order(order.symbol, order.order_id)
            return True
        except Exception as e:
            logger.warning("Cancel failed: %s", e)
            return False

    async def place_taker_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        """Place an IOC taker order (fallback)."""
        request = OrderRequest(
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )

        logger.info(
            "Placing taker fallback: symbol=%s side=%s qty=%s price=%s",
            symbol,
            side.value,
            qty,
            price,
        )

        return await self.x10.place_order(request)
