"""
Funding Tracker.

Tracks and records funding payments for open trades.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.config.settings import Settings
from funding_bot.domain.events import FundingCollected
from funding_bot.domain.models import Exchange, Trade, TradeStatus
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort
from funding_bot.services.market_data import MarketDataService

logger = get_logger(__name__)


class FundingTracker:
    """
    Tracks realized funding payments for open positions.

    Periodically:
    - Fetches funding data from exchanges
    - Updates trade funding_collected
    - Saves PnL snapshots
    """

    def __init__(
        self,
        settings: Settings,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: TradeStorePort,
        event_bus: EventBusPort,
        market_data: MarketDataService,
    ):
        self.settings = settings
        self.lighter = lighter
        self.x10 = x10
        self.store = store
        self.event_bus = event_bus
        self.market_data = market_data

        self._running = False
        self._task: asyncio.Task | None = None
        self._update_interval = 30.0  # 30 seconds for faster dashboard updates

    async def start(self) -> None:
        """Start the funding tracker."""
        if self._running:
            return

        logger.info("Starting FundingTracker...")
        self._running = True
        self._task = asyncio.create_task(
            self._track_loop(),
            name="funding_tracker"
        )

    async def stop(self) -> None:
        """Stop the funding tracker."""
        self._running = False

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        logger.info("FundingTracker stopped")

    async def _track_loop(self) -> None:
        """Background loop to track funding and PnL."""
        # Create separate tasks for funding (slow) and PnL (fast)
        funding_task = asyncio.create_task(self._funding_loop())
        pnl_task = asyncio.create_task(self._pnl_loop())

        try:
            await asyncio.gather(funding_task, pnl_task)
        except asyncio.CancelledError:
            funding_task.cancel()
            pnl_task.cancel()
            await asyncio.gather(funding_task, pnl_task, return_exceptions=True)

    async def _funding_loop(self) -> None:
        """Slow loop for realized funding updates."""
        while self._running:
            try:
                await self.update_all_trades()
                # Record current funding rates for historical analytics
                await self._record_funding_history()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Funding update error: {e}")

            await asyncio.sleep(self._update_interval)

    async def _pnl_loop(self) -> None:
        """Fast loop for unrealized PnL updates."""
        while self._running:
            try:
                open_trades = await self.store.list_open_trades()
                for trade in open_trades:
                    # Update PnL in memory and DB (no snapshot)
                    await self.save_pnl_update(trade)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"PnL update error: {e}")

            # Update PnL every 5 seconds for dashboard responsiveness
            await asyncio.sleep(5.0)

    async def update_all_trades(self) -> None:
        """Update funding for all open trades."""
        open_trades = await self.store.list_open_trades()

        for trade in open_trades:
            if trade.status != TradeStatus.OPEN:
                continue

            try:
                await self._update_trade_funding(trade)
            except Exception as e:
                logger.warning(f"Failed to update funding for {trade.symbol}: {e}")

    async def _update_trade_funding(self, trade: Trade) -> None:
        """Update funding for a single trade using realized values from exchanges."""
        # Start time for fetching history
        start_time = trade.opened_at or trade.created_at

        # Best-effort: backfill missing fees so funding adjustments (X10) and PnL are correct.
        await self._maybe_refresh_leg_fees(trade)

        # Fetch realized funding from both exchanges
        # Note: These operations might raise ExchangeError. We let them propagate
        # up to update_all_trades to avoid partial updates corrupting the state.

        # Parallel fetch for efficiency
        # Using gather to run them concurrently
        results = await asyncio.gather(
            self.lighter.get_realized_funding(trade.symbol, start_time),
            self.x10.get_realized_funding(trade.symbol, start_time),
            return_exceptions=True
        )

        # Check for exceptions
        for r in results:
            if isinstance(r, Exception):
                raise r

        lighter_funding = results[0]
        x10_funding = results[1]

        # NOTE: X10Adapter now uses GET /user/funding/history for precise funding payments.
        # The old workaround (adding fees back to realised_pnl) is no longer needed.

        now = datetime.now(UTC)

        # Per-exchange tracking: funding_events.exchange is now "LIGHTER"/"X10" (legacy used "NET").
        breakdown = await self.store.get_funding_breakdown(trade.trade_id)

        # Migrate legacy NET-only accounting to per-exchange snapshots (best-effort).
        if "NET" in breakdown:
            await self.store.replace_funding_events(
                trade_id=trade.trade_id,
                events=[
                    {"exchange": Exchange.LIGHTER.value, "amount": lighter_funding, "timestamp": now},
                    {"exchange": Exchange.X10.value, "amount": x10_funding, "timestamp": now},
                ],
            )
            logger.info(
                f"Funding migration for {trade.symbol}: converted NET funding_events "
                f"to per-exchange snapshots (Lighter=${lighter_funding:.4f}, X10=${x10_funding:.4f})"
            )
            return

        prev_lighter = breakdown.get(Exchange.LIGHTER.value, Decimal("0"))
        prev_x10 = breakdown.get(Exchange.X10.value, Decimal("0"))

        delta_lighter = lighter_funding - prev_lighter
        delta_x10 = x10_funding - prev_x10

        eps = Decimal("0.0001")
        if abs(delta_lighter) <= eps and abs(delta_x10) <= eps:
            return

        if abs(delta_lighter) > eps:
            await self.store.record_funding(
                trade_id=trade.trade_id,
                exchange=Exchange.LIGHTER.value,
                amount=delta_lighter,
                timestamp=now,
            )

        if abs(delta_x10) > eps:
            await self.store.record_funding(
                trade_id=trade.trade_id,
                exchange=Exchange.X10.value,
                amount=delta_x10,
                timestamp=now,
            )

        # Persist update to DB (record_funding updated the cached trade object)
        await self.store.update_trade(
            trade.trade_id,
            {
                "funding_collected": trade.funding_collected,
                "last_funding_update": now,
            },
        )

        total_delta = delta_lighter + delta_x10

        await self.event_bus.publish(
            FundingCollected(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                exchange=Exchange.LIGHTER if total_delta >= 0 else Exchange.X10,
                amount=total_delta,
                cumulative=trade.funding_collected,
            )
        )

        logger.info(
            f"Funding update for {trade.symbol}: delta=${total_delta:.4f} "
            f"(Lighter delta=${delta_lighter:.4f}, X10 delta=${delta_x10:.4f}) "
            f"(Lighter=${lighter_funding:.4f}, X10=${x10_funding:.4f}, Total=${trade.funding_collected:.4f})"
        )

    async def _maybe_refresh_leg_fees(self, trade: Trade) -> None:
        """
        Best-effort refresh of leg fees for already-filled orders.

        This is important because:
        - Some adapters only expose fees via trade history (not order objects).
        - Fee backfilling is still useful for PnL reports and trade analysis.
        """
        updates: dict[str, Decimal] = {}

        async def _refresh_leg(
            leg_name: str,
            exchange: ExchangePort,
            leg_exchange: Exchange,
            order_id: str | None,
            current_fee: Decimal,
        ) -> None:
            if not order_id:
                return
            if current_fee != Decimal("0"):
                return
            try:
                o = await exchange.get_order(trade.symbol, order_id)
            except Exception:
                return
            if not o:
                return
            if o.fee is None:
                return
            if o.fee == Decimal("0"):
                return
            updates[f"{leg_name}_fees"] = o.fee

        leg1_ex = self.lighter if trade.leg1.exchange == Exchange.LIGHTER else self.x10
        leg2_ex = self.lighter if trade.leg2.exchange == Exchange.LIGHTER else self.x10
        await asyncio.gather(
            _refresh_leg(
                "leg1", leg1_ex, trade.leg1.exchange, trade.leg1.order_id, trade.leg1.fees
            ),
            _refresh_leg(
                "leg2", leg2_ex, trade.leg2.exchange, trade.leg2.order_id, trade.leg2.fees
            ),
            return_exceptions=True,
        )

        if not updates:
            return

        await self.store.update_trade(trade.trade_id, updates)

    async def save_pnl_update(self, trade: Trade) -> None:
        """Calculate and save unrealized PnL to trade record (Lightweight)."""
        # Get current price
        price_data = self.market_data.get_price(trade.symbol)
        current_price = price_data.mid_price if price_data else Decimal("0")

        if current_price == 0:
            return

        # Calculate unrealized PnL
        if trade.leg1.side.value == "BUY":
             leg1_pnl = (current_price - trade.leg1.entry_price) * trade.leg1.filled_qty
        else:
             leg1_pnl = (trade.leg1.entry_price - current_price) * trade.leg1.filled_qty

        # Leg 2 is usually visually hedged (inverse logic or same?)
        # Convention: If we are Long X10 (Buy), PnL = (Mark - Entry) * Qty
        # If we are Short X10 (Sell), PnL = (Entry - Mark) * Qty

        # NOTE: X10 price might differ slightly, but using mid_price for both is a fair approximation
        # for "Estimated uPnL". For precision, we should fetch X10 mark price specifically if available.
        if trade.leg2.side.value == "BUY":
             leg2_pnl = (current_price - trade.leg2.entry_price) * trade.leg2.filled_qty
        else:
             leg2_pnl = (trade.leg2.entry_price - current_price) * trade.leg2.filled_qty

        unrealized_pnl = leg1_pnl + leg2_pnl

        # Update trade state object
        trade.unrealized_pnl = unrealized_pnl

        # Persist ONLY to the trade table (fast update)
        await self.store.update_trade(
            trade_id=trade.trade_id,
            updates={"unrealized_pnl": unrealized_pnl}
        )

    async def save_pnl_snapshot(self, trade: Trade) -> None:
        """Save a PnL snapshot for a trade (Historical)."""
        # Reuse calculation logic via the update method first
        await self.save_pnl_update(trade)

        # Save historical snapshot
        await self.store.save_pnl_snapshot(
            trade_id=trade.trade_id,
            realized_pnl=trade.realized_pnl,
            unrealized_pnl=trade.unrealized_pnl,
            funding=trade.funding_collected,
            fees=trade.total_fees,
            timestamp=datetime.now(UTC),
        )

    async def _record_funding_history(self) -> None:
        """
        Record current funding rates for all tradeable symbols to DB.

        This creates a historical record of funding rates for:
        - APY stability analysis (was 80% stable or a 30-min spike?)
        - PnL attribution (how much came from funding vs spread)
        - Backtesting future strategies

        Uses INSERT OR IGNORE to deduplicate if called multiple times per hour.
        """
        symbols = self.market_data.get_common_symbols()
        if not symbols:
            return

        now = datetime.now(UTC)
        # Round timestamp to nearest hour for deduplication
        # (funding rates are typically hourly, no need for sub-hour granularity)
        rounded_hour = now.replace(minute=0, second=0, microsecond=0)

        recorded = 0
        for symbol in symbols:
            snapshot = self.market_data.get_funding(symbol)
            if not snapshot:
                continue

            # Record Lighter rate
            if snapshot.lighter_rate:
                with contextlib.suppress(Exception):
                    await self.store.record_funding_history(
                        symbol=symbol,
                        exchange="LIGHTER",
                        rate_hourly=snapshot.lighter_rate.rate,
                        next_funding_time=snapshot.lighter_rate.next_funding_time,
                        timestamp=rounded_hour,
                    )
                    recorded += 1

            # Record X10 rate
            if snapshot.x10_rate:
                with contextlib.suppress(Exception):
                    await self.store.record_funding_history(
                        symbol=symbol,
                        exchange="X10",
                        rate_hourly=snapshot.x10_rate.rate,
                        next_funding_time=snapshot.x10_rate.next_funding_time,
                        timestamp=rounded_hour,
                    )
                    recorded += 1

        if recorded > 0:
            logger.debug(f"Recorded {recorded} funding rate snapshots to history")
