"""
Execution flow helpers (moved from ExecutionEngine).
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from funding_bot.domain.events import (
    RollbackCompleted,
    RollbackInitiated,
)
from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    OrderRequest,
    OrderType,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _rollback(self, trade: Trade, reason: str = "Leg2 failed") -> None:
    """Rollback Leg1 due to failed Leg2."""
    trade.status = TradeStatus.ROLLBACK
    trade.execution_state = ExecutionState.ROLLBACK_QUEUED
    await self._update_trade(trade)

    await self.event_bus.publish(
        RollbackInitiated(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            reason=reason,
            leg_to_close=Exchange.LIGHTER,
            qty=trade.leg1.filled_qty,
        )
    )

    logger.info(f"Initiating rollback for {trade.symbol}: {reason}")

    trade.execution_state = ExecutionState.ROLLBACK_IN_PROGRESS
    await self._update_trade(trade)

    try:
        # Prefer live position qty for the close size (trade.leg1.filled_qty can be stale when
        # WS/polling misses a fill event or when a late fill happens after a cancel attempt).
        qty_to_close = trade.leg1.filled_qty
        with contextlib.suppress(Exception):
            pos = await self.lighter.get_position(trade.symbol)
            if pos and pos.qty and pos.qty > 0:
                qty_to_close = pos.qty
                trade.leg1.filled_qty = qty_to_close
                await self._update_trade(trade)

        if qty_to_close <= 0:
            trade.execution_state = ExecutionState.ROLLBACK_DONE
            await self._update_trade(trade)
            with contextlib.suppress(Exception):
                await self.event_bus.publish(
                    RollbackCompleted(
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                        success=True,
                        loss_usd=Decimal("0"),
                    )
                )
            logger.info("Rollback complete: no position to close")
            trade.status = TradeStatus.FAILED
            await self._update_trade(trade)
            return

        # Place reduce-only market order to close Leg1
        request = OrderRequest(
            symbol=trade.symbol,
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side.inverse(),
            qty=qty_to_close,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )

        order = await self.lighter.place_order(request)

        # Wait for fill
        filled = await self._wait_for_fill(
            self.lighter,
            trade.symbol,
            order.order_id,
            timeout=10.0,
        )

        # Confirmation: market close orders can fill before we observe a fill event. Prefer:
        # 1) observed fill, else 2) order status, else 3) position is flat.
        if not filled or not (filled.filled_qty and filled.filled_qty > 0):
            with contextlib.suppress(Exception):
                refreshed = await self.lighter.get_order(trade.symbol, order.order_id)
                if refreshed and refreshed.filled_qty and refreshed.filled_qty > 0:
                    filled = refreshed

        pos_closed = False
        with contextlib.suppress(Exception):
            pos_after = await self.lighter.get_position(trade.symbol)
            if pos_after is None or pos_after.qty <= Decimal("0"):
                pos_closed = True

        if filled and filled.filled_qty > 0:
            trade.execution_state = ExecutionState.ROLLBACK_DONE
            trade.leg1.exit_price = filled.avg_fill_price

            # Calculate rollback slippage loss.
            # entry_price may be 0 or stale if rollback happens before Leg1 finalization.
            # In that case, use the exit_price as entry approximation (slippage ≈ 0).
            entry_price = trade.leg1.entry_price or filled.avg_fill_price
            notional_closed = filled.avg_fill_price * filled.filled_qty

            if trade.leg1.side == Side.BUY:
                slippage_loss = (entry_price - filled.avg_fill_price) * filled.filled_qty
            else:
                slippage_loss = (filled.avg_fill_price - entry_price) * filled.filled_qty

            await self.event_bus.publish(
                RollbackCompleted(
                    trade_id=trade.trade_id,
                    symbol=trade.symbol,
                    success=True,
                    loss_usd=abs(slippage_loss),
                )
            )

            # CRITICAL FIX: Verify position is actually closed after MARKET order
            # The fill confirmation may be stale/cached - always verify actual position
            if not pos_closed:
                with contextlib.suppress(Exception):
                    pos_verify = await self.lighter.get_position(trade.symbol)
                    if pos_verify is None or pos_verify.qty <= Decimal("0"):
                        pos_closed = True
                    else:
                        logger.error(
                            f"ROLLBACK VERIFICATION FAILED: {trade.symbol} position "
                            f"still open (qty={pos_verify.qty}) despite fill confirmation. "
                            f"MANUAL ACTION REQUIRED!"
                        )
                        trade.execution_state = ExecutionState.ROLLBACK_FAILED
                        await self.event_bus.publish(
                            RollbackCompleted(
                                trade_id=trade.trade_id,
                                symbol=trade.symbol,
                                success=False,
                                loss_usd=Decimal("0"),
                            )
                        )
                        return

            logger.info(f"Rollback complete: notional=${notional_closed:.2f}, slippage_loss=${abs(slippage_loss):.4f}")

            # Safety: if Leg2 partially filled before rollback, flatten X10 too to avoid naked exposure.
            if trade.leg2.filled_qty and trade.leg2.filled_qty > 0:
                try:
                    logger.warning(
                        f"Rollback: closing partial Leg2 exposure on X10: {trade.leg2.filled_qty} {trade.symbol}"
                    )
                    x10_close_req = OrderRequest(
                        symbol=trade.symbol,
                        exchange=Exchange.X10,
                        side=trade.leg2.side.inverse(),
                        qty=trade.leg2.filled_qty,
                        order_type=OrderType.MARKET,  # adapter emulates via aggressive LIMIT IOC
                        reduce_only=True,
                    )
                    x10_order = await self.x10.place_order(x10_close_req)
                    x10_timeout = float(
                        _safe_decimal(
                            getattr(
                                self.settings.execution,
                                "hedge_ioc_fill_timeout_seconds",
                                None,
                            ),
                            default=Decimal("8.0"),
                        )
                    )
                    await self._wait_for_fill(
                        self.x10,
                        trade.symbol,
                        x10_order.order_id,
                        timeout=x10_timeout,
                    )
                except Exception as e:
                    logger.error(f"Rollback: failed to close partial Leg2 on X10 (MANUAL ACTION REQUIRED): {e}")

        else:
            # If the position is already flat, treat rollback as successful even if we couldn't
            # observe a fill event for the close order.
            if pos_closed:
                trade.execution_state = ExecutionState.ROLLBACK_DONE
                await self.event_bus.publish(
                    RollbackCompleted(
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                        success=True,
                        loss_usd=Decimal("0"),
                    )
                )
                logger.info("Rollback complete: position already closed")
            else:
                trade.execution_state = ExecutionState.ROLLBACK_FAILED
                logger.error("Rollback failed: could not close position")

    except Exception as e:
        trade.execution_state = ExecutionState.ROLLBACK_FAILED
        logger.exception(f"Rollback error: {e}")

    trade.status = TradeStatus.FAILED
    await self._update_trade(trade)
