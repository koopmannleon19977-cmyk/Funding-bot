"""
Pre-attempt position reconciliation for leg1.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.models import Opportunity, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg1_types import Leg1Config, Leg1State

logger = get_logger("funding_bot.services.execution")


async def _pre_attempt_position_check(
    self,
    trade: Trade,
    opp: Opportunity,
    config: Leg1Config,
    state: Leg1State,
    attempt_index: int,
) -> None:
    # [CRITICAL SAFETY] Check actual exchange position BEFORE placing new order
    # This prevents double-position bugs when previous fill was missed by WS/REST
    if attempt_index > 0 and config.initial_pos_qty is not None:
        try:
            pre_attempt_pos = await self.lighter.get_position(trade.symbol)
            pre_attempt_pos_qty = pre_attempt_pos.qty if pre_attempt_pos else Decimal("0")
            position_delta = pre_attempt_pos_qty - config.initial_pos_qty

            state.last_pos_delta = position_delta

            if position_delta > state.total_filled + config.pos_tolerance:
                missed_fill = position_delta - state.total_filled
                logger.warning(
                    f"PRE-ATTEMPT FILL DETECTION for {trade.symbol}! "
                    f"Position Delta={position_delta:.6f} > tracked total_filled={state.total_filled:.6f}. "
                    f"Assimilating missed fill of {missed_fill:.6f} coins."
                )
                # Estimate avg price from last attempt's limit price (conservative)
                last_price_estimate = trade.leg1.entry_price if trade.leg1.entry_price > 0 else Decimal("0")
                if last_price_estimate <= 0:
                    price_data = self.market_data.get_price(trade.symbol)
                    last_price_estimate = price_data.lighter_price if price_data else Decimal("0")

                state.total_filled += missed_fill
                state.total_notional += missed_fill * last_price_estimate

                trade.leg1.filled_qty = state.total_filled
                if state.total_filled > 0:
                    trade.leg1.entry_price = state.total_notional / state.total_filled
                await self._update_trade(trade)

                logger.info(f"Updated total_filled to {state.total_filled:.6f} after pre-attempt position check")
        except Exception as pos_check_err:
            logger.warning(f"Pre-attempt position check failed: {pos_check_err}")
