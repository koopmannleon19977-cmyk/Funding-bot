"""
Execution Engine.

Handles trade execution with:
- Two-leg delta-neutral entry
- Leg1 as maker (Lighter), Leg2 as taker (X10)
- Microfill policy
- Rollback on hedge failure
- State machine transitions with persistence

This is the heart of the trading bot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.config.settings import Settings
from funding_bot.domain.errors import (
    ExecutionError,
)
from funding_bot.domain.events import (
    TradeStateChanged,
)
from funding_bot.domain.models import (
    Exchange,
    ExecutionAttempt,
    Opportunity,
    Order,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort
from funding_bot.services.execution_flow import (
    _execute_impl as _execute_impl_flow,
)
from funding_bot.services.execution_flow import (
    _execute_leg1 as _execute_leg1_flow,
)
from funding_bot.services.execution_flow import (
    _execute_leg2 as _execute_leg2_flow,
)
from funding_bot.services.execution_flow import (
    _rollback as _rollback_flow,
)
from funding_bot.services.execution_orderbook import (
    _get_best_effort_x10_depth_snapshot as _get_best_effort_x10_depth_snapshot_orders,
)
from funding_bot.services.execution_orderbook import (
    _get_best_effort_x10_orderbook as _get_best_effort_x10_orderbook_orders,
)
from funding_bot.services.execution_orders import (
    _place_lighter_taker_ioc as _place_lighter_taker_ioc_orders,
)
from funding_bot.services.execution_orders import (
    _wait_for_fill as _wait_for_fill_orders,
)
from funding_bot.services.execution_orders import (
    _wait_for_fill_polling as _wait_for_fill_polling_orders,
)
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.services.execution_ws import (
    cleanup_stale_watchers as _cleanup_stale_watchers_ws,
)
from funding_bot.services.execution_ws import (
    ensure_ws_order_subscriptions as _ensure_ws_order_subscriptions_ws,
)
from funding_bot.services.execution_ws import (
    on_ws_order_update as _on_ws_order_update_ws,
)
from funding_bot.services.market_data import (
    MarketDataService,
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger(__name__)


class ExecutionEngine:
    """
    Executes delta-neutral trades.

    Execution Strategy (default):
    1. Place Leg1 as POST_ONLY LIMIT on Lighter (maker)
    2. Wait for fill
    3. Place Leg2 as MARKET on X10 (taker hedge)
    4. If hedge fails: rollback Leg1

    State Machine:
    PENDING -> LEG1_SUBMITTED -> LEG1_FILLED -> LEG2_SUBMITTED -> COMPLETE
                    |                |
                    v                v
                  ABORTED         ROLLBACK_*
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

        # Execution lock per symbol
        self._locks: dict[str, asyncio.Lock] = {}

        # Active executions
        self._active_executions: dict[str, Trade] = {}
        self._last_trade_status: dict[str, TradeStatus] = {}

        # Turbo mode: WS-driven order fill waiting (best-effort)
        self._ws_orders_ready: bool = False
        self._ws_orders_lock = asyncio.Lock()
        self._order_watchers: dict[tuple[Exchange, str], set[asyncio.Queue[Order]]] = {}
        self._last_order_update: dict[tuple[Exchange, str], Order] = {}

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        """Get or create lock for symbol."""
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    async def _cleanup_stale_watchers(self) -> None:
        """Clean up stale order watchers and cached order updates."""
        return await _cleanup_stale_watchers_ws(self)

    async def _kpi_create_attempt(self, attempt: ExecutionAttempt) -> str | None:
        try:
            return await self.store.create_execution_attempt(attempt)
        except Exception as e:
            logger.warning(f"Failed to persist execution attempt (non-fatal): {e}")
            return None

    async def _kpi_update_attempt(self, attempt_id: str | None, updates: dict[str, Any]) -> None:
        if not attempt_id:
            return
        try:
            await self.store.update_execution_attempt(attempt_id, updates)
        except Exception as e:
            logger.warning(f"Failed to update execution attempt (non-fatal): {e}")

    async def execute(self, opportunity: Opportunity) -> ExecutionResult:
        """
        Execute a trade for an opportunity.

        Thread-safe per symbol via locks.
        """
        symbol = opportunity.symbol
        lock = self._get_lock(symbol)

        async with lock:
            return await self._execute_impl(opportunity)

    async def _execute_impl(self, opp: Opportunity) -> ExecutionResult:
        """
        Internal execution implementation.
        """
        return await _execute_impl_flow(self, opp)

    async def _preflight_checks(self, trade: Trade, opp: Opportunity) -> None:
        """Run preflight checks before execution."""

        # Check balances
        lighter_balance = await self.lighter.get_available_balance()
        x10_balance = await self.x10.get_available_balance()

        # Preflight Balance Check
        # FIX: Do not block based on arbitrary 'suggested_notional' ($150).
        # Dynamic Sizing later will handle the actual sizing and scaling down.
        # Just ensure we have *some* positive balance to attempt a trade.
        min_required = Decimal("5.0")  # Minimum $5 margin to be worth it

        if lighter_balance.available < min_required:
            raise ExecutionError(
                f"Insufficient Lighter balance for any trade: {lighter_balance.available:.2f} < {min_required}",
                symbol=trade.symbol,
            )

        if x10_balance.available < min_required:
            raise ExecutionError(
                f"Insufficient X10 balance for any trade: {x10_balance.available:.2f} < {min_required}",
                symbol=trade.symbol,
            )

        logger.debug(f"Preflight passed: Lighter=${lighter_balance.available:.2f}, X10=${x10_balance.available:.2f}")

    async def _calculate_quantity(self, trade: Trade, opp: Opportunity) -> Decimal:
        """Calculate common quantity respecting both exchange step sizes."""
        lighter_info = self.market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
        x10_info = self.market_data.get_market_info(trade.symbol, Exchange.X10)

        # Get step sizes
        lighter_step = lighter_info.step_size if lighter_info else Decimal("0.0001")
        x10_step = x10_info.step_size if x10_info else Decimal("0.0001")

        # Use larger step size for compatibility
        step = max(lighter_step, x10_step)

        # Round down to step size
        # FIX: Use trade.target_qty (which may have been dynamically updated)
        # instead of opp.suggested_qty (which is static from opportunities)
        qty = trade.target_qty
        qty = (qty // step) * step

        # Check minimums
        lighter_min = lighter_info.min_qty if lighter_info else Decimal("0")
        x10_min = x10_info.min_qty if x10_info else Decimal("0")
        min_qty = max(lighter_min, x10_min)

        if qty < min_qty:
            # Attempt to bump to minimum only if it doesn't violate sizing/risk caps.
            # If exchange min_qty forces a much larger notional than we sized for,
            # we must reject instead of silently increasing exposure.
            price_data = self.market_data.get_price(trade.symbol)
            price = price_data.mid_price if price_data else Decimal("0")

            est_value = min_qty * price

            bump_multiple = _safe_decimal(
                getattr(self.settings.execution, "max_min_qty_bump_multiple", None),
                Decimal("1.5"),
            )
            if bump_multiple <= 1:
                bump_multiple = Decimal("1.0")

            max_allowed = trade.target_notional_usd * bump_multiple

            if est_value > Decimal("0") and max_allowed > 0 and est_value <= max_allowed:
                logger.warning(
                    f"Bumping quantity for {trade.symbol} from {qty} to "
                    f"min {min_qty} (Est Value: ${est_value:.2f} vs "
                    f"Target ${trade.target_notional_usd:.2f})"
                )
                qty = min_qty
            else:
                raise ExecutionError(
                    f"Quantity {qty} below minimum {min_qty}. "
                    f"Min-notional ${est_value:.2f} exceeds target ${trade.target_notional_usd:.2f} "
                    f"* {bump_multiple} (${max_allowed:.2f}).",
                    symbol=trade.symbol,
                )

        return qty

    async def _execute_leg1(self, trade: Trade, opp: Opportunity, *, attempt_id: str | None = None) -> None:
        """
        Execute Leg 1 (Lighter - Maker) with Dynamic Linear Repricing.
        """
        return await _execute_leg1_flow(self, trade, opp, attempt_id=attempt_id)

    async def _execute_leg2(
        self,
        trade: Trade,
        opp: Opportunity,
        *,
        desired_qty: Decimal | None = None,
        leg1_filled_monotonic: float | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Execute Leg 2 (X10 - Hedge) using Smart Limit IOC.

        The X10 SDK places LIMIT orders (including IOC). We hedge by submitting a marketable
        LIMIT IOC with a slippage cap. If the IOC cancels without fill, we retry quickly using
        a fresh X10 L1 snapshot and slightly wider caps before triggering a rollback.
        """
        return await _execute_leg2_flow(
            self,
            trade,
            opp,
            desired_qty=desired_qty,
            leg1_filled_monotonic=leg1_filled_monotonic,
            attempt_id=attempt_id,
        )

    async def _rollback(self, trade: Trade, reason: str = "Leg2 failed") -> None:
        """
        Rollback Leg1 due to failed Leg2.
        """
        return await _rollback_flow(self, trade, reason)

    async def _place_lighter_taker_ioc(
        self,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        slippage: Decimal,
        reduce_only: bool,
        timeout_seconds: float,
        purpose: str,
    ) -> Order | None:
        """Place a marketable LIMIT IOC on Lighter with an explicit slippage cap.

        We avoid adapter MARKET emulation (which uses a wide fixed slippage) and instead
        compute a tight price from a fresh L1 snapshot.
        """
        return await _place_lighter_taker_ioc_orders(
            self,
            symbol=symbol,
            side=side,
            qty=qty,
            slippage=slippage,
            reduce_only=reduce_only,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
        )

    async def _wait_for_fill(
        self,
        adapter: ExchangePort,
        symbol: str,
        order_id: str,
        timeout: float = 30.0,
        check_callback: Callable[[], Awaitable[bool]] | None = None,
    ) -> Order | None:
        """Wait for order to fill or timeout."""
        return await _wait_for_fill_orders(
            self,
            adapter,
            symbol,
            order_id,
            timeout=timeout,
            check_callback=check_callback,
        )

    async def _wait_for_fill_polling(
        self,
        adapter: ExchangePort,
        symbol: str,
        order_id: str,
        *,
        timeout: float,
        check_callback: Callable[[], Awaitable[bool]] | None = None,
    ) -> Order | None:
        return await _wait_for_fill_polling_orders(
            self,
            adapter,
            symbol,
            order_id,
            timeout=timeout,
            check_callback=check_callback,
        )

    async def _ensure_ws_order_subscriptions(self) -> None:
        """Register WS order callbacks once (best-effort)."""
        return await _ensure_ws_order_subscriptions_ws(self)

    async def _on_ws_order_update(self, order: Order) -> None:
        """Dispatch WS order updates to any in-flight waiters."""
        return await _on_ws_order_update_ws(self, order)

    async def _get_best_effort_x10_depth_snapshot(
        self,
        symbol: str,
        *,
        levels: int,
        max_age_seconds: float,
    ) -> OrderbookDepthSnapshot:
        return await _get_best_effort_x10_depth_snapshot_orders(
            self,
            symbol,
            levels=levels,
            max_age_seconds=max_age_seconds,
        )

    async def _get_best_effort_x10_orderbook(
        self,
        symbol: str,
        *,
        max_age_seconds: float,
    ) -> OrderbookSnapshot:
        return await _get_best_effort_x10_orderbook_orders(
            self,
            symbol,
            max_age_seconds=max_age_seconds,
        )

    async def _get_best_effort_orderbook(self, symbol: str, *, max_age_seconds: float) -> object:
        """
        Prefer MarketDataService cache (which can be WS-fed) and fall back to REST refresh if stale.
        """
        try:
            ob = self.market_data.get_orderbook(symbol)
            if ob and getattr(ob, "lighter_updated", None) and getattr(ob, "x10_updated", None):
                now = datetime.now(UTC)
                l_age = (now - ob.lighter_updated).total_seconds() if ob.lighter_updated else 1e9
                x_age = (now - ob.x10_updated).total_seconds() if ob.x10_updated else 1e9
                if l_age <= max_age_seconds and x_age <= max_age_seconds:
                    return ob
        except Exception:
            pass

        return await self.market_data.get_fresh_orderbook(symbol)

    async def _update_trade(self, trade: Trade) -> None:
        """Persist trade state."""
        old_status = self._last_trade_status.get(trade.trade_id, trade.status)
        self._last_trade_status[trade.trade_id] = trade.status
        await self.store.update_trade(
            trade.trade_id,
            {
                "status": trade.status,
                "execution_state": trade.execution_state,
                "leg1_order_id": trade.leg1.order_id,
                "leg1_qty": trade.leg1.qty,
                "leg1_filled_qty": trade.leg1.filled_qty,
                "leg1_entry_price": trade.leg1.entry_price,
                "leg1_fees": trade.leg1.fees,
                "leg2_order_id": trade.leg2.order_id,
                "leg2_qty": trade.leg2.qty,
                "leg2_filled_qty": trade.leg2.filled_qty,
                "leg2_entry_price": trade.leg2.entry_price,
                "leg2_fees": trade.leg2.fees,
                "target_qty": trade.target_qty,
            },
        )

        # Publish state change
        await self.event_bus.publish(
            TradeStateChanged(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                old_status=old_status,
                new_status=trade.status,
                execution_state=trade.execution_state,
            )
        )

    def get_active_executions(self) -> list[Trade]:
        """Get currently executing trades."""
        return list(self._active_executions.values())
