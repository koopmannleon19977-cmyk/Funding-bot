"""
Execution post-leg2 logic for execution_impl.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from decimal import Decimal

from funding_bot.domain.events import TradeOpened
from funding_bot.domain.models import (
    Exchange,
    ExecutionAttemptStatus,
    Opportunity,
    OrderRequest,
    OrderType,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_helpers import _slippage_bps
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _execute_impl_post_leg2(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
    leg1_filled_monotonic: float,
) -> ExecutionResult:
    # Execute Leg 2 (X10 - Hedge)
    leg2_t0 = time.monotonic()
    await self._execute_leg2(trade, opp, leg1_filled_monotonic=leg1_filled_monotonic, attempt_id=attempt_id)
    leg2_seconds = Decimal(str(max(0.0, time.monotonic() - leg2_t0)))
    await self._kpi_update_attempt(
        attempt_id,
        {
            "stage": "LEG2_FILLED",
            "leg2_fill_seconds": leg2_seconds,
            "data": {
                "leg2_order_id": trade.leg2.order_id,
                "leg2_filled_qty": str(trade.leg2.filled_qty),
                "leg2_entry_price": str(trade.leg2.entry_price),
                "leg2_fees": str(trade.leg2.fees),
            },
        },
    )

    # Success - mark trade as open
    trade.mark_opened()
    await self._update_trade(trade)

    # --- P0.2: POST-ENTRY POSITION VERIFICATION (with retry for API lag) ---
    # Immediately verify both legs exist on exchanges to catch race conditions
    # where one leg may have failed/closed between placement and now.
    # FIX 2026-01-02: Add retry loop to handle API propagation delays that cause
    # false "Broken Hedge" alerts when position data takes 1-2s to appear.
    max_verify_attempts = 3
    verify_delay_seconds = 1.0
    verified_ok = False
    last_verify_lighter = None
    last_verify_x10 = None

    for verify_attempt in range(max_verify_attempts):
        try:
            verify_lighter = await self.lighter.get_position(trade.symbol)
            verify_x10 = await self.x10.get_position(trade.symbol)
            last_verify_lighter = verify_lighter
            last_verify_x10 = verify_x10

            qty_threshold = Decimal("0.0001")
            lighter_qty = _safe_decimal(getattr(verify_lighter, "qty", None))
            x10_qty = _safe_decimal(getattr(verify_x10, "qty", None))
            has_lighter = bool(verify_lighter) and (lighter_qty > qty_threshold)
            has_x10 = bool(verify_x10) and (x10_qty > qty_threshold)

            if has_lighter and has_x10:
                verified_ok = True
                logger.debug(
                    f"Post-entry verification OK for {trade.symbol} (attempt {verify_attempt + 1}): "
                    f"lighter_qty={verify_lighter.qty}, x10_qty={verify_x10.qty}"
                )
                break
            # One leg missing - could be API lag, retry
            missing = "LIGHTER" if not has_lighter else "X10"
            logger.warning(
                f"Post-entry verification attempt {verify_attempt + 1}/{max_verify_attempts}: "
                f"{trade.symbol} missing {missing}, retrying in {verify_delay_seconds}s..."
            )
            if verify_attempt < max_verify_attempts - 1:
                await asyncio.sleep(verify_delay_seconds)
        except Exception as verify_err:
            logger.warning(
                f"Post-entry verification attempt {verify_attempt + 1}/{max_verify_attempts} "
                f"failed for {trade.symbol}: {verify_err}"
            )
            if verify_attempt < max_verify_attempts - 1:
                await asyncio.sleep(verify_delay_seconds)

    # After all retries, check final state
    if not verified_ok:
        qty_threshold = Decimal("0.0001")
        last_lighter_qty = _safe_decimal(getattr(last_verify_lighter, "qty", None))
        last_x10_qty = _safe_decimal(getattr(last_verify_x10, "qty", None))
        has_lighter = bool(last_verify_lighter) and (last_lighter_qty > qty_threshold)
        has_x10 = bool(last_verify_x10) and (last_x10_qty > qty_threshold)

        if not has_lighter or not has_x10:
            missing_exchange = Exchange.LIGHTER if not has_lighter else Exchange.X10
            remaining_qty = last_lighter_qty if has_lighter else last_x10_qty if has_x10 else Decimal("0")
            logger.critical(
                f"POST-ENTRY VERIFICATION FAILED for {trade.symbol} after {max_verify_attempts} attempts: "
                f"missing {missing_exchange.value}! "
                f"lighter_qty={last_lighter_qty}, "
                f"x10_qty={last_x10_qty}"
            )

            # Import here to avoid circular import at module level
            from funding_bot.domain.events import BrokenHedgeDetected

            # Publish BrokenHedgeDetected to trigger global trading pause
            with contextlib.suppress(Exception):
                await self.event_bus.publish(
                    BrokenHedgeDetected(
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                        missing_exchange=missing_exchange,
                        remaining_qty=remaining_qty,
                        details={
                            "detection_point": "post_entry_verification",
                            "verify_attempts": max_verify_attempts,
                            "lighter_qty": str(last_lighter_qty),
                            "x10_qty": str(last_x10_qty),
                        },
                    )
                )
            # Best-effort emergency close of the remaining leg to reduce exposure.
            close_qty = Decimal("0")
            close_side = None
            adapter = None
            if missing_exchange == Exchange.LIGHTER:
                adapter = self.x10
                pos = last_verify_x10
                close_qty = _safe_decimal(getattr(pos, "qty", None)) if pos else trade.leg2.filled_qty
                close_side = (pos.side if pos and getattr(pos, "side", None) else trade.leg2.side).inverse()
            else:
                adapter = self.lighter
                pos = last_verify_lighter
                close_qty = _safe_decimal(getattr(pos, "qty", None)) if pos else trade.leg1.filled_qty
                close_side = (pos.side if pos and getattr(pos, "side", None) else trade.leg1.side).inverse()

            if adapter and close_side and close_qty > qty_threshold:
                logger.critical(
                    f"EMERGENCY CLOSE: {trade.symbol} remaining leg on {adapter.exchange.value} "
                    f"qty={close_qty} side={close_side.value}"
                )
                try:
                    request = OrderRequest(
                        symbol=trade.symbol,
                        exchange=adapter.exchange,
                        side=close_side,
                        qty=close_qty,
                        order_type=OrderType.MARKET,
                        reduce_only=True,
                    )
                    order = await adapter.place_order(request)
                    timeout_s = float(
                        _safe_decimal(
                            getattr(self.settings.execution, "hedge_ioc_fill_timeout_seconds", None),
                            Decimal("8.0"),
                        )
                    )
                    await self._wait_for_fill(adapter, trade.symbol, order.order_id, timeout=timeout_s)
                except Exception as close_err:
                    logger.error(f"EMERGENCY CLOSE FAILED for {trade.symbol} on {adapter.exchange.value}: {close_err}")

            trade.status = TradeStatus.CLOSING
            trade.close_reason = "post_entry_broken_hedge"
            await self.store.update_trade(
                trade.trade_id,
                {
                    "status": trade.status,
                    "close_reason": trade.close_reason,
                },
            )

    leg1_bps = _slippage_bps(side=trade.leg1.side, fill_price=trade.leg1.entry_price, reference_price=opp.mid_price)
    leg2_bps = _slippage_bps(side=trade.leg2.side, fill_price=trade.leg2.entry_price, reference_price=opp.mid_price)
    await self._kpi_update_attempt(
        attempt_id,
        {
            "status": ExecutionAttemptStatus.OPENED,
            "stage": "OPENED",
            "total_fees": trade.total_fees,
            "leg1_slippage_bps": leg1_bps,
            "leg2_slippage_bps": leg2_bps,
            "data": {
                "trade_status": trade.status.value,
                "execution_state": trade.execution_state.value,
            },
        },
    )

    # Publish event
    await self.event_bus.publish(
        TradeOpened(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            leg1_exchange=trade.leg1.exchange,
            leg2_exchange=trade.leg2.exchange,
            qty=trade.target_qty,
            notional_usd=trade.target_notional_usd,
            entry_apy=trade.entry_apy,
            leg1_price=trade.leg1.entry_price,
            leg2_price=trade.leg2.entry_price,
        )
    )

    logger.info(
        f"Trade {trade.trade_id[:8]} opened: {trade.symbol} "
        f"qty={trade.target_qty:.6f} "
        f"L1={trade.leg1.entry_price:.6f} L2={trade.leg2.entry_price:.6f}"
    )

    return ExecutionResult(success=True, trade=trade)
