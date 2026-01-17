"""
Position manager facade with orchestration logic.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.config.settings import Settings
from funding_bot.domain.models import Exchange, Trade, TradeStatus
from funding_bot.domain.rules import ExitDecision
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort
from funding_bot.services.market_data import MarketDataService
from funding_bot.services.positions.broken_hedge import _handle_broken_hedge_if_needed
from funding_bot.services.positions.close import (
    _calculate_close_price,
    _close_both_legs_coordinated,
    _close_impl,
    _close_leg,
    _close_lighter_smart,
    _close_verify_and_finalize,
    _close_x10_smart,
    _ensure_lighter_websocket,
    _execute_ioc_close,
    _execute_lighter_close_attempt,
    _execute_lighter_maker_close,
    _execute_lighter_taker_fallback,
    _get_lighter_close_config,
    _post_close_readback,
    _rebalance_trade,
    _should_skip_lighter_close,
    _submit_maker_order,
    _verify_closed,
)
from funding_bot.services.positions.exit_eval import _evaluate_exit
from funding_bot.services.positions.imbalance import _check_position_imbalance
from funding_bot.services.positions.pnl import (
    _calculate_realizable_pnl,
    _calculate_realizable_pnl_depth,
    _calculate_realized_pnl,
    _calculate_unrealized_pnl,
)
from funding_bot.services.positions.types import CloseResult

if TYPE_CHECKING:
    from funding_bot.services.opportunities import OpportunityEngine

logger = get_logger(__name__)

# Constants
_Z_SCORE_EXIT_LOOKBACK_HOURS = 168  # 7 days for Z-score exit


class PositionManager:
    """
    Manages open trades and handles exit logic.

    Responsibilities:
    - Monitor trades for exit conditions
    - Close both legs simultaneously
    - Calculate and record PnL
    - Verify positions are actually closed
    """

    def __init__(
        self,
        settings: Settings,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: TradeStorePort,
        event_bus: EventBusPort,
        market_data: MarketDataService,
        opportunity_engine: OpportunityEngine | None = None,
    ):
        self.settings = settings
        self.lighter = lighter
        self.x10 = x10
        self.store = store
        self.event_bus = event_bus
        self.market_data = market_data
        self.opportunity_engine = opportunity_engine

        # Close locks per symbol
        self._close_locks: dict[str, asyncio.Lock] = {}

        # Broken-hedge confirmation state:
        # symbol -> (missing_exchange, consecutive_hits, last_seen_utc)
        self._broken_hedge_hits: dict[str, tuple[Exchange, int, datetime]] = {}
        # Close-failure alert throttle:
        # symbol -> last_alert_utc
        self._close_failure_alert_last: dict[str, datetime] = {}
        # Trade snapshot persistence throttle:
        # trade_id -> last_persist_utc
        self._trade_snapshot_last: dict[str, datetime] = {}
        # Exit eval logging throttle (60s interval):
        # trade_id -> last_log_utc
        self._exit_eval_log_last: dict[str, datetime] = {}
        # Rebalance cooldowns:
        # symbol -> cooldown_until_utc
        self._rebalance_cooldowns: dict[str, datetime] = {}
        # Rebalance cooldown log throttle:
        # symbol -> last_logged_cooldown_until_utc
        self._rebalance_cooldown_logged_until: dict[str, datetime] = {}

        # 🚀 PERFORMANCE: Track active orderbook subscriptions to avoid re-subscribing
        # Only subscribe when a trade is opened, unsubscribe when closed
        self._active_orderbook_subs: set[str] = set()  # Symbols with active subscriptions
        self._orderbook_subs_lock = asyncio.Lock()

    def _get_close_lock(self, symbol: str) -> asyncio.Lock:
        """Get or create close lock for symbol."""
        if symbol not in self._close_locks:
            self._close_locks[symbol] = asyncio.Lock()
        return self._close_locks[symbol]

    async def _get_historical_context(self, symbol: str, current_apy: Decimal) -> dict[str, Any]:
        """
        Get historical context for a symbol (crash history, volatility metrics).

        Args:
            symbol: Trading symbol
            current_apy: Current net APY for crash detection

        Returns:
            Dict with keys:
            - crash_count: Number of historical crashes (legacy, unused)
            - avg_recovery_minutes: Average recovery time (legacy, unused)
            - current_crash_depth: Depth if currently in crash (legacy, unused)
            - predicted_recovery_p50: Predicted recovery time (legacy, unused)
            - recent_apy_history: List of hourly net APY values (NEW - for velocity/Z-score)
        """
        # Fetch historical APY from store for velocity/Z-score exits
        recent_apy_history = []
        try:
            recent_apy_history = await self.store.get_recent_apy_history(
                symbol=symbol,
                hours_back=_Z_SCORE_EXIT_LOOKBACK_HOURS,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch historical APY for {symbol}: {e}")
            recent_apy_history = []

        return {
            "crash_count": 0,  # Legacy field (no longer used)
            "avg_recovery_minutes": 0,  # Legacy field
            "current_crash_depth": None,  # Legacy field
            "predicted_recovery_p50": None,  # Legacy field
            "recent_apy_history": recent_apy_history,  # ✅ NEW: For velocity/Z-score exits
        }

    async def check_trades(self) -> list[Trade]:
        """
        Check all open trades for exit conditions.

        Returns list of trades that were closed.
        """
        open_trades = await self.store.list_open_trades()
        closed_trades: list[Trade] = []

        # 🚀 PERFORMANCE: Smart orderbook subscription management
        # Only subscribe to symbols that aren't already subscribed
        # This eliminates redundant subscription calls on every loop iteration
        ensure_ob_fn_lighter = getattr(self.lighter, "ensure_orderbook_ws", None)
        ensure_ob_fn_x10 = getattr(self.x10, "ensure_orderbook_ws", None)

        if open_trades and (callable(ensure_ob_fn_lighter) or callable(ensure_ob_fn_x10)):
            # Get current symbols
            current_symbols = {t.symbol for t in open_trades}

            # Find new symbols that need subscription
            async with self._orderbook_subs_lock:
                new_symbols = current_symbols - self._active_orderbook_subs
                if new_symbols:
                    # Update active subscriptions
                    self._active_orderbook_subs.update(current_symbols)

            # Only subscribe to new symbols
            if new_symbols:
                async def _ensure_one(sym: str) -> None:
                    with contextlib.suppress(Exception):
                        if callable(ensure_ob_fn_lighter):
                            await ensure_ob_fn_lighter(sym, timeout=0.0)  # type: ignore[misc]
                    with contextlib.suppress(Exception):
                        if callable(ensure_ob_fn_x10):
                            await ensure_ob_fn_x10(sym, timeout=0.0)  # type: ignore[misc]

                await asyncio.gather(
                    *(_ensure_one(sym) for sym in new_symbols),
                    return_exceptions=True
                )
                logger.debug(f"Subscribed to orderbook for {len(new_symbols)} new symbols: {new_symbols}")

            # Clean up stale subscriptions (symbols no longer in open trades)
            async with self._orderbook_subs_lock:
                stale_symbols = self._active_orderbook_subs - current_symbols
                if stale_symbols:
                    self._active_orderbook_subs.difference_update(stale_symbols)
                    # Note: We don't explicitly unsubscribe here as most exchanges
                    # handle this gracefully. If needed, add unsubscribe logic.
                    logger.debug(f"Cleaned up {len(stale_symbols)} stale subscriptions: {stale_symbols}")

        # Get best outside opportunity APY for Opportunity Cost check
        best_opp_apy = Decimal("0")
        if self.opportunity_engine:
            try:
                open_symbols = {t.symbol for t in open_trades if t.status == TradeStatus.OPEN}
                best_opp = await self.opportunity_engine.get_best_opportunity(open_symbols)
                if best_opp:
                    best_opp_apy = best_opp.apy
            except Exception as e:
                logger.warning(f"Failed to fetch best opportunity for exit check: {e}")

        # 🚀 PERFORMANCE: Single-pass categorization (2x faster than double comprehension)
        closing_trades: list[Trade] = []
        open_trades_to_check: list[Trade] = []
        for trade in open_trades:
            if trade.status == TradeStatus.CLOSING:
                closing_trades.append(trade)
            elif trade.status == TradeStatus.OPEN:
                open_trades_to_check.append(trade)

        # Handle CLOSING trades sequentially (must finish before anything else)
        for trade in closing_trades:
            try:
                result = await self.close_trade(trade, trade.close_reason or "retry_close")
                if result.success and trade.status == TradeStatus.CLOSED:
                    closed_trades.append(trade)
            except Exception as e:
                logger.exception(f"Error retrying close for {trade.symbol}: {e}")

        # PREMIUM-OPTIMIZED: Parallel exit evaluation for OPEN trades
        # This leverages Premium's high REST limits while respecting WS constraints
        eval_results = await self._evaluate_exits_parallel(open_trades_to_check, best_opp_apy)

        # Process evaluation results and close trades
        for trade, decision, broken_hedge, imbalance_checked in eval_results:
            # First handle broken hedge if needed (must close immediately)
            if broken_hedge:
                closed_trades.append(trade)
                continue

            try:
                # Check position imbalance if not yet checked
                if not imbalance_checked:
                    await self._check_position_imbalance(trade)

                # Process exit decision
                if decision and decision.should_exit:
                    if decision.reason and decision.reason.startswith("REBALANCE:"):
                        now = datetime.now(UTC)
                        cooldown_minutes = int(getattr(self.settings.trading, "cooldown_minutes", 60))
                        cooldown_until = self._rebalance_cooldowns.get(trade.symbol)
                        if cooldown_until and now < cooldown_until:
                            last_logged_until = self._rebalance_cooldown_logged_until.get(trade.symbol)
                            if last_logged_until != cooldown_until:
                                logger.info(
                                    f"Rebalance cooldown active for {trade.symbol}: "
                                    f"skipping until {cooldown_until.isoformat()}"
                                )
                                self._rebalance_cooldown_logged_until[trade.symbol] = cooldown_until
                            continue
                        if cooldown_until and now >= cooldown_until:
                            del self._rebalance_cooldowns[trade.symbol]
                            self._rebalance_cooldown_logged_until.pop(trade.symbol, None)
                        self._rebalance_cooldowns[trade.symbol] = now + timedelta(minutes=cooldown_minutes)
                        self._rebalance_cooldown_logged_until.pop(trade.symbol, None)

                    logger.info(f"Exit condition met for {trade.symbol}: {decision.reason}")
                    result = await self.close_trade(trade, decision.reason)

                    if result.success and trade.status == TradeStatus.CLOSED:
                        closed_trades.append(trade)

            except Exception as e:
                logger.exception(f"Error checking trade {trade.symbol}: {e}")

        return closed_trades

    async def close_trade(self, trade: Trade, reason: str) -> CloseResult:
        """
        Close a trade by closing both legs.

        Thread-safe per symbol.
        """
        lock = self._get_close_lock(trade.symbol)

        async with lock:
            return await self._close_impl(trade, reason)

    async def _update_trade(self, trade: Trade) -> None:
        """Persist trade updates."""
        await self.store.update_trade(
            trade.trade_id,
            {
                "status": trade.status,
                "leg1_exit_price": trade.leg1.exit_price,
                "leg1_fees": trade.leg1.fees,
                "leg2_exit_price": trade.leg2.exit_price,
                "leg2_fees": trade.leg2.fees,
                "realized_pnl": trade.realized_pnl,
                "close_reason": trade.close_reason,
                "closed_at": trade.closed_at,
            },
        )

    async def force_close_all(self, reason: str = "Emergency close") -> int:
        """Force close all open trades."""
        open_trades = await self.store.list_open_trades()
        closed = 0

        for trade in open_trades:
            try:
                result = await self.close_trade(trade, reason)
                if result.success:
                    closed += 1
            except Exception as e:
                logger.exception(f"Error force closing {trade.symbol}: {e}")

        logger.info(f"Force closed {closed}/{len(open_trades)} trades")
        return closed

    async def _evaluate_exits_parallel(
        self,
        trades: list[Trade],
        best_opportunity_apy: Decimal,
        max_concurrent: int = 10,
    ) -> list[tuple[Trade, ExitDecision | None, bool, bool]]:
        """
        Evaluate exit rules for multiple trades in parallel with controlled concurrency.

        Args:
            trades: List of trades to evaluate
            best_opportunity_apy: Best opportunity APY for opportunity cost check
            max_concurrent: Maximum concurrent evaluations (Premium-safe)

        Returns:
            List of tuples (trade, decision, broken_hedge, imbalance_checked)
            - decision: ExitDecision or None if evaluation failed
            - broken_hedge: True if hedge was broken and needs closing
            - imbalance_checked: True if imbalance was already checked

        NOTE: With Lighter Premium (24,000 req/min), we can evaluate 10+ positions
        concurrently. Each evaluation may make 2-3 API calls (position fetch), so with
        10 concurrent = ~30 calls, well within Premium limits.

        Conservative defaults:
        - max_concurrent=10: Process 10 trades concurrently
        - Each eval = ~2-3 API calls (position data, maybe orderbook)
        - Total = ~30 calls concurrently, far below Premium limit of 24,000/min
        """
        if not trades:
            return []

        results: list[tuple[Trade, ExitDecision | None, bool, bool]] = []

        async def evaluate_single_trade(trade: Trade) -> tuple[Trade, ExitDecision | None, bool, bool]:
            """Evaluate exit for a single trade with safety checks."""
            try:
                # Check for broken hedge first (must close immediately)
                broken_hedge = await self._handle_broken_hedge_if_needed(trade)

                # Check position imbalance (Treadfi-style warning)
                # We do this before exit eval to ensure monitoring runs
                await self._check_position_imbalance(trade)

                # Evaluate exit rules
                decision = await self._evaluate_exit(trade, best_opportunity_apy)

                return (trade, decision, broken_hedge, True)  # imbalance_checked=True
            except Exception as e:
                logger.warning(f"Failed to evaluate exit for {trade.symbol}: {e}")
                return (trade, None, False, False)

        # Create semaphore for controlled concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def evaluate_with_limit(trade: Trade) -> tuple[Trade, ExitDecision | None, bool, bool]:
            """Evaluate with semaphore to limit concurrency."""
            async with semaphore:
                return await evaluate_single_trade(trade)

        # Process all trades in parallel with concurrency limit
        results = await asyncio.gather(*[
            evaluate_with_limit(trade) for trade in trades
        ], return_exceptions=True)

        # Filter out any exceptions from gather
        valid_results = [
            r for r in results
            if isinstance(r, tuple) and len(r) == 4
        ]

        return valid_results

    _handle_broken_hedge_if_needed = _handle_broken_hedge_if_needed
    _check_position_imbalance = _check_position_imbalance
    _evaluate_exit = _evaluate_exit
    _calculate_unrealized_pnl = _calculate_unrealized_pnl
    _calculate_realizable_pnl = _calculate_realizable_pnl
    _calculate_realizable_pnl_depth = _calculate_realizable_pnl_depth
    _calculate_realized_pnl = _calculate_realized_pnl
    _close_impl = _close_impl
    _close_verify_and_finalize = _close_verify_and_finalize
    _post_close_readback = _post_close_readback
    _close_lighter_smart = _close_lighter_smart
    _close_x10_smart = _close_x10_smart
    _close_leg = _close_leg
    _verify_closed = _verify_closed
    _get_lighter_close_config = _get_lighter_close_config
    _should_skip_lighter_close = _should_skip_lighter_close
    _ensure_lighter_websocket = _ensure_lighter_websocket
    _calculate_close_price = _calculate_close_price
    _execute_lighter_maker_close = _execute_lighter_maker_close
    _execute_lighter_close_attempt = _execute_lighter_close_attempt
    _execute_lighter_taker_fallback = _execute_lighter_taker_fallback
    _rebalance_trade = _rebalance_trade
    _close_both_legs_coordinated = _close_both_legs_coordinated
    _submit_maker_order = _submit_maker_order
    _execute_ioc_close = _execute_ioc_close
