"""
Surge Pro Maker Engine.

Volume farming strategy using POST_ONLY orders for 0% fees.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.models import (
    Exchange,
    OrderRequest,
    OrderType,
    Side,
    SurgeTrade,
    SurgeTradeStatus,
    TimeInForce,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.surge_pro.maker_executor import MakerExecutor

if TYPE_CHECKING:
    from funding_bot.config.settings import Settings

logger = get_logger(__name__)
STRATEGY_NAME = "surge_pro_maker"


def compute_imbalance(bids: list, asks: list, depth: int = 10) -> Decimal:
    """Compute orderbook imbalance. Positive = more bids, negative = more asks."""
    bid_qty = sum(Decimal(str(b[1])) for b in bids[:depth])
    ask_qty = sum(Decimal(str(a[1])) for a in asks[:depth])
    total = bid_qty + ask_qty
    if total == 0:
        return Decimal("0")
    return (bid_qty - ask_qty) / total


def compute_spread_bps(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """Compute spread in basis points."""
    if best_bid <= 0:
        return Decimal("999999")
    return ((best_ask - best_bid) / best_bid) * 10000


class SurgeProMakerEngine:
    """Maker-based Surge Pro engine for volume farming."""

    def __init__(self, *, settings: Settings, x10, store):
        self.settings = settings
        self.x10 = x10
        self.store = store
        self.maker_executor = MakerExecutor(settings=settings.surge_pro, x10=x10)
        self._cooldowns: dict[str, float] = {}

    def _get_tick_size(self, symbol: str) -> Decimal:
        """Get tick size for symbol."""
        markets = getattr(self.x10, "_markets", {})
        market_name = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol
        market = markets.get(market_name)
        if market:
            return getattr(market, "tick_size", Decimal("0.01"))
        return Decimal("0.01")

    def _get_step_size(self, symbol: str) -> Decimal:
        """Get step size (qty increment) for symbol."""
        markets = getattr(self.x10, "_markets", {})
        market_name = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol
        market = markets.get(market_name)
        if market:
            return getattr(market, "step_size", Decimal("0.00001"))
        return Decimal("0.00001")

    async def _maybe_enter_maker(self, symbol: str) -> None:
        """Attempt maker entry for symbol."""
        # Get orderbook
        depth = await self.x10.get_orderbook_depth(symbol, 10)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []

        # Check imbalance
        imbalance = compute_imbalance(bids, asks, 10)
        threshold = self.settings.surge_pro.entry_imbalance_threshold

        side: Side | None = None
        if imbalance >= threshold:
            side = Side.BUY
        elif imbalance <= -threshold:
            side = Side.SELL

        if side is None:
            logger.debug("No entry signal for %s: imbalance=%s", symbol, imbalance)
            return

        # Get L1 for pricing
        l1 = await self.x10.get_orderbook_l1(symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))

        # Check spread
        spread_bps = compute_spread_bps(best_bid, best_ask)
        if spread_bps > self.settings.surge_pro.max_spread_bps:
            logger.debug("Spread too wide for %s: %s bps", symbol, spread_bps)
            return

        # Calculate maker price
        tick_size = self._get_tick_size(symbol)
        maker_price = self.maker_executor.calc_maker_price(
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
        )

        # Calculate quantity
        notional = self.settings.surge_pro.max_trade_notional_usd
        mid = (best_bid + best_ask) / 2
        qty = notional / mid
        step_size = self._get_step_size(symbol)
        qty = (qty / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size

        if qty <= 0:
            return

        # Place POST_ONLY order
        client_order_id = f"surge_maker_entry_{uuid.uuid4().hex[:8]}"

        order = await self.x10.place_order(
            OrderRequest(
                symbol=symbol,
                exchange=Exchange.X10,
                side=side,
                qty=qty,
                order_type=OrderType.LIMIT,
                price=maker_price,
                time_in_force=TimeInForce.POST_ONLY,
                client_order_id=client_order_id,
            )
        )

        # Wait for fill
        timeout = self.settings.surge_pro.maker_entry_timeout_s
        result = await self.maker_executor.wait_for_fill(order, timeout)

        if result.filled:
            # Create trade record
            trade = SurgeTrade(
                trade_id=str(uuid.uuid4()),
                symbol=symbol,
                exchange=Exchange.X10,
                side=side,
                qty=result.order.filled_qty if result.order else qty,
                entry_price=result.fill_price or maker_price,
                status=SurgeTradeStatus.OPEN,
                opened_at=datetime.now(UTC),
                entry_order_id=order.order_id,
                entry_client_order_id=client_order_id,
            )
            await self.store.create_surge_trade(trade)
            logger.info(
                "Maker entry filled: symbol=%s side=%s price=%s fill_time=%sms",
                symbol,
                side.value,
                result.fill_price,
                result.fill_time_ms,
            )
        else:
            # Cancel unfilled order
            await self.maker_executor.cancel_order(order)
            logger.debug("Maker entry not filled, cancelled: symbol=%s", symbol)
