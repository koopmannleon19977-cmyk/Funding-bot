"""
Finalize helpers for leg1 execution.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.errors import Leg1FailedError
from funding_bot.domain.events import LegFilled
from funding_bot.domain.models import Exchange, ExecutionState, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg1_types import Leg1State
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _finalize_leg1_execution(
    self,
    trade: Trade,
    state: Leg1State,
) -> None:
    # Final Evaluation after Loop
    if state.total_filled > 0:
        price = trade.leg1.entry_price
        min_hedge_usd = _safe_decimal(self.settings.execution.microfill_max_unhedged_usd, Decimal("5.0"))
        min_hedge_qty = min_hedge_usd / price if (price and price > 0) else Decimal("0")

        if state.success or state.total_filled >= min_hedge_qty:
            # If we proceed with a partial maker fill, clamp the trade sizing to the
            # actually-opened quantity so downstream exposure/accounting can't assume the
            # original target size.
            if not state.success and state.total_filled < trade.target_qty:
                trade.target_qty = state.total_filled
                trade.leg1.qty = state.total_filled
                trade.leg2.qty = state.total_filled
                if trade.leg1.entry_price and trade.leg1.entry_price > 0:
                    trade.target_notional_usd = state.total_filled * trade.leg1.entry_price

            # MARK SUCCESS
            trade.execution_state = ExecutionState.LEG1_FILLED
            await self._update_trade(trade)

            # Publish event
            await self.event_bus.publish(
                LegFilled(
                    trade_id=trade.trade_id,
                    symbol=trade.symbol,
                    exchange=Exchange.LIGHTER,
                    side=trade.leg1.side,
                    order_id=trade.leg1.order_id or "aggregated",
                    qty=state.total_filled,
                    price=trade.leg1.entry_price,
                    fee=state.total_fees,
                )
            )
            logger.info(f"Leg1 finalized: {state.total_filled:.6f} @ {trade.leg1.entry_price:.6f}")
            return

        # ABORT AND CLEANUP (Too small to hedge)
        logger.warning(f"Final fill too small to hedge: {state.total_filled:.6f} < {min_hedge_qty:.6f}. Closing dust.")
        await self._rollback(trade, f"Aggregate fill too small to hedge ({state.total_filled})")
        raise Leg1FailedError(
            f"Fill too small to hedge ({state.total_filled})",
            symbol=trade.symbol,
        )

    # No fill at all
    trade.execution_state = ExecutionState.ABORTED
    raise Leg1FailedError(
        f"Leg1 not filled after {self.settings.execution.maker_order_max_retries} attempts",
        symbol=trade.symbol,
    )
