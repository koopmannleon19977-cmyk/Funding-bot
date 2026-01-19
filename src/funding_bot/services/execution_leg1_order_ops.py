"""
Order placement and fill waiting helpers for leg1.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from funding_bot.domain.errors import Leg1HedgeEvaporatedError
from funding_bot.domain.models import Exchange, Opportunity, Order, OrderRequest, OrderStatus, OrderType, Side, Trade
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_leg1_types import Leg1AttemptContext, Leg1State
from funding_bot.services.liquidity_gates import check_x10_depth_compliance_by_impact
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _cancel_lighter_order_if_active(
    self,
    trade: Trade,
    order_id: str | None,
) -> tuple[bool, int]:
    if not order_id:
        return True, 0
    cancel_attempts = 0
    for cancel_attempt in range(3):
        try:
            cancel_attempts = cancel_attempt + 1
            status_check = await self.lighter.get_order(trade.symbol, order_id)
            if not status_check or not status_check.is_active:
                return True, cancel_attempts

            logger.debug(f"Cancelling remainder of order {order_id} (Attempt {cancel_attempt + 1})")
            cancelled = await self.lighter.cancel_order(trade.symbol, order_id)
            if cancelled:
                return True, cancel_attempts
        except Exception as ce:
            logger.warning(f"Cancel attempt {cancel_attempt + 1} failed: {ce}")

        await asyncio.sleep(0.5)

    return False, cancel_attempts


async def _place_or_modify_leg1_order(
    self,
    trade: Trade,
    ctx: Leg1AttemptContext,
    state: Leg1State,
) -> tuple[Order, bool]:
    # Modified Logic: Check if we can modify the existing order instead of cancel/replace
    order = None
    modified = False
    old_order_id = state.active_order_id  # Track old order for post-placement verification

    if state.use_modify_next and state.active_order_id:
        logger.debug(f"Attempting modify_order for {state.active_order_id} to price {ctx.price}")
        try:
            modified = await self.lighter.modify_order(
                symbol=trade.symbol,
                order_id=state.active_order_id,
                price=ctx.price,
                qty=ctx.remaining_qty,
            )
            if modified:
                trade.leg1.order_id = state.active_order_id
                logger.info(f"Leg1 order modified: {state.active_order_id} @ {ctx.price:.6f}")

                # Create wrapper order object for downstream logic
                order = Order(
                    order_id=state.active_order_id,
                    symbol=trade.symbol,
                    exchange=Exchange.LIGHTER,
                    side=trade.leg1.side,
                    qty=ctx.remaining_qty,
                    price=ctx.price,
                    order_type=OrderType.LIMIT,
                    status=OrderStatus.OPEN,
                    client_order_id="",
                )
            else:
                logger.warning(f"Modify failed for {state.active_order_id}, falling back to new order")
                await _cancel_lighter_order_if_active(self, trade, state.active_order_id)
                state.use_modify_next = False
        except Exception as e:
            logger.warning(f"Modify exception for {state.active_order_id}: {e}")
            await _cancel_lighter_order_if_active(self, trade, state.active_order_id)
            state.use_modify_next = False

    if not modified:
        request = OrderRequest(
            symbol=trade.symbol,
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side,
            qty=ctx.remaining_qty,
            order_type=OrderType.LIMIT,
            price=ctx.price,
            time_in_force=ctx.time_in_force,
        )
        order = await self.lighter.place_order(request)

        # === D4: POST-REPLACEMENT VERIFICATION (Race Condition Fix) ===
        # After placing new order, verify old order didn't fill during async gap.
        # This prevents double-exposure race condition where:
        # 1. Cancel old order (get confirmation)
        # 2. Place new order
        # 3. [RACE] Old order fills between step 1-2 due to exchange processing delay
        if old_order_id and old_order_id != order.order_id:
            try:
                old_order_status = await self.lighter.get_order(trade.symbol, old_order_id)
                if old_order_status and old_order_status.status == OrderStatus.FILLED:
                    # Race condition detected: old order filled during async gap
                    # Cancel new order to prevent double exposure
                    logger.critical(
                        f"RACE CONDITION: Old order {old_order_id} filled during replacement. "
                        f"Cancelling new order {order.order_id} to prevent double exposure."
                    )
                    try:
                        await self.lighter.cancel_order(trade.symbol, order.order_id)
                    except Exception as cancel_err:
                        logger.warning(f"Failed to cancel new order {order.order_id}: {cancel_err}")
                    raise RuntimeError(
                        f"Order replacement race condition: Old order {old_order_id} filled "
                        f"during async gap before new order {order.order_id} was placed. "
                        f"New order cancelled to prevent double exposure."
                    )
            except Exception as e:
                # Don't fail on verification errors - log and continue
                # The reconciliation system will catch any actual double-exposure issues
                logger.warning(f"Post-replacement verification failed for {old_order_id}: {e}")

    if order is None:
        raise RuntimeError("Leg1 order placement failed with no order returned")

    trade.leg1.order_id = order.order_id
    # IMPORTANT: set `active_order_id` immediately so exception handlers and anti-salvage
    # paths never try to cancel a `None`/empty id.
    state.active_order_id = order.order_id
    logger.debug(f"Leg1 order placed: {order.order_id} @ {ctx.price:.2f}")

    return order, modified


async def _wait_for_leg1_fill_with_guard(
    self,
    trade: Trade,
    opp: Opportunity,
    order: Order,
    maker_wait_timeout: float,
) -> tuple[Order | None, Decimal, str]:
    t_wait0 = time.monotonic()

    # Define safe-guard callback to monitor hedge liquidity while waiting.
    # If hedge liquidity evaporates, this will cancel Leg 1 immediately.
    async def ensure_hedge_integrity() -> bool:
        try:
            # 1. Calculate what we still need to hedge
            pending_hedge_qty = trade.target_qty - trade.leg1.filled_qty
            if pending_hedge_qty <= 0:
                return True

            # 2. Get X10 liquidity snapshot (best effort, small cache ok)
            # We use a slightly looser check than pre-flight to avoid noise,
            # but stricter than "nothing".
            x10_book = await self._get_best_effort_orderbook(trade.symbol, max_age_seconds=1.0)

            avail_qty = Decimal("0")
            if x10_book:
                if trade.leg2.side == Side.SELL:
                    avail_qty = x10_book.x10_bid_qty if x10_book.x10_bid > 0 else Decimal("0")
                else:
                    avail_qty = x10_book.x10_ask_qty if x10_book.x10_ask > 0 else Decimal("0")

            # 3. Check if liquidity dropped below critical threshold
            # Threshold: We need at least X% of pending size to be available.
            # Relaxed to 80% to improve fill rate while maintaining core safety.
            min_req_qty = pending_hedge_qty * Decimal("0.8")

            # Use min notional check as absolute floor (e.g. $10)
            mid = opp.mid_price
            if mid > 0 and (min_req_qty * mid) < Decimal("10"):
                # If remaining part is dust, don't abort.
                pass
            elif avail_qty < min_req_qty:
                # L1 is not enough. Check if L2/Depth has enough within impact.
                # preventing unnecessary rollbacks when L1 flutters but depth is solid.
                depth_saved = False
                try:
                    depth_impact_raw = getattr(self.settings.trading, "depth_gate_max_price_impact_percent", None)
                    depth_impact = _safe_decimal(depth_impact_raw, Decimal("0.0015"))

                    x10_depth = await self.market_data.get_fresh_orderbook_depth(trade.symbol, levels=10)
                    # Check if we can find the required quantity (80% of pending) within impact limits
                    depth_res = check_x10_depth_compliance_by_impact(
                        x10_depth,
                        x10_side=trade.leg2.side,
                        target_qty=min_req_qty,
                        target_notional_usd=min_req_qty * opp.mid_price,
                        min_l1_notional_usd=Decimal("0"),
                        min_l1_notional_multiple=Decimal("0"),
                        max_l1_qty_utilization=Decimal("1.0"),
                        max_price_impact_pct=depth_impact,
                    )
                    if depth_res.passed:
                        depth_saved = True
                except Exception as e_depth:
                    logger.debug(f"Anti-Salvage depth check failed: {e_depth}")

                if not depth_saved:
                    logger.warning(
                        f"ANTI-SALVAGE: Hedge liquidity evaporated for {trade.symbol}! "
                        f"Need {pending_hedge_qty:.4f}, Avail {avail_qty:.4f}. Cancelling Leg 1."
                    )
                    # Cancel immediately
                    await _cancel_lighter_order_if_active(self, trade, order.order_id)
                    # Raising error will break the wait loop and trigger rollback/failure handling
                    raise Leg1HedgeEvaporatedError(
                        f"Hedge liquidity evaporated: Need {pending_hedge_qty}, Avail {avail_qty}",
                        symbol=trade.symbol,
                    ) from None

            return True
        except Leg1HedgeEvaporatedError:
            raise
        except Exception as e:
            logger.warning(f"Anti-Salvage check failed (ignoring): {e}")
            return True

    filled_order = await self._wait_for_fill(
        self.lighter,
        trade.symbol,
        order.order_id,
        timeout=maker_wait_timeout,
        check_callback=ensure_hedge_integrity,
    )
    wait_seconds = Decimal(str(max(0.0, time.monotonic() - t_wait0)))
    active_order_id = order.order_id
    if filled_order and filled_order.order_id and not str(filled_order.order_id).startswith("pending_"):
        # Prefer the system order_id from WS/polling once available so downstream
        # cancels and persisted state don't depend on placeholder resolution.
        active_order_id = str(filled_order.order_id)
        trade.leg1.order_id = active_order_id

    return filled_order, wait_seconds, active_order_id
