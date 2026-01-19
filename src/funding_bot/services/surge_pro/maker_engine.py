"""
Surge Pro Maker Engine.

Volume farming strategy using POST_ONLY orders for 0% fees.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SurgeTrade,
    SurgeTradeStatus,
    TimeInForce,
)
from funding_bot.observability.logging import LOG_TAG_PROFIT, get_logger
from funding_bot.services.surge_pro.maker_executor import MakerExecutor
from funding_bot.services.surge_pro.risk_guard import RiskGuard

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
        self.risk_guard = RiskGuard(settings=settings.surge_pro, store=store)
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
            self._set_cooldown(symbol)
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

    async def _close_trade_maker(self, trade: SurgeTrade, reason: str) -> None:
        """Close trade using maker orders with taker fallback."""
        exit_side = trade.side.inverse()

        # Get current prices
        l1 = await self.x10.get_orderbook_l1(trade.symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))
        tick_size = self._get_tick_size(trade.symbol)

        max_retries = self.settings.surge_pro.maker_exit_max_retries
        timeout = self.settings.surge_pro.maker_exit_timeout_s

        for attempt in range(max_retries):
            # Refresh prices for repricing
            if attempt > 0:
                l1 = await self.x10.get_orderbook_l1(trade.symbol)
                best_bid = Decimal(str(l1.get("best_bid", "0")))
                best_ask = Decimal(str(l1.get("best_ask", "0")))

            maker_price = self.maker_executor.calc_maker_price(
                side=exit_side,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )

            client_order_id = f"surge_maker_exit_{uuid.uuid4().hex[:8]}"

            order = await self.x10.place_order(
                OrderRequest(
                    symbol=trade.symbol,
                    exchange=Exchange.X10,
                    side=exit_side,
                    qty=trade.qty,
                    order_type=OrderType.LIMIT,
                    price=maker_price,
                    time_in_force=TimeInForce.POST_ONLY,
                    reduce_only=True,
                    client_order_id=client_order_id,
                )
            )

            result = await self.maker_executor.wait_for_fill(order, timeout)

            if result.filled:
                await self._finalize_close(trade, reason, result.fill_price, result.order)
                logger.info(
                    "Maker exit filled: symbol=%s reason=%s attempt=%d",
                    trade.symbol,
                    reason,
                    attempt + 1,
                )
                return

            # Cancel and retry
            await self.maker_executor.cancel_order(order)
            logger.debug("Maker exit attempt %d failed, retrying", attempt + 1)

        # Taker fallback
        logger.warning("Maker exit exhausted, using taker fallback: symbol=%s", trade.symbol)
        await self._close_trade_taker(trade, reason)

    async def _close_trade_taker(self, trade: SurgeTrade, reason: str) -> None:
        """Close trade using IOC taker order (fallback)."""
        exit_side = trade.side.inverse()

        l1 = await self.x10.get_orderbook_l1(trade.symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))

        # Aggressive taker price with slippage (0.5%)
        price = best_ask * Decimal("1.005") if exit_side == Side.BUY else best_bid * Decimal("0.995")

        client_order_id = f"surge_taker_exit_{uuid.uuid4().hex[:8]}"

        order = await self.x10.place_order(
            OrderRequest(
                symbol=trade.symbol,
                exchange=Exchange.X10,
                side=exit_side,
                qty=trade.qty,
                order_type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
                client_order_id=client_order_id,
            )
        )

        if order.status == OrderStatus.FILLED:
            await self._finalize_close(trade, reason, order.avg_fill_price, order)
        else:
            # TODO: Handle partial fills and failed taker exits
            # For now, log error - trade remains OPEN for manual intervention
            logger.error("Taker exit failed: symbol=%s status=%s", trade.symbol, order.status)

    async def _finalize_close(
        self,
        trade: SurgeTrade,
        reason: str,
        exit_price: Decimal | None,
        order: Order | None,
    ) -> None:
        """Finalize trade closure."""
        price = exit_price or trade.entry_price
        fees = trade.fees + (order.fee if order and order.fee else Decimal("0"))

        closed_trade = replace(
            trade,
            exit_price=price,
            fees=fees,
            status=SurgeTradeStatus.CLOSED,
            exit_reason=reason,
            closed_at=datetime.now(UTC),
            exit_order_id=order.order_id if order else None,
        )

        await self.store.update_surge_trade(closed_trade)

        # Calculate PnL
        if trade.side == Side.BUY:
            pnl = (price - trade.entry_price) * trade.qty
        else:
            pnl = (trade.entry_price - price) * trade.qty

        net_pnl = pnl - fees

        logger.info(
            "%s Surge Pro close: symbol=%s reason=%s pnl=%s net=%s fees=%s",
            LOG_TAG_PROFIT,
            trade.symbol,
            reason,
            pnl,
            net_pnl,
            fees,
        )

    async def tick(self) -> None:
        """Main loop tick - scan for entries and manage exits."""
        # Check risk limits
        risk_result = await self.risk_guard.check()
        if risk_result.blocked:
            logger.debug("Tick blocked by RiskGuard: %s", risk_result.reason)
            return

        # Check open trades for exits
        open_trades = await self.store.list_open_surge_trades()
        for trade in open_trades:
            await self._maybe_exit(trade)

        # Check for new entries
        if len(open_trades) >= self.settings.surge_pro.max_open_trades:
            return

        open_symbols = {t.symbol for t in open_trades}
        for symbol in self.settings.surge_pro.symbols:
            if symbol in open_symbols:
                continue
            if self._is_on_cooldown(symbol):
                continue
            await self._maybe_enter_maker(symbol)

    async def _maybe_exit(self, trade: SurgeTrade) -> None:
        """Check if trade should be exited."""
        if trade.status != SurgeTradeStatus.OPEN:
            return

        # Get current price
        try:
            mark_price = await self.x10.get_mark_price(trade.symbol)
        except Exception:
            l1 = await self.x10.get_orderbook_l1(trade.symbol)
            bid = Decimal(str(l1.get("best_bid", "0")))
            ask = Decimal(str(l1.get("best_ask", "0")))
            mark_price = (bid + ask) / 2

        if mark_price <= 0:
            return

        # Calculate PnL in bps
        if trade.side == Side.BUY:
            pnl_bps = ((mark_price - trade.entry_price) / trade.entry_price) * 10000
        else:
            pnl_bps = ((trade.entry_price - mark_price) / trade.entry_price) * 10000

        # Check stop loss
        if pnl_bps <= -self.settings.surge_pro.stop_loss_bps:
            await self._close_trade_taker(trade, "STOP_LOSS")  # Immediate taker for SL
            return

        # Check take profit
        if pnl_bps >= self.settings.surge_pro.take_profit_bps:
            await self._close_trade_maker(trade, "TAKE_PROFIT")
            return

        # Check signal flip
        depth = await self.x10.get_orderbook_depth(trade.symbol, 10)
        imbalance = compute_imbalance(depth.get("bids", []), depth.get("asks", []), 10)
        exit_threshold = self.settings.surge_pro.exit_imbalance_threshold

        if (
            trade.side == Side.BUY
            and imbalance <= -exit_threshold
            or trade.side == Side.SELL
            and imbalance >= exit_threshold
        ):
            await self._close_trade_maker(trade, "SIGNAL_FLIP")

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if symbol is on cooldown."""
        cooldown_until = self._cooldowns.get(symbol)
        if not cooldown_until:
            return False
        if time.time() >= cooldown_until:
            del self._cooldowns[symbol]
            return False
        return True

    def _set_cooldown(self, symbol: str) -> None:
        """Set cooldown for symbol."""
        self._cooldowns[symbol] = time.time() + self.settings.surge_pro.cooldown_seconds
