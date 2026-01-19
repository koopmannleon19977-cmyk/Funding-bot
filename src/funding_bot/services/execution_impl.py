"""
Execution flow helpers (moved from ExecutionEngine).
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from funding_bot.domain.errors import DomainError
from funding_bot.domain.models import (
    Exchange,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionState,
    Opportunity,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_impl_entry_guard import _execute_impl_entry_guard
from funding_bot.services.execution_impl_post_leg1 import _execute_impl_post_leg1
from funding_bot.services.execution_impl_post_leg2 import _execute_impl_post_leg2
from funding_bot.services.execution_impl_pre import _execute_impl_pre
from funding_bot.services.execution_impl_sizing import _execute_impl_sizing
from funding_bot.services.execution_types import ExecutionResult

logger = get_logger("funding_bot.services.execution")


async def _execute_impl(self, opp: Opportunity) -> ExecutionResult:
    """Internal execution implementation."""

    attempt_id: str | None = None
    mode = "LIVE" if self.settings.live_trading else "PAPER"

    # Ensure WS order subscriptions are registered before placing the first order so we don't
    # miss instant fills (mirrors the `.tmp` WS-callback approach).
    es = self.settings.execution
    if bool(getattr(es, "ws_fill_wait_enabled", True)):
        await self._ensure_ws_order_subscriptions()
        # Best-effort: subscribe to Lighter per-market account_orders stream so instant fills
        # are captured via WS callbacks (mirrors `.tmp` tools).
        if bool(getattr(es, "ws_ready_gate_enabled", True)):
            ensure_market_fn = getattr(self.lighter, "ensure_orders_ws_market", None)
            if callable(ensure_market_fn):
                timeout_s = float(getattr(es, "ws_ready_gate_timeout_seconds", Decimal("2.0")) or 2.0)
                with contextlib.suppress(Exception):
                    await ensure_market_fn(opp.symbol, timeout=timeout_s)
            wait_x10_ready = getattr(self.x10, "wait_ws_ready", None)
            if callable(wait_x10_ready):
                timeout_s = float(getattr(es, "ws_ready_gate_timeout_seconds", Decimal("2.0")) or 2.0)
                with contextlib.suppress(Exception):
                    await wait_x10_ready(timeout=timeout_s)

    # X10 orderbooks WS is most reliable per-market (single symbol). Start it on-demand for this trade
    # so execution-time spread/depth checks have fresh L1 data without subscribing to the ALL-markets stream.
    # We start this UNCONDITIONALLY for execution to avoid 2s+ REST latency on hedge checks.
    ws = getattr(self.settings, "websocket", None)
    with contextlib.suppress(Exception):
        depth = int(getattr(ws, "orderbook_stream_depth", 1) or 1)
        await self.x10.subscribe_orderbook_l1([f"{opp.symbol}-USD"], depth=depth)

    logger.info(
        f"Executing trade for {opp.symbol}: "
        f"Long {opp.long_exchange.value}, "
        f"Short {opp.short_exchange.value}, "
        f"Qty={opp.suggested_qty:.6f}, APY={opp.apy:.2%}"
    )

    # Create trade object
    trade = Trade.create(
        symbol=opp.symbol,
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=(Side.BUY if opp.long_exchange == Exchange.LIGHTER else Side.SELL),
        leg2_exchange=Exchange.X10,
        target_qty=opp.suggested_qty,
        target_notional_usd=opp.suggested_notional,
        entry_apy=opp.apy,
        entry_spread=opp.spread_pct,
    )

    # Important: don't persist a Trade until we've passed preflight checks.
    # Otherwise the DB/KPIs get polluted with "trades" that never executed.
    trade.status = TradeStatus.PENDING
    trade.execution_state = ExecutionState.PENDING
    trade_persisted = False

    try:
        # Create KPI/decision log record (separate from trades table).
        attempt = ExecutionAttempt.create(
            symbol=opp.symbol,
            mode=mode,
            trade_id=trade.trade_id,
            stage="START",
            data={
                "apy": str(opp.apy),
                "expected_value_usd": str(opp.expected_value_usd),
                "breakeven_hours": str(opp.breakeven_hours),
                "opp_spread_pct": str(opp.spread_pct),
                "mid_price": str(opp.mid_price),
                "lighter_rate": str(opp.lighter_rate),
                "x10_rate": str(opp.x10_rate),
                "net_funding_hourly": str(opp.net_funding_hourly),
                "long_exchange": opp.long_exchange.value,
                "short_exchange": opp.short_exchange.value,
                "suggested_qty": str(opp.suggested_qty),
                "suggested_notional": str(opp.suggested_notional),
            },
        )
        attempt_id = await self._kpi_create_attempt(attempt)

        preflight = await _execute_impl_pre(self, opp, trade, attempt_id)
        if isinstance(preflight, ExecutionResult):
            return preflight
        fresh_ob, max_spread, prefetched_depth_ob = preflight

        sizing = await _execute_impl_sizing(
            self,
            opp,
            trade,
            attempt_id,
            fresh_ob,
            prefetched_depth_ob,
        )
        if isinstance(sizing, ExecutionResult):
            return sizing
        prefetched_depth_ob = sizing

        entry_guard = await _execute_impl_entry_guard(
            self,
            opp,
            trade,
            attempt_id,
            fresh_ob,
            prefetched_depth_ob,
        )
        if isinstance(entry_guard, ExecutionResult):
            return entry_guard

        # Persist trade only once we're actually about to execute orders.
        trade.status = TradeStatus.OPENING
        trade.execution_state = ExecutionState.PENDING
        await self.store.create_trade(trade)
        self._active_executions[trade.trade_id] = trade
        trade_persisted = True
        await self._kpi_update_attempt(attempt_id, {"stage": "TRADE_PERSISTED", "trade_id": trade.trade_id})

        post_leg1_result, leg1_filled_t = await _execute_impl_post_leg1(
            self,
            opp,
            trade,
            attempt_id,
            max_spread,
        )
        if post_leg1_result is not None:
            return post_leg1_result

        return await _execute_impl_post_leg2(
            self,
            opp,
            trade,
            attempt_id,
            leg1_filled_t,
        )

    except Exception as e:
        # DomainErrors represent expected/handled failure modes (rejections, liquidity aborts, etc.).
        # Logging these with full tracebacks makes live logs noisy and can be misleading.
        if isinstance(e, DomainError):
            logger.error(f"Execution failed for {trade.symbol}: {e}")
        else:
            logger.exception(f"Execution failed for {trade.symbol}: {e}")
        if not trade_persisted:
            trade.status = TradeStatus.REJECTED
            trade.execution_state = ExecutionState.ABORTED
            trade.error = str(e)
            await self._kpi_update_attempt(
                attempt_id,
                {
                    "status": ExecutionAttemptStatus.REJECTED,
                    "stage": "REJECTED_EXCEPTION",
                    "reason": str(e),
                    "data": {"error": str(e)},
                },
            )
            return ExecutionResult(success=False, trade=trade, error=str(e))

        trade.status = TradeStatus.FAILED
        if trade.execution_state not in (
            ExecutionState.ROLLBACK_QUEUED,
            ExecutionState.ROLLBACK_IN_PROGRESS,
            ExecutionState.ROLLBACK_DONE,
            ExecutionState.ROLLBACK_FAILED,
        ):
            trade.execution_state = ExecutionState.FAILED
        await self._update_trade(trade)
        await self._kpi_update_attempt(
            attempt_id,
            {
                "status": ExecutionAttemptStatus.FAILED,
                "stage": "FAILED_EXCEPTION",
                "reason": str(e),
                "data": {"error": str(e)},
            },
        )
        return ExecutionResult(success=False, trade=trade, error=str(e))

    finally:
        if trade_persisted:
            self._active_executions.pop(trade.trade_id, None)
