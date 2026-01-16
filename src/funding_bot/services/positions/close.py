"""
Close and verification logic for positions.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.events import AlertEvent, TradeClosed
from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.services.positions.types import CloseResult, PositionsStillOpenError
from funding_bot.services.positions.utils import _round_price_to_tick
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger(__name__)


# =============================================================================
# STRUCTURED LOGGING: Final Execution Metrics (post-close)
# =============================================================================

def _log_final_execution_metrics(
    trade: Trade,
    leg1_vwap: Decimal,
    leg2_vwap: Decimal,
    leg1_fees: Decimal,
    leg2_fees: Decimal,
    leg1_readback_corrected: bool,
    leg2_readback_corrected: bool,
    total_pnl: Decimal,
    funding_collected: Decimal,
) -> None:
    """
    Log structured final execution metrics after close.

    Format: JSON-structured with safe value types (no secrets).
    """
    log_data = {
        "event": "final_execution_metrics",
        "timestamp": datetime.now(UTC).isoformat(),
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "close_reason": trade.close_reason or "UNKNOWN",
        "hold_duration_hours": float(trade.hold_duration_seconds / 3600),
        # VWAP per leg (final exit prices)
        "leg1_vwap": float(leg1_vwap) if leg1_vwap > 0 else None,
        "leg2_vwap": float(leg2_vwap) if leg2_vwap > 0 else None,
        # Fees per leg (final, after readback corrections)
        "leg1_fees_usd": float(leg1_fees) if leg1_fees > 0 else 0.0,
        "leg2_fees_usd": float(leg2_fees) if leg2_fees > 0 else 0.0,
        "total_fees_usd": float(leg1_fees + leg2_fees),
        # Readback corrections (if applied)
        "leg1_readback_corrected": leg1_readback_corrected,
        "leg2_readback_corrected": leg2_readback_corrected,
        # Final PnL breakdown
        "total_pnl_usd": float(total_pnl),
        "funding_collected_usd": float(funding_collected),
        "realized_pnl_usd": float(total_pnl - funding_collected),
    }

    # Log as info (for monitoring/dashboards/trade reconciliation)
    logger.info(
        "CLOSE_FINAL: %s %s VWAP(leg1)=%.4f VWAP(leg2)=%.4f fees(leg1)=$%.2f fees(leg2)=$%.2f net=$%.2f",
        trade.symbol,
        trade.trade_id[:8],
        leg1_vwap if leg1_vwap > 0 else 0,
        leg2_vwap if leg2_vwap > 0 else 0,
        leg1_fees,
        leg2_fees,
        total_pnl,
        extra={"structured_data": log_data},
    )


def _delta_from_cumulative_fill(
    order_id: str,
    cum_qty: Decimal,
    cum_fee: Decimal,
    fill_price: Decimal,
    qty_seen: dict[str, Decimal],
    fee_seen: dict[str, Decimal],
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Convert cumulative filled_qty and fee for an order into deltas.
    Returns (delta_qty, delta_notional, delta_fee).
    Guards against negative deltas (API resets/out-of-order updates).
    """
    prev_qty = qty_seen.get(order_id, Decimal("0"))
    prev_fee = fee_seen.get(order_id, Decimal("0"))

    delta_qty = cum_qty - prev_qty
    delta_fee = cum_fee - prev_fee

    if delta_qty < 0:
        delta_qty = Decimal("0")
    if delta_fee < 0:
        delta_fee = Decimal("0")

    if delta_qty > 0:
        qty_seen[order_id] = cum_qty
    if delta_fee > 0:
        fee_seen[order_id] = cum_fee

    delta_notional = delta_qty * fill_price
    return delta_qty, delta_notional, delta_fee


def _log_close_order_placed(
    trade: Trade,
    exchange: Exchange,
    leg: str,  # "leg1" or "leg2"
    order_id: str,
    client_order_id: str | None,
    side: Side,
    qty: Decimal,
    price: Decimal,
    time_in_force: TimeInForce,
    post_only: bool = False,
    attempt_id: int | None = None,
) -> None:
    """
    Log a close order placement event to trade.events.

    This event is critical for T3 post-close readback, which reconstructs
    final VWAP and fees from exchange API by collecting all close order IDs.
    """
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "CLOSE_ORDER_PLACED",
        "exchange": exchange.value,
        "leg": leg,
        "order_id": str(order_id),
        "client_order_id": str(client_order_id) if client_order_id else None,
        "side": side.value,
        "qty": str(qty),
        "price": str(price),
        "time_in_force": time_in_force.value,
        "post_only": post_only,
        "attempt_id": attempt_id,
    })
    logger.debug(
        f"Close order logged: {exchange.value} {leg} order_id={order_id} "
        f"side={side.value} qty={qty} price={price}"
    )


def _log_rebalance_start(
    trade: Trade,
    net_delta: Decimal,
    leg1_notional: Decimal,
    leg2_notional: Decimal,
    rebalance_exchange: Exchange,
    rebalance_notional: Decimal,
) -> None:
    """Log rebalance start event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "REBALANCE_START",
        "net_delta": str(net_delta),
        "leg1_notional": str(leg1_notional),
        "leg2_notional": str(leg2_notional),
        "rebalance_exchange": rebalance_exchange.value,
        "rebalance_notional": str(rebalance_notional),
    })
    logger.info(f"Rebalance start logged: {rebalance_exchange.value} notional=${rebalance_notional:.2f}")


def _log_rebalance_order_placed(
    trade: Trade,
    exchange: Exchange,
    order_id: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
    time_in_force: TimeInForce,
    post_only: bool = False,
) -> None:
    """Log rebalance order placement event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "REBALANCE_ORDER_PLACED",
        "exchange": exchange.value,
        "order_id": str(order_id),
        "side": side.value,
        "qty": str(qty),
        "price": str(price),
        "time_in_force": time_in_force.value,
        "post_only": post_only,
    })
    logger.info(f"Rebalance order logged: {exchange.value} order_id={order_id} side={side.value} qty={qty}")


def _log_rebalance_complete(
    trade: Trade,
    filled_qty: Decimal,
    avg_price: Decimal,
    fee: Decimal,
) -> None:
    """Log rebalance complete event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "REBALANCE_COMPLETE",
        "filled_qty": str(filled_qty),
        "avg_price": str(avg_price),
        "fee": str(fee),
    })
    logger.info(f"Rebalance complete logged: filled={filled_qty} @ {avg_price:.2f}")


def _log_coordinated_close_start(trade: Trade) -> None:
    """Log coordinated close start event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "COORDINATED_CLOSE_START",
    })
    logger.info(f"Coordinated close start logged for {trade.symbol}")


def _log_coordinated_close_maker_placed(
    trade: Trade,
    exchange: Exchange,
    leg: str,
    order_id: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
) -> None:
    """Log coordinated close maker order placement event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "COORDINATED_CLOSE_MAKER_PLACED",
        "exchange": exchange.value,
        "leg": leg,
        "order_id": str(order_id),
        "side": side.value,
        "qty": str(qty),
        "price": str(price),
    })
    logger.info(f"Coordinated close maker logged: {exchange.value} {leg} order_id={order_id}")


def _log_coordinated_close_ioc_escalate(
    trade: Trade,
    legs: list[str],
) -> None:
    """Log coordinated close IOC escalation event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "COORDINATED_CLOSE_IOC_ESCALATE",
        "legs": legs,
    })
    logger.info(f"Coordinated close IOC escalate logged: legs={legs}")


def _log_coordinated_close_complete(
    trade: Trade,
    filled_legs: set[str],
) -> None:
    """Log coordinated close complete event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "COORDINATED_CLOSE_COMPLETE",
        "filled_legs": list(filled_legs),
    })
    logger.info(f"Coordinated close complete logged: legs={filled_legs}")


def _close_get_order_timeout_seconds(self) -> float:
    """
    Per-call timeout for exchange `get_order` during close.

    Avoids close flows stalling indefinitely when an adapter blocks while resolving
    pending IDs / waiting for WS updates.
    """
    es = getattr(self.settings, "execution", None)
    timeout = getattr(es, "close_get_order_timeout_seconds", None)
    try:
        timeout_s = float(timeout) if timeout is not None else 2.0
    except Exception:
        timeout_s = 2.0
    # Guardrails: never 0, never excessively large.
    return max(0.1, min(timeout_s, 10.0))


def _is_x10_reduce_only_missing_position_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "position is missing for reduce-only order" in msg
        or "code\":1137" in msg
        or "code 1137" in msg
        or "error\":{\"code\":1137" in msg
    )


# =============================================================================
# Helper Classes for Lighter Close Configuration
# =============================================================================

class _LighterCloseConfig:
    """Configuration for Lighter close operations."""

    def __init__(
        self,
        max_attempts: int,
        attempt_timeout: float,
        total_timeout: float,
        use_fast_close: bool = False,
        use_taker_directly: bool = False,
    ):
        self.max_attempts = max_attempts
        self.attempt_timeout = attempt_timeout
        self.total_timeout = total_timeout
        self.use_fast_close = use_fast_close
        self.use_taker_directly = use_taker_directly


def _get_lighter_close_config(self, trade: Trade) -> _LighterCloseConfig:
    """Determine close configuration based on trade close reason."""
    close_reason = trade.close_reason or ""
    is_early_tp = close_reason.startswith("EARLY_TAKE_PROFIT")
    is_apy_crash = "APY crashed" in close_reason
    fast_close_enabled = bool(
        getattr(self.settings.trading, "early_tp_fast_close_enabled", True)
    )
    use_fast_close = (is_early_tp or is_apy_crash) and fast_close_enabled

    if use_fast_close:
        use_taker_directly = bool(
            getattr(self.settings.trading, "early_tp_fast_close_use_taker_directly", False)
        )
        if use_taker_directly:
            return _LighterCloseConfig(
                max_attempts=0,
                attempt_timeout=0.0,
                total_timeout=0.0,
                use_fast_close=True,
                use_taker_directly=True,
            )

        max_attempts = int(getattr(
            self.settings.trading, "early_tp_fast_close_max_attempts", 2
        ))
        attempt_timeout = float(getattr(
            self.settings.trading, "early_tp_fast_close_attempt_timeout_seconds", 5.0
        ))
        total_timeout = float(getattr(
            self.settings.trading, "early_tp_fast_close_total_timeout_seconds", 30.0
        ))
        return _LighterCloseConfig(
            max_attempts=max_attempts,
            attempt_timeout=attempt_timeout,
            total_timeout=total_timeout,
            use_fast_close=True,
        )
    else:
        # Standard maker-chase config
        return _LighterCloseConfig(
            max_attempts=5,
            attempt_timeout=10.0,
            total_timeout=float("inf"),  # No total limit
            use_fast_close=False,
        )


def _should_skip_lighter_close(
    self,
    trade: Trade,
    pos,
    pos_fetch_ok: bool,
) -> tuple[bool, str | None]:
    """
    Check if Lighter close should be skipped based on position state.

    Returns:
        (should_skip, reason) - If should_skip is True, reason contains explanation
    """
    qty_threshold = Decimal("0.0001")
    close_reason = trade.close_reason or ""
    missing_lighter_expected = "_missing_lighter" in close_reason

    if not pos_fetch_ok:
        # Position fetch failed - proceed with best-effort close
        return False, None

    if not pos or pos.qty <= qty_threshold:
        if missing_lighter_expected:
            return True, f"Lighter position not found/empty for {trade.symbol}; skipping (expected)"
        # If the exchange confirms there is no position, do NOT attempt a close
        return True, f"Lighter position not found/empty for {trade.symbol}; skipping (safety)"

    if getattr(pos, "side", None) and pos.side != trade.leg1.side:
        return True, (
            f"Lighter position side mismatch for {trade.symbol}: "
            f"expected={trade.leg1.side.value} actual={pos.side.value}"
        )

    return False, None


async def _ensure_lighter_websocket(self, trade: Trade) -> None:
    """
    Ensure Lighter Account WebSocket is subscribed for instant fill detection.
    This avoids detection latency during close operations.
    """
    ensure_ws_fn = getattr(self.lighter, "ensure_orders_ws_market", None)
    if ensure_ws_fn:
        try:
            await asyncio.wait_for(ensure_ws_fn(trade.symbol, timeout=5.0), timeout=6.0)
            logger.debug(f"Ensured Lighter Account WS subscribed for {trade.symbol} close")
        except Exception as e:
            logger.warning(f"Failed to ensure Lighter WS for {trade.symbol}: {e}")


def _calculate_close_price(
    self,
    trade: Trade,
    close_side: Side,
) -> Decimal:
    """Calculate optimal price for closing Lighter position."""
    price_data = self.market_data.get_price(trade.symbol)
    if not price_data:
        raise ValueError("No price data available")

    # Start with mid-price fallback
    if close_side == Side.BUY:
        # Closing Short -> Buy at Best Bid
        price = price_data.lighter_price * Decimal("0.9999")
    else:
        # Closing Long -> Sell at Best Ask
        price = price_data.lighter_price * Decimal("1.0001")

    # Refine with Orderbook if available
    if hasattr(self.market_data, "get_orderbook"):
        ob = self.market_data.get_orderbook(trade.symbol)
        if ob:
            if close_side == Side.BUY and ob.lighter_bid > 0:
                price = ob.lighter_bid
            elif close_side == Side.SELL and ob.lighter_ask > 0:
                price = ob.lighter_ask

    # Round to tick
    lighter_info = self.market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
    tick = lighter_info.tick_size if lighter_info else Decimal("0.01")
    rounding = "ceil" if close_side == Side.SELL else "floor"
    return _round_price_to_tick(price, tick, rounding=rounding)


# =============================================================================
# Grid Step Close Configuration
# =============================================================================

class _GridStepConfig:
    """Configuration for grid step close operation."""

    def __init__(
        self,
        enabled: bool,
        grid_step: Decimal,
        num_levels: int,
        qty_per_level: Decimal,
    ):
        self.enabled = enabled
        self.grid_step = grid_step
        self.num_levels = num_levels
        self.qty_per_level = qty_per_level


async def _get_l1_depth_usd(self, symbol: str, close_side: Side) -> Decimal:
    """Calculate L1 depth in USD for the given side."""
    try:
        ob = self.market_data.get_orderbook(symbol)
        if not ob:
            return Decimal("0")

        if close_side == Side.BUY:
            # Closing Short: need Ask depth
            l1_qty = ob.lighter_ask_qty if hasattr(ob, 'lighter_ask_qty') else Decimal("0")
            l1_price = ob.lighter_ask if ob.lighter_ask > 0 else Decimal("1")
        else:
            # Closing Long: need Bid depth
            l1_qty = ob.lighter_bid_qty if hasattr(ob, 'lighter_bid_qty') else Decimal("0")
            l1_price = ob.lighter_bid if ob.lighter_bid > 0 else Decimal("1")

        if l1_price <= 0:
            l1_price = Decimal("1")

        return l1_qty * l1_price
    except Exception as e:
        logger.debug(f"Failed to get L1 depth for {symbol}: {e}")
        return Decimal("0")


async def _get_recent_volatility(self, symbol: str) -> Decimal:
    """
    Calculate recent price volatility.

    Returns volatility as a decimal (e.g., 0.01 = 1%).

    Simplified version without historical candle data.
    """
    try:
        # Use orderbook spread as MINIMUM volatility bound
        # Spread is the tightest possible volatility (market noise floor)
        ob = self.market_data.get_orderbook(symbol)
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            spread_pct = (ob.lighter_ask - ob.lighter_bid) / ob.lighter_bid
            # Spread is typically tighter than volatility, so we use it as a lower bound
            # Multiply by 2-3x to get realistic volatility from spread
            estimated_volatility = min(spread_pct * Decimal("3"), Decimal("0.02"))  # Cap at 2%

            logger.debug(
                f"Grid Step: Using spread-based volatility for {symbol}: "
                f"{estimated_volatility:.2%} (spread={spread_pct:.2%})"
            )
            return estimated_volatility

        # Fallback: Use symbol-specific default volatility
        # Different symbols have different inherent volatility
        symbol_defaults = {
            "BTC": Decimal("0.008"),   # 0.8% - Bitcoin is less volatile
            "ETH": Decimal("0.012"),   # 1.2% - Ethereum moderately volatile
            "SOL": Decimal("0.02"),    # 2.0% - Solana more volatile
            "DOGE": Decimal("0.025"),  # 2.5% - Memecoins volatile
        }

        default_vol = symbol_defaults.get(symbol, Decimal("0.01"))  # Default 1%

        logger.debug(f"Grid Step: Using default volatility for {symbol}: {default_vol:.2%}")
        return default_vol

    except Exception as e:
        logger.debug(f"Grid Step: Failed to calculate volatility for {symbol}: {e}")
        return Decimal("0.01")  # Conservative default: 1%  # Conservative default: 1%


async def _should_use_grid_step_async(
    self,
    trade: Trade,
    close_side: Side,
    qty_usd: Decimal,
) -> bool:
    """
    Determine if grid step should be used for this close (async version).

    Grid step is used when:
    1. Grid step is enabled in config
    2. Position is large enough (> min_notional_usd)
    3. L1 depth is insufficient (< position size * l1_depth_trigger)
    """
    try:
        es = self.settings.execution
        if not getattr(es, 'grid_step_enabled', False):
            return False

        # Check minimum notional
        min_notional = getattr(es, 'grid_step_min_notional_usd', Decimal("200.0"))
        if qty_usd < min_notional:
            logger.debug(
                f"Grid Step: Position too small ${qty_usd:.2f} < ${min_notional:.2f}"
            )
            return False

        # Check L1 depth
        l1_depth_usd = await _get_l1_depth_usd(self, trade.symbol, close_side)

        if l1_depth_usd <= 0:
            logger.debug(f"Grid Step: No L1 depth data for {trade.symbol}")
            return False

        l1_trigger = getattr(es, 'grid_step_l1_depth_trigger', Decimal("0.5"))
        l1_threshold = qty_usd * l1_trigger

        if l1_depth_usd >= l1_threshold:
            logger.debug(
                f"Grid Step: L1 depth sufficient ${l1_depth_usd:.2f} >= ${l1_threshold:.2f}"
            )
            return False

        logger.info(
            f"Grid Step: Enabled for {trade.symbol} "
            f"(pos=${qty_usd:.2f}, L1=${l1_depth_usd:.2f} < ${l1_threshold:.2f})"
        )
        return True

    except Exception as e:
        logger.warning(f"Grid Step: Error checking if should use grid: {e}")
        return False


async def _calculate_grid_step_config(
    self,
    trade: Trade,
    qty: Decimal,
    close_side: Side,
) -> _GridStepConfig:
    """
    Calculate grid step configuration for close.

    Returns:
        _GridStepConfig with calculated parameters
    """
    es = self.settings.execution

    # Get position notional
    price = _calculate_close_price(self, trade, close_side)
    qty_usd = qty * price

    # Calculate adaptive grid step based on volatility
    grid_step = Decimal("0.0005")  # Default 0.05%

    if getattr(es, 'grid_step_adaptive', True):
        volatility = await _get_recent_volatility(self, trade.symbol)

        vol_high = getattr(es, 'grid_step_volatility_threshold_high', Decimal("0.01"))
        vol_low = getattr(es, 'grid_step_volatility_threshold_low', Decimal("0.005"))

        step_min = getattr(es, 'grid_step_min_pct', Decimal("0.0002"))
        step_max = getattr(es, 'grid_step_max_pct', Decimal("0.001"))

        if volatility >= vol_high:
            # High volatility: use max step
            grid_step = step_max
            logger.debug(
                f"Grid Step: High volatility {volatility:.2%}, using max step {grid_step:.2%}"
            )
        elif volatility >= vol_low:
            # Medium volatility: use balanced step
            grid_step = (step_min + step_max) / Decimal("2")
            logger.debug(
                f"Grid Step: Medium volatility {volatility:.2%}, using balanced step {grid_step:.2%}"
            )
        else:
            # Low volatility: use min step
            grid_step = step_min
            logger.debug(
                f"Grid Step: Low volatility {volatility:.2%}, using min step {grid_step:.2%}"
            )
    else:
        # Fixed grid step
        grid_step = getattr(es, 'grid_step_max_pct', Decimal("0.001"))

    # Calculate number of levels
    max_levels = getattr(es, 'grid_step_max_levels', 5)
    l1_depth_usd = await _get_l1_depth_usd(self, trade.symbol, close_side)

    if l1_depth_usd > 0:
        # Calculate levels needed based on L1 depth
        levels_needed = min(
            max_levels,
            int((qty_usd / l1_depth_usd).quantize(Decimal("1")))
        )
        num_levels = max(2, min(levels_needed, max_levels))  # At least 2, at most max_levels
    else:
        num_levels = max_levels

    qty_per_level = qty / Decimal(str(num_levels))

    logger.info(
        f"Grid Step Config: {trade.symbol} step={grid_step:.2%}, "
        f"levels={num_levels}, qty_per_level=${(qty_per_level * price):.2f}"
    )

    return _GridStepConfig(
        enabled=True,
        grid_step=grid_step,
        num_levels=num_levels,
        qty_per_level=qty_per_level,
    )


async def _close_verify_and_finalize(self, trade: Trade, reason: str) -> CloseResult:
    """
    Verify positions are closed and finalize trade (idempotent close path).

    Called when trade is already in CLOSING state to prevent duplicate orders.
    This path skips placing new orders and instead:
    1. Verifies positions are actually closed
    2. Performs post-close readback (T3) to get accurate VWAP/fees from API
    3. Finalizes the trade with calculated realized_pnl

    Args:
        trade: Trade in CLOSING state
        reason: Close reason (may differ from original, use original if set)

    Returns:
        CloseResult with success status and realized_pnl
    """
    try:
        # 1. Verify positions are closed
        await self._verify_closed(trade)

        # 2. Post-close readback (T3) - get accurate VWAP/fees from exchange API
        leg1_corrected, leg2_corrected = await self._post_close_readback(trade)

        # 3. Calculate final PnL
        realized_pnl = self._calculate_realized_pnl(trade)

        # 4. Log structured final execution metrics
        leg1_vwap = getattr(trade.leg1, "exit_price", Decimal("0"))
        leg2_vwap = getattr(trade.leg2, "exit_price", Decimal("0"))
        leg1_fees = getattr(trade.leg1, "fees", Decimal("0"))
        leg2_fees = getattr(trade.leg2, "fees", Decimal("0"))

        _log_final_execution_metrics(
            trade=trade,
            leg1_vwap=leg1_vwap,
            leg2_vwap=leg2_vwap,
            leg1_fees=leg1_fees,
            leg2_fees=leg2_fees,
            leg1_readback_corrected=leg1_corrected,
            leg2_readback_corrected=leg2_corrected,
            total_pnl=trade.total_pnl,
            funding_collected=trade.funding_collected,
        )

        # 5. Update trade
        trade.mark_closed(reason, realized_pnl)
        await self._update_trade(trade)

        # 6. Publish event
        await self.event_bus.publish(
            TradeClosed(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                reason=reason,
                realized_pnl=realized_pnl,
                funding_collected=trade.funding_collected,
                total_fees=trade.total_fees,
                hold_duration_seconds=trade.hold_duration_seconds,
            )
        )

        net_pnl = trade.total_pnl
        logger.info(
            f"Trade {trade.trade_id[:8]} verified+finalized: NetPnL=${net_pnl:.2f} "
            f"(= realized_after_fees ${realized_pnl:.2f} + funding ${trade.funding_collected:.2f}; "
            f"fees_included=${trade.total_fees:.2f})"
        )

        return CloseResult(
            success=True,
            realized_pnl=realized_pnl,
            reason=reason,
        )

    except Exception as e:
        logger.exception(f"Error in verify+finalize for {trade.symbol}: {e}")
        # Don't alert here - we already have close failure alerts in _close_impl
        return CloseResult(
            success=False,
            reason=reason,
            error=str(e),
        )


def _collect_close_order_ids(trade: Trade) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """
    Collect close order IDs from trade.events by exchange and leg.

    Returns:
        Tuple of (lighter_order_ids, x10_order_ids) as dicts mapping leg -> order_ids
        Example: ({"leg1": ["order1", "order2"]}, {"leg2": ["order3"]})
    """
    lighter_orders: dict[str, list[str]] = {"leg1": [], "leg2": []}
    x10_orders: dict[str, list[str]] = {"leg1": [], "leg2": []}

    for event in trade.events:
        if event.get("event_type") == "CLOSE_ORDER_PLACED":
            exchange = event.get("exchange")
            leg = event.get("leg")
            order_id = event.get("order_id")

            if exchange == "lighter" and leg in lighter_orders:
                lighter_orders[leg].append(order_id)
            elif exchange == "x10" and leg in x10_orders:
                x10_orders[leg].append(order_id)

    return lighter_orders, x10_orders


async def _recompute_leg_from_orders(
    adapter: ExchangePort,
    symbol: str,
    order_ids: list[str],
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Fetch close orders from exchange API and recompute VWAP and total fees.

    Handles Lighter's cumulative filled_qty/fee by calculating deltas.
    Returns (total_qty, total_notional, total_fees).

    Args:
        adapter: Exchange adapter for fetching orders
        symbol: Trading symbol
        order_ids: List of close order IDs to fetch

    Returns:
        Tuple of (total_qty, total_notional, total_fees) from all orders
    """
    total_qty = Decimal("0")
    total_notional = Decimal("0")
    total_fees = Decimal("0")

    # Track cumulative fills to handle Lighter's cumulative filled_qty/fee
    qty_seen: dict[str, Decimal] = {}
    fee_seen: dict[str, Decimal] = {}

    for order_id in order_ids:
        try:
            order = await adapter.get_order(symbol, order_id)

            if not order:
                logger.warning(f"Recompute: order {order_id} not found for {symbol}")
                continue

            # Calculate delta from cumulative values
            if order.filled_qty and order.filled_qty > 0:
                d_qty, d_notional, d_fee = _delta_from_cumulative_fill(
                    order_id=order_id,
                    cum_qty=order.filled_qty,
                    cum_fee=order.fee,
                    fill_price=order.avg_fill_price if order.avg_fill_price > 0 else Decimal("0"),
                    qty_seen=qty_seen,
                    fee_seen=fee_seen,
                )

                total_qty += d_qty
                total_notional += d_notional
                total_fees += d_fee

                logger.debug(
                    f"Recompute: {adapter.exchange} order {order_id}: "
                    f"delta_qty={d_qty}, delta_notional={d_notional}, delta_fee={d_fee}"
                )

        except Exception as e:
            logger.warning(f"Recompute: failed to fetch order {order_id}: {e}")

    return total_qty, total_notional, total_fees


async def _post_close_readback(self, trade: Trade) -> tuple[bool, bool]:
    """
    Perform post-close readback to get accurate VWAP and fees from exchange API.

    This is the "source of truth" for exit prices and fees, correcting any
    discrepancies from:
    - Order state lag (get_order not immediately reflecting fills)
    - Partial fills across multiple orders
    - Fee calculation differences

    Process:
    1. Parse trade.events to collect all close order IDs per exchange/leg
    2. Fetch each order via adapter.get_order()
    3. Recalculate VWAP and total fees from API data
    4. Update trade.leg.exit_price and trade.leg.fees if discrepancy > tolerance

    Returns:
        Tuple of (leg1_corrected, leg2_corrected) - bools indicating if corrections were applied
    """
    # Tolerance: 3 bps (0.0003 = 0.03%) for VWAP, $0.30 for net_pnl
    tolerance_pct = Decimal("0.0003")  # 3 bps tolerance for VWAP discrepancies
    net_pnl_tolerance_usd = Decimal("0.30")  # $0.30 tolerance for net_pnl discrepancies

    # Collect close order IDs from events
    lighter_order_ids, x10_order_ids = _collect_close_order_ids(trade)

    # Process Lighter leg (leg1)
    leg1_corrected = False
    if lighter_order_ids["leg1"]:
        leg1_corrected = await self._readback_leg(
            trade=trade,
            adapter=self.lighter,
            leg=trade.leg1,
            order_ids=lighter_order_ids["leg1"],
            tolerance_pct=tolerance_pct,
            net_pnl_tolerance_usd=net_pnl_tolerance_usd,
        )

    # Process X10 leg (leg2)
    leg2_corrected = False
    if x10_order_ids["leg2"]:
        leg2_corrected = await self._readback_leg(
            trade=trade,
            adapter=self.x10,
            leg=trade.leg2,
            order_ids=x10_order_ids["leg2"],
            tolerance_pct=tolerance_pct,
            net_pnl_tolerance_usd=net_pnl_tolerance_usd,
        )

    return leg1_corrected, leg2_corrected


async def _readback_leg(
    self,
    trade: Trade,
    adapter: ExchangePort,
    leg: TradeLeg,
    order_ids: list[str],
    tolerance_pct: Decimal,
    net_pnl_tolerance_usd: Decimal,
) -> bool:
    """
    Readback a single leg's close orders to recalculate VWAP and fees.

    Fetches all close orders for a leg from the exchange API and recalculates
    the exit VWAP and total fees. Updates the leg if discrepancy exceeds tolerance:
    - VWAP diff > 3 bps (0.0003)
    - OR net_pnl diff > $0.30

    Returns:
        True if corrections were applied, False otherwise
    """
    if not order_ids:
        return False

    logger.info(f"Readback for {trade.symbol} {adapter.exchange}: {len(order_ids)} orders")

    # Fetch orders from exchange API and recompute VWAP/fees
    total_qty, total_notional, total_fees = await _recompute_leg_from_orders(
        adapter=adapter,
        symbol=trade.symbol,
        order_ids=order_ids,
    )

    # Calculate VWAP from API data
    api_vwap = total_notional / total_qty if total_qty > 0 else Decimal("0")

    # Check discrepancy with current values
    current_exit_price = getattr(leg, "exit_price", Decimal("0"))
    current_fees = getattr(leg, "fees", Decimal("0"))

    corrected = False
    if api_vwap > 0:
        price_diff_pct = abs(api_vwap - current_exit_price) / current_exit_price if current_exit_price > 0 else Decimal("1")
        fee_diff_usd = abs(total_fees - current_fees)

        # Calculate net_pnl impact: (price_diff * qty) + fee_diff
        leg_qty = getattr(leg, "filled_qty", Decimal("0"))
        price_impact_usd = abs(api_vwap - current_exit_price) * leg_qty
        net_pnl_diff_usd = price_impact_usd + fee_diff_usd

        # Update if discrepancy exceeds tolerance:
        # VWAP diff > 3 bps OR net_pnl diff > $0.30
        if price_diff_pct > tolerance_pct or net_pnl_diff_usd > net_pnl_tolerance_usd:
            logger.warning(
                f"Readback UPDATE {trade.symbol} {adapter.exchange}: "
                f"exit_price {current_exit_price} -> {api_vwap} ({price_diff_pct:.4%}), "
                f"fees {current_fees} -> {total_fees}, "
                f"net_pnl_impact=${net_pnl_diff_usd:.2f} "
                f"(price_impact=${price_impact_usd:.2f}, fee_diff=${fee_diff_usd:.2f})"
            )
            leg.exit_price = api_vwap
            leg.fees = total_fees
            corrected = True
        else:
            logger.debug(
                f"Readback OK {trade.symbol} {adapter.exchange}: "
                f"exit_price={api_vwap}, fees={total_fees} "
                f"(price_diff={price_diff_pct:.4%}, net_pnl_impact=${net_pnl_diff_usd:.2f} within tolerance)"
            )

    return corrected


async def _close_impl(self, trade: Trade, reason: str) -> CloseResult:
    """Internal close implementation with Smart Exit logic."""
    # Trigger Cooldown (prevent immediate re-entry churn)
    if self.opportunity_engine:
        self.opportunity_engine.mark_cooldown(trade.symbol)

    logger.info(f"Closing trade {trade.trade_id[:8]} ({trade.symbol}): {reason}")

    # === IDEMPOTENCY CHECK (T2) ===
    # If trade is already CLOSING with a reason set, skip placing new orders.
    # Instead, verify positions are closed and finalize with post-close readback.
    if trade.status == TradeStatus.CLOSING and trade.close_reason:
        logger.info(
            f"Trade {trade.trade_id[:8]} already CLOSING (reason={trade.close_reason}) - "
            f"verify+finalize mode (idempotent)"
        )
        return await self._close_verify_and_finalize(trade, reason)

    # === T1: REBALANCE PATH (Reduce-Only, NO Full Close) ===
    # IMPORTANT: Check REBALANCE BEFORE setting status to CLOSING!
    # Rebalance is a reduce-only operation that keeps the trade OPEN.
    # We do NOT persist close_reason or change status for rebalance.
    # STRICT: Only trigger on "REBALANCE:" prefix to avoid false positives
    is_rebalance = (reason or "").startswith("REBALANCE:")
    if is_rebalance:
        logger.info(f"REBALANCE path selected for {trade.symbol}: {reason}")
        # DO NOT set status to CLOSING - trade stays OPEN
        await self._rebalance_trade(trade)
        # Trade remains OPEN with restored delta neutrality
        return CloseResult(
            success=True,
            realized_pnl=Decimal("0"),  # No PnL realization for rebalance
            reason="REBALANCED",
        )

    # For all non-rebalance exits: persist intent and mark as CLOSING
    trade.close_reason = reason
    trade.status = TradeStatus.CLOSING
    await self._update_trade(trade)

    try:
        # === T2: COORDINATED CLOSE PATH (Parallel Dual-Leg Close) ===
        # For ECON/OPT exits: Use coordinated close to minimize unhedged window.
        # EMERGENCY, CRASH, and Early-TP fast-close bypass coordinated close for speed.
        coordinated_enabled = bool(getattr(self.settings.trading, "coordinated_close_enabled", True))
        is_emergency_or_fast = (reason or "").startswith((
            "EMERGENCY",
            "EARLY_TAKE_PROFIT",
            "VELOCITY",  # Funding Velocity Exit (APY crash proactive)
            "Z-SCORE",  # APY crash exits should be fast
        )) or ("APY crashed" in (reason or ""))  # Also check substring for APY crash without prefix
        use_coordinated = coordinated_enabled and not is_emergency_or_fast

        # Smart Exit Strategy (default):
        # - Close Lighter as maker (save fees), then close X10.
        #
        # Early-TP Exception:
        # When we intentionally capture a transient cross-exchange price dislocation,
        # we must close BOTH legs immediately to avoid legging risk.
        is_early_tp = (reason or "").startswith("EARLY_TAKE_PROFIT") or (
            (trade.close_reason or "").startswith("EARLY_TAKE_PROFIT")
        )
        early_tp_fast_enabled = bool(
            getattr(self.settings.trading, "early_tp_fast_close_enabled", True)
        )
        early_tp_taker_direct = bool(
            getattr(self.settings.trading, "early_tp_fast_close_use_taker_directly", False)
        )

        if is_early_tp and early_tp_fast_enabled and early_tp_taker_direct:
            await asyncio.gather(
                self._close_leg(self.lighter, trade, trade.leg1),
                self._close_x10_smart(trade),
                return_exceptions=False,
            )
            await self._update_trade(trade)
        elif use_coordinated:
            # === T2: COORDINATED CLOSE (for ECON/OPT exits) ===
            # Parallel maker orders on both legs, minimizing unhedged window
            await self._close_both_legs_coordinated(trade)
        else:
            # 1. Close Lighter
            await self._close_lighter_smart(trade)
            # Persist partial close progress (exit price/fees) even if later steps fail.
            await self._update_trade(trade)

            # 2. Close X10 (Hedge)
            logger.info(f"Closing X10 for {trade.symbol}")
            await self._close_x10_smart(trade)
            await self._update_trade(trade)

        # Verify positions are closed
        await self._verify_closed(trade)

        # Post-close readback (T3/T4) - get accurate VWAP/fees from exchange API
        # Must be done BEFORE calculating realized_pnl so API truth wins
        leg1_corrected, leg2_corrected = await self._post_close_readback(trade)

        # Calculate final PnL (T4: AFTER readback, using API-corrected exit prices/fees)
        realized_pnl = self._calculate_realized_pnl(trade)

        # Log structured final execution metrics
        leg1_vwap = getattr(trade.leg1, "exit_price", Decimal("0"))
        leg2_vwap = getattr(trade.leg2, "exit_price", Decimal("0"))
        leg1_fees = getattr(trade.leg1, "fees", Decimal("0"))
        leg2_fees = getattr(trade.leg2, "fees", Decimal("0"))

        _log_final_execution_metrics(
            trade=trade,
            leg1_vwap=leg1_vwap,
            leg2_vwap=leg2_vwap,
            leg1_fees=leg1_fees,
            leg2_fees=leg2_fees,
            leg1_readback_corrected=leg1_corrected,
            leg2_readback_corrected=leg2_corrected,
            total_pnl=trade.total_pnl,
            funding_collected=trade.funding_collected,
        )

        # Update trade
        trade.mark_closed(reason, realized_pnl)
        await self._update_trade(trade)

        # Publish event
        await self.event_bus.publish(
            TradeClosed(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                reason=reason,
                realized_pnl=realized_pnl,
                funding_collected=trade.funding_collected,
                total_fees=trade.total_fees,
                hold_duration_seconds=trade.hold_duration_seconds,
            )
        )

        net_pnl = trade.total_pnl
        logger.info(
            f"Trade {trade.trade_id[:8]} closed: NetPnL=${net_pnl:.2f} "
            f"(= realized_after_fees ${realized_pnl:.2f} + funding ${trade.funding_collected:.2f}; "
            f"fees_included=${trade.total_fees:.2f})"
        )

        return CloseResult(
            success=True,
            realized_pnl=realized_pnl,
            reason=reason,
        )

    except Exception as e:
        logger.exception(f"Error closing trade {trade.symbol}: {e}")
        # Ensure we don't lose partial exit prices/fees on failure.
        with contextlib.suppress(Exception):
            await self._update_trade(trade)

        # Alert (throttled) so we can intervene if a trade is stuck CLOSING / positions remain open.
        now = datetime.now(UTC)
        last = self._close_failure_alert_last.get(trade.symbol)
        min_interval_seconds = 300.0  # 5 minutes
        should_send = (last is None) or ((now - last).total_seconds() >= min_interval_seconds)
        if isinstance(e, PositionsStillOpenError):
            # More urgent; allow a shorter interval.
            min_interval_seconds = 60.0
            should_send = (last is None) or ((now - last).total_seconds() >= min_interval_seconds)

        if should_send:
            self._close_failure_alert_last[trade.symbol] = now
            level = "CRITICAL" if isinstance(e, PositionsStillOpenError) else "ERROR"
            details = {
                "trade_id": trade.trade_id,
                "close_reason": reason,
                "status": trade.status.value,
                "leg1_exchange": trade.leg1.exchange.value,
                "leg1_side": trade.leg1.side.value,
                "leg1_qty": str(trade.leg1.qty),
                "leg1_filled_qty": str(trade.leg1.filled_qty),
                "leg1_order_id": trade.leg1.order_id,
                "leg2_exchange": trade.leg2.exchange.value,
                "leg2_side": trade.leg2.side.value,
                "leg2_qty": str(trade.leg2.qty),
                "leg2_filled_qty": str(trade.leg2.filled_qty),
                "leg2_order_id": trade.leg2.order_id,
                "error": str(e),
            }
            with contextlib.suppress(Exception):
                await self.event_bus.publish(
                    AlertEvent(
                        level=level,
                        message=f"Close failed: {trade.symbol}",
                        details=details,
                    )
                )

        return CloseResult(
            success=False,
            reason=reason,
            error=str(e),
        )


async def _close_lighter_smart(self, trade: Trade) -> None:
    """Close Lighter leg with Maker Chasing.

    Simplified version with extracted helper functions for better testability
    and maintainability. Handles both fast-close (Early-TP) and standard
    maker-chase strategies.

    Fast-close (Early-TP/APY-Crash):
    - Fewer attempts (default 2)
    - Shorter per-attempt timeout (default 5s)
    - Total time limit (default 30s) before forcing taker
    - Optional: Skip maker entirely with use_taker_directly

    Standard close:
    - Up to 5 maker attempts
    - 10s per-attempt timeout
    - No total time limit
    """
    if trade.leg1.filled_qty == 0:
        return

    # Step 1: Fetch and validate position state
    pos_fetch_ok = True
    try:
        pos = await self.lighter.get_position(trade.symbol)
    except Exception as e:
        logger.warning(f"Failed to fetch Lighter position for close ({trade.symbol}): {e}")
        pos_fetch_ok = False
        pos = None

    # Step 2: Check if we should skip close (safety checks)
    should_skip, skip_reason = _should_skip_lighter_close(self, trade, pos, pos_fetch_ok)
    if should_skip:
        if skip_reason:
            logger.info(skip_reason)
        return

    # Step 3: Get close configuration
    config = _get_lighter_close_config(self, trade)
    qty_threshold = Decimal("0.0001")

    # Step 4: Handle direct taker close (Early-TP fast path)
    if config.use_taker_directly:
        logger.info(f"Early-TP Fast Close: Going directly to Taker for {trade.symbol}")
        close_start_time = time.monotonic()
        await self._close_leg(self.lighter, trade, trade.leg1)
        trade.close_attempts = 0
        trade.close_duration_seconds = Decimal(str(time.monotonic() - close_start_time))
        return

    # Step 5: Ensure WebSocket for instant fill detection
    await _ensure_lighter_websocket(self, trade)

    # Step 6: Execute maker close with retries
    close_start_time = time.monotonic()
    close_side = trade.leg1.side.inverse()
    qty = (
        pos.qty
        if (pos_fetch_ok and pos and pos.qty > qty_threshold)
        else trade.leg1.filled_qty
    )

    result = await self._execute_lighter_maker_close(
        trade=trade,
        config=config,
        close_side=close_side,
        qty=qty,
        qty_threshold=qty_threshold,
        close_start_time=close_start_time,
    )

    # Step 7: Finalize trade state
    if result.closed_qty_total > 0:
        trade.leg1.exit_price = result.closed_notional_total / result.closed_qty_total
        trade.leg1.fees += result.closed_fee_total
    trade.close_attempts = result.attempts_made
    trade.close_duration_seconds = Decimal(str(time.monotonic() - close_start_time))


class _LighterCloseResult:
    """Result of Lighter close operation."""

    def __init__(self):
        self.closed_qty_total = Decimal("0")
        self.closed_notional_total = Decimal("0")
        self.closed_fee_total = Decimal("0")
        self.attempts_made = 0


async def _execute_lighter_maker_close(
    self,
    trade: Trade,
    config: _LighterCloseConfig,
    close_side: Side,
    qty: Decimal,
    qty_threshold: Decimal,
    close_start_time: float,
) -> _LighterCloseResult:
    """
    Execute maker-based close with retry loop.

    This is the core close logic that attempts to close the position
    using maker orders with multiple retries before falling back to taker.

    Grid Step: If enabled and conditions are met, distributes orders across
    multiple price levels to reduce order pollution and increase fill rate.
    """
    result = _LighterCloseResult()
    remaining_qty = qty

    # Check if Grid Step should be used (only on first attempt with full qty)
    use_grid_step = False
    grid_config = None

    try:
        price = _calculate_close_price(self, trade, close_side)
        qty_usd = remaining_qty * price

        # Check Grid Step eligibility (only first attempt)
        if result.attempts_made == 0:
            use_grid_step = await _should_use_grid_step_async(
                self, trade, close_side, qty_usd
            )

            if use_grid_step:
                grid_config = await _calculate_grid_step_config(
                    self, trade, remaining_qty, close_side
                )
                logger.info(
                    f"Grid Step Activated: {trade.symbol} "
                    f"step={grid_config.grid_step:.2%}, "
                    f"levels={grid_config.num_levels}"
                )
    except Exception as e:
        logger.debug(f"Grid Step check failed: {e}")
        use_grid_step = False

    for attempt in range(config.max_attempts):
        if remaining_qty <= qty_threshold:
            return result

        # Check total timeout (fast-close only)
        elapsed = time.monotonic() - close_start_time
        if elapsed >= config.total_timeout:
            logger.warning(
                f"Early-TP Fast Close: Total timeout {config.total_timeout}s reached for {trade.symbol} "
                f"after {result.attempts_made} attempts. Escalating to Taker."
            )
            break

        result.attempts_made += 1

        # Grid Step: Distribute orders across price levels (first attempt only)
        if use_grid_step and grid_config and attempt == 0:
            logger.info(f"Closing Lighter with Grid Step (Attempt {attempt + 1}/{config.max_attempts})")

            grid_result = await self._execute_lighter_close_grid_step(
                trade=trade,
                close_side=close_side,
                remaining_qty=remaining_qty,
                grid_config=grid_config,
                config=config,
                qty_threshold=qty_threshold,
                close_start_time=close_start_time,
            )

            # Update totals
            result.closed_qty_total += grid_result.closed_qty
            result.closed_notional_total += grid_result.closed_notional
            result.closed_fee_total += grid_result.closed_fee
            remaining_qty = grid_result.remaining_qty

            if remaining_qty <= qty_threshold:
                return result

            if grid_result.position_closed:
                logger.info(f"Grid Step: Position confirmed closed for {trade.symbol}")
                return result

            # Grid step completed, continue with standard retry logic for remaining
            use_grid_step = False  # Disable grid for subsequent attempts
            continue

        # Standard close (single order)
        logger.info(f"Closing Lighter (Attempt {attempt + 1}/{config.max_attempts})")

        # Get price
        try:
            price = _calculate_close_price(self, trade, close_side)
        except Exception as e:
            logger.warning(f"Failed to get price for {trade.symbol}: {e}")
            await asyncio.sleep(1.0)
            continue

        # Place and monitor order
        attempt_result = await self._execute_lighter_close_attempt(
            trade=trade,
            close_side=close_side,
            remaining_qty=remaining_qty,
            price=price,
            config=config,
            qty_threshold=qty_threshold,
            close_start_time=close_start_time,
        )

        # Update totals
        result.closed_qty_total += attempt_result.closed_qty
        result.closed_notional_total += attempt_result.closed_notional
        result.closed_fee_total += attempt_result.closed_fee
        remaining_qty = attempt_result.remaining_qty

        if remaining_qty <= qty_threshold:
            return result

        # Check for position state change (fill detection via position)
        if attempt_result.position_closed:
            logger.info(f"Position confirmed closed via position check for {trade.symbol}")
            if result.closed_qty_total == 0:
                # Assume full close if we haven't tracked any partials
                result.closed_qty_total = qty
                result.closed_notional_total = qty * price
            return result

    # If we reach here, Maker failed. Force Taker Close.
    if config.use_fast_close:
        logger.warning(
            f"Early-TP Fast Close: Maker failed after {result.attempts_made} attempts, "
            f"forcing Taker Exit"
        )
    else:
        logger.warning("Lighter Smart Close failed, forcing Market Exit")

    taker_result = await self._execute_lighter_taker_fallback(
        trade=trade,
        remaining_qty=remaining_qty,
    )
    result.closed_qty_total += taker_result.closed_qty
    result.closed_notional_total += taker_result.closed_notional
    result.closed_fee_total += taker_result.closed_fee

    return result


class _LighterCloseAttemptResult:
    """Result of a single close attempt."""

    def __init__(self):
        self.closed_qty = Decimal("0")
        self.closed_notional = Decimal("0")
        self.closed_fee = Decimal("0")
        self.remaining_qty: Decimal = Decimal("0")
        self.position_closed = False


async def _execute_lighter_close_attempt(
    self,
    trade: Trade,
    close_side: Side,
    remaining_qty: Decimal,
    price: Decimal,
    config: _LighterCloseConfig,
    qty_threshold: Decimal,
    close_start_time: float,
) -> _LighterCloseAttemptResult:
    """Execute a single close attempt with order placement and fill monitoring."""
    result = _LighterCloseAttemptResult()
    result.remaining_qty = remaining_qty

    try:
        # Place Order
        request = OrderRequest(
            symbol=trade.symbol,
            exchange=Exchange.LIGHTER,
            side=close_side,
            qty=remaining_qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,
            reduce_only=True,
        )

        order = await self.lighter.place_order(request)

        # T1: Log close order for post-close readback (T3)
        _log_close_order_placed(
            trade=trade,
            exchange=Exchange.LIGHTER,
            leg="leg1",
            order_id=str(order.order_id),
            client_order_id=getattr(order, "client_order_id", None),
            side=close_side,
            qty=remaining_qty,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,
            post_only=True,
        )

        active_order_id = order.order_id
        last_seen: Order | None = None
        order_filled_qty = Decimal("0")
        order_fee_seen = Decimal("0")

        # Wait for fill (use per-attempt timeout)
        for i in range(int(config.attempt_timeout * 2)):
            # Also check total timeout during wait
            if (time.monotonic() - close_start_time) >= config.total_timeout:
                break

            try:
                filled = await asyncio.wait_for(
                    self.lighter.get_order(trade.symbol, active_order_id),
                    timeout=_close_get_order_timeout_seconds(self),
                )
            except TimeoutError:
                filled = None

            last_seen = filled
            if (
                filled
                and filled.order_id
                and not str(filled.order_id).startswith("pending_")
            ):
                active_order_id = str(filled.order_id)

            if filled and filled.filled_qty > order_filled_qty:
                delta_qty = filled.filled_qty - order_filled_qty
                order_filled_qty = filled.filled_qty
                fill_price = filled.avg_fill_price if filled.avg_fill_price > 0 else price
                result.closed_qty += delta_qty
                result.closed_notional += delta_qty * fill_price
                if filled.fee >= order_fee_seen:
                    result.closed_fee += filled.fee - order_fee_seen
                    order_fee_seen = filled.fee
                result.remaining_qty = max(Decimal("0"), remaining_qty - delta_qty)

                if result.remaining_qty <= qty_threshold:
                    if filled.is_active:
                        with contextlib.suppress(Exception):
                            await self.lighter.cancel_order(trade.symbol, active_order_id)
                    return result

            if filled and filled.is_filled:
                return result
            if filled and filled.status.is_terminal():
                break  # Cancelled/Rejected

            # Occasionally check actual position state during the loop
            if i % 4 == 0:  # Every 2 seconds
                try:
                    pos_check = await self.lighter.get_position(trade.symbol)
                    if not pos_check or pos_check.qty <= qty_threshold:
                        result.position_closed = True
                        return result
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        # Timeout -> Cancel (only if still active)
        if last_seen is None or last_seen.is_active:
            await self.lighter.cancel_order(trade.symbol, active_order_id)

        # Check if position is actually closed (more reliable than order status)
        try:
            pos_check = await self.lighter.get_position(trade.symbol)
            if not pos_check or pos_check.qty <= qty_threshold:
                logger.info(
                    f"Lighter position confirmed closed for {trade.symbol} "
                    f"(order fill may not have been detected via get_order timeout)"
                )
                result.position_closed = True
                if result.closed_qty == 0:
                    result.closed_qty = remaining_qty
                    result.closed_notional = remaining_qty * price
                result.remaining_qty = Decimal("0")
                return result
            # Update remaining_qty from exchange (authoritative)
            if pos_check.qty != result.remaining_qty:
                logger.info(
                    f"Lighter position qty changed: {result.remaining_qty} -> {pos_check.qty} "
                    f"(detected fill that get_order missed)"
                )
                delta_filled = result.remaining_qty - pos_check.qty
                if delta_filled > 0:
                    result.closed_qty += delta_filled
                    result.closed_notional += delta_filled * price
                result.remaining_qty = pos_check.qty
        except Exception as e_pos:
            logger.warning(f"Failed to check Lighter position after close attempt: {e_pos}")

    except Exception as e:
        logger.warning(f"Lighter close attempt failed: {e}")
        await asyncio.sleep(1.0)

    return result


class _GridStepCloseResult:
    """Result of grid step close operation."""

    def __init__(self):
        self.closed_qty = Decimal("0")
        self.closed_notional = Decimal("0")
        self.closed_fee = Decimal("0")
        self.remaining_qty: Decimal = Decimal("0")
        self.position_closed = False


# =============================================================================
# GRID STEP HELPER FUNCTIONS
# =============================================================================


def _calculate_grid_price_levels(
    base_price: Decimal,
    close_side: Side,
    grid_config: "_GridStepConfig",
    tick: Decimal,
) -> list[Decimal]:
    """
    Calculate price levels for grid step close.

    Args:
        base_price: Starting price for the grid
        close_side: BUY (close short) or SELL (close long)
        grid_config: Grid configuration with step size and num_levels
        tick: Tick size for price rounding

    Returns:
        List of prices at each grid level
    """
    price_levels = []

    for i in range(grid_config.num_levels):
        if close_side == Side.SELL:
            # Closing Long: Prices below base (more aggressive)
            price_multiplier = Decimal("1") - (grid_config.grid_step * Decimal(str(i)))
            rounding = "floor"
        else:
            # Closing Short: Prices above base (more aggressive)
            price_multiplier = Decimal("1") + (grid_config.grid_step * Decimal(str(i)))
            rounding = "ceil"

        level_price = _round_price_to_tick(
            base_price * price_multiplier,
            tick,
            rounding=rounding
        )
        price_levels.append(level_price)

    return price_levels


async def _place_grid_orders(
    self,
    trade: Trade,
    close_side: Side,
    price_levels: list[Decimal],
    grid_config: "_GridStepConfig",
    qty_threshold: Decimal,
    remaining_qty: Decimal,
) -> tuple[dict[str, tuple[Decimal, Decimal]], list[str]]:
    """
    Place orders at each grid price level.

    Returns:
        Tuple of (active_orders dict, order_ids_to_cancel list)
    """
    active_orders: dict[str, tuple[Decimal, Decimal]] = {}
    order_ids_to_cancel: list[str] = []
    current_remaining = remaining_qty

    for i, level_price in enumerate(price_levels):
        level_qty = grid_config.qty_per_level

        # Last order gets remaining qty (to account for rounding)
        if i == grid_config.num_levels - 1:
            level_qty = current_remaining

        if level_qty <= qty_threshold:
            break

        try:
            request = OrderRequest(
                symbol=trade.symbol,
                exchange=Exchange.LIGHTER,
                side=close_side,
                qty=level_qty,
                order_type=OrderType.LIMIT,
                price=level_price,
                time_in_force=TimeInForce.POST_ONLY,
                reduce_only=True,
            )

            order = await self.lighter.place_order(request)

            # Log grid step close order for post-close readback
            _log_close_order_placed(
                trade=trade,
                exchange=Exchange.LIGHTER,
                leg="leg1",
                order_id=str(order.order_id),
                client_order_id=getattr(order, "client_order_id", None),
                side=close_side,
                qty=level_qty,
                price=level_price,
                time_in_force=TimeInForce.POST_ONLY,
                post_only=True,
                attempt_id=i + 1,
            )

            active_orders[str(order.order_id)] = (level_price, level_qty)
            order_ids_to_cancel.append(str(order.order_id))
            current_remaining -= level_qty

            logger.debug(
                f"Grid Step: Placed level {i+1}/{grid_config.num_levels} "
                f"@ {level_price:.4f} qty={level_qty}"
            )

        except Exception as e:
            logger.warning(f"Grid Step: Failed to place order level {i+1}: {e}")

    return active_orders, order_ids_to_cancel


def _process_order_fill_delta(
    order_id: str,
    filled: "Order",
    level_price: Decimal,
    order_qty_seen: dict[str, Decimal],
    order_fee_seen: dict[str, Decimal],
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Process order fill with delta accounting for cumulative values.

    Returns:
        (delta_qty, delta_notional, delta_fee)
    """
    fill_price = filled.avg_fill_price if filled.avg_fill_price > 0 else level_price

    return _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=filled.filled_qty,
        cum_fee=filled.fee,
        fill_price=fill_price,
        qty_seen=order_qty_seen,
        fee_seen=order_fee_seen,
    )


async def _check_grid_order_status(
    self,
    trade: Trade,
    active_orders: dict[str, tuple[Decimal, Decimal]],
    order_qty_seen: dict[str, Decimal],
    order_fee_seen: dict[str, Decimal],
) -> tuple[Decimal, Decimal, Decimal, list[str], bool]:
    """
    Check status of all active grid orders.

    Returns:
        (total_filled, total_notional, total_fee, still_active_ids, all_terminal)
    """
    total_filled = Decimal("0")
    total_notional = Decimal("0")
    total_fee = Decimal("0")
    still_active: list[str] = []
    all_terminal = True
    orders_to_remove: list[str] = []

    for order_id in list(active_orders.keys()):
        try:
            filled = await asyncio.wait_for(
                self.lighter.get_order(trade.symbol, order_id),
                timeout=_close_get_order_timeout_seconds(self),
            )

            level_price, _ = active_orders[order_id]

            if filled.is_filled:
                # Order fully filled
                d_qty, d_notional, d_fee = _process_order_fill_delta(
                    order_id, filled, level_price, order_qty_seen, order_fee_seen
                )
                total_filled += d_qty
                total_notional += d_notional
                total_fee += d_fee

                logger.debug(f"Grid Step: Level @ {level_price:.4f} filled {filled.filled_qty}")

                # Cleanup tracking
                order_qty_seen.pop(order_id, None)
                order_fee_seen.pop(order_id, None)
                orders_to_remove.append(order_id)

            elif filled.is_active:
                # Order still active
                still_active.append(order_id)
                all_terminal = False

                # Check for partial fills
                if filled.filled_qty > 0:
                    d_qty, d_notional, d_fee = _process_order_fill_delta(
                        order_id, filled, level_price, order_qty_seen, order_fee_seen
                    )
                    total_filled += d_qty
                    total_notional += d_notional
                    total_fee += d_fee

            else:
                # Order cancelled/rejected/terminal
                if filled.filled_qty > 0:
                    d_qty, d_notional, d_fee = _process_order_fill_delta(
                        order_id, filled, level_price, order_qty_seen, order_fee_seen
                    )
                    total_filled += d_qty
                    total_notional += d_notional
                    total_fee += d_fee

                # Cleanup tracking
                order_qty_seen.pop(order_id, None)
                order_fee_seen.pop(order_id, None)
                orders_to_remove.append(order_id)

        except TimeoutError:
            all_terminal = False
            still_active.append(order_id)
        except Exception as e:
            logger.debug(f"Grid Step: Error checking order {order_id}: {e}")
            order_qty_seen.pop(order_id, None)
            order_fee_seen.pop(order_id, None)
            orders_to_remove.append(order_id)

    # Remove processed orders
    for order_id in orders_to_remove:
        active_orders.pop(order_id, None)

    return total_filled, total_notional, total_fee, still_active, all_terminal


async def _cancel_remaining_grid_orders(
    self,
    trade: Trade,
    order_ids_to_cancel: list[str],
    active_orders: dict[str, tuple[Decimal, Decimal]],
) -> None:
    """Cancel any remaining unfilled grid orders."""
    for order_id in order_ids_to_cancel:
        if order_id in active_orders:
            try:
                await self.lighter.cancel_order(trade.symbol, order_id)
                logger.debug(f"Grid Step: Cancelled unfilled order {order_id}")
            except Exception as e:
                logger.debug(f"Grid Step: Failed to cancel order {order_id}: {e}")


async def _execute_lighter_close_grid_step(
    self,
    trade: Trade,
    close_side: Side,
    remaining_qty: Decimal,
    grid_config: _GridStepConfig,
    config: _LighterCloseConfig,
    qty_threshold: Decimal,
    close_start_time: float,
) -> _GridStepCloseResult:
    """
    Execute grid step close by distributing orders across price levels.

    Orchestrates: base price → price levels → order placement → fill monitoring → cleanup.

    Args:
        trade: Trade to close
        close_side: Side of close (BUY/SELL)
        remaining_qty: Total quantity to close
        grid_config: Grid step configuration
        config: Lighter close config
        qty_threshold: Minimum quantity threshold
        close_start_time: Start time of close operation

    Returns:
        _GridStepCloseResult with totals and remaining qty
    """
    result = _GridStepCloseResult()
    result.remaining_qty = remaining_qty

    # 1. Calculate base price
    try:
        base_price = _calculate_close_price(self, trade, close_side)
    except Exception as e:
        logger.warning(f"Grid Step: Failed to get base price: {e}")
        return result

    # 2. Calculate price levels
    lighter_info = self.market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
    tick = lighter_info.tick_size if lighter_info else Decimal("0.01")
    price_levels = _calculate_grid_price_levels(base_price, close_side, grid_config, tick)

    logger.info(
        f"Grid Step: Placing {grid_config.num_levels} orders for {trade.symbol}: "
        f"prices={[f'{p:.4f}' for p in price_levels]}"
    )

    # 3. Place orders at each level
    active_orders, order_ids_to_cancel = await _place_grid_orders(
        self, trade, close_side, price_levels, grid_config, qty_threshold, remaining_qty
    )

    if not active_orders:
        logger.warning("Grid Step: No orders placed successfully")
        return result

    # 4. Monitor fills with timeout
    wait_time = min(config.attempt_timeout, 10.0)
    start_wait = time.monotonic()
    check_interval = 0.5

    order_qty_seen: dict[str, Decimal] = {}
    order_fee_seen: dict[str, Decimal] = {}

    while (time.monotonic() - start_wait) < wait_time:
        # Check all active orders
        total_filled, total_notional, total_fee, still_active, all_terminal = await _check_grid_order_status(
            self, trade, active_orders, order_qty_seen, order_fee_seen
        )

        # Update totals
        if total_filled > 0:
            result.closed_qty += total_filled
            result.closed_notional += total_notional
            result.closed_fee += total_fee
            result.remaining_qty = max(qty_threshold, remaining_qty - result.closed_qty)

        # Check if all filled
        if result.remaining_qty <= qty_threshold:
            logger.info(f"Grid Step: All levels filled for {trade.symbol}")
            result.position_closed = True
            break

        # Check position if some filled
        if result.closed_qty > 0:
            try:
                pos_check = await self.lighter.get_position(trade.symbol)
                if not pos_check or pos_check.qty <= qty_threshold:
                    logger.info(f"Grid Step: Position confirmed closed for {trade.symbol}")
                    result.position_closed = True
                    if result.closed_qty < remaining_qty:
                        result.closed_qty = remaining_qty
                        result.closed_notional = remaining_qty * base_price
                    result.remaining_qty = Decimal("0")
                    break
            except Exception:
                pass

        if all_terminal or not still_active:
            break

        await asyncio.sleep(check_interval)

    # 5. Cancel remaining orders
    await _cancel_remaining_grid_orders(self, trade, order_ids_to_cancel, active_orders)

    logger.info(
        f"Grid Step Result: {trade.symbol} "
        f"filled={result.closed_qty} (${result.closed_notional:.2f}) "
        f"remaining={result.remaining_qty} fees=${result.closed_fee:.4f}"
    )

    return result


class _LighterTakerResult:
    """Result of taker fallback close."""

    def __init__(self):
        self.closed_qty = Decimal("0")
        self.closed_notional = Decimal("0")
        self.closed_fee = Decimal("0")


async def _execute_lighter_taker_fallback(
    self,
    trade: Trade,
    remaining_qty: Decimal,
) -> _LighterTakerResult:
    """Execute taker (market) fallback when maker close fails."""
    result = _LighterTakerResult()

    filled = await self._close_leg(
        self.lighter,
        trade,
        trade.leg1,
        override_qty=remaining_qty,
        update_leg=False,
    )

    if filled and filled.filled_qty > 0:
        fill_price = filled.avg_fill_price if filled.avg_fill_price > 0 else trade.leg1.exit_price
        result.closed_qty = filled.filled_qty
        if fill_price and fill_price > 0:
            result.closed_notional = filled.filled_qty * fill_price
        result.closed_fee = filled.fee

    return result


# =============================================================================
# REBALANCE HELPER FUNCTIONS
# =============================================================================


def _calculate_rebalance_target(
    trade: Trade,
    leg1_mark_price: Decimal | None,
    leg2_mark_price: Decimal | None,
) -> tuple[Exchange, TradeLeg, Decimal, Decimal]:
    """
    Calculate which leg to rebalance and by how much.

    Determines the net delta of the position and identifies which leg
    needs to be reduced to restore delta neutrality.

    Args:
        trade: Trade with leg1 (Lighter) and leg2 (X10).
        leg1_mark_price: Current mark price for leg1, or None to use entry.
        leg2_mark_price: Current mark price for leg2, or None to use entry.

    Returns:
        Tuple of (exchange, leg, rebalance_notional, net_delta):
        - exchange: Which exchange to trade on
        - leg: The TradeLeg to reduce
        - rebalance_notional: USD value to reduce
        - net_delta: Current net delta (positive = net long)
    """

    leg1_px = leg1_mark_price if leg1_mark_price and leg1_mark_price > 0 else trade.leg1.entry_price
    leg2_px = leg2_mark_price if leg2_mark_price and leg2_mark_price > 0 else trade.leg2.entry_price

    leg1_notional = trade.leg1.filled_qty * leg1_px
    leg2_notional = trade.leg2.filled_qty * leg2_px

    leg1_delta = leg1_notional if trade.leg1.side == Side.BUY else -leg1_notional
    leg2_delta = leg2_notional if trade.leg2.side == Side.BUY else -leg2_notional

    net_delta = leg1_delta + leg2_delta

    if net_delta > 0:
        if leg1_delta > 0 and leg2_delta < 0:
            return Exchange.LIGHTER, trade.leg1, abs(net_delta), net_delta
        else:
            return Exchange.X10, trade.leg2, abs(net_delta), net_delta
    else:
        if leg1_delta < 0 and leg2_delta > 0:
            return Exchange.LIGHTER, trade.leg1, abs(net_delta), net_delta
        else:
            return Exchange.X10, trade.leg2, abs(net_delta), net_delta


def _get_rebalance_price(
    self,
    trade: Trade,
    rebalance_exchange: Exchange,
    rebalance_side: Side,
) -> Decimal | None:
    """Get the appropriate price for a rebalance order."""
    price_data = self.market_data.get_price(trade.symbol)
    ob = self.market_data.get_orderbook(trade.symbol)

    if rebalance_exchange == Exchange.LIGHTER:
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            return ob.lighter_bid if rebalance_side == Side.BUY else ob.lighter_ask
        elif price_data and price_data.lighter_price > 0:
            return price_data.lighter_price
    else:
        if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
            return ob.x10_bid if rebalance_side == Side.BUY else ob.x10_ask
        elif price_data and price_data.x10_price > 0:
            return price_data.x10_price

    return None


async def _execute_rebalance_maker(
    self,
    trade: Trade,
    adapter: "ExchangePort",
    rebalance_exchange: Exchange,
    rebalance_side: Side,
    rebalance_qty: Decimal,
    order_price: Decimal,
    maker_timeout: float,
) -> tuple["Order | None", Decimal]:
    """
    Execute maker order for rebalance with timeout.

    Returns:
        (final_order_state, remaining_qty)
    """
    maker_request = OrderRequest(
        symbol=trade.symbol,
        exchange=rebalance_exchange,
        side=rebalance_side,
        qty=rebalance_qty,
        price=order_price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=True,
    )

    maker_order = await adapter.place_order(maker_request)

    if not maker_order:
        return None, rebalance_qty

    _log_rebalance_order_placed(
        trade=trade,
        exchange=rebalance_exchange,
        order_id=str(maker_order.order_id),
        side=rebalance_side,
        qty=rebalance_qty,
        price=order_price,
        time_in_force=TimeInForce.POST_ONLY,
        post_only=True,
    )

    logger.info(f"Rebalance maker order placed: {maker_order.order_id} on {rebalance_exchange.value}")

    # Wait for fill with timeout
    start_time = time.time()
    updated = None

    while time.time() - start_time < maker_timeout:
        await asyncio.sleep(0.5)
        updated = await adapter.get_order(maker_order.order_id)
        if updated:
            if updated.is_filled or updated.status == OrderStatus.FILLED:
                return updated, Decimal("0")
            if not updated.is_active:
                break

    # Cancel maker if not filled
    if maker_order and (not updated or updated.is_active):
        try:
            logger.info(f"Cancelling maker order {maker_order.order_id} before IOC fallback")
            await adapter.cancel_order(maker_order.order_id, trade.symbol)
        except Exception as e:
            logger.warning(f"Failed to cancel maker order {maker_order.order_id}: {e}")

    # Calculate remaining qty
    remaining_qty = rebalance_qty
    if updated and updated.filled_qty > 0:
        remaining_qty = max(Decimal("0"), rebalance_qty - updated.filled_qty)

    return updated, remaining_qty


async def _execute_rebalance_ioc(
    self,
    trade: Trade,
    adapter: "ExchangePort",
    rebalance_exchange: Exchange,
    rebalance_side: Side,
    remaining_qty: Decimal,
    tick: Decimal,
) -> "Order | None":
    """Execute IOC fallback for rebalance."""
    ob = self.market_data.get_orderbook(trade.symbol)
    price_data = self.market_data.get_price(trade.symbol)

    # Get aggressive price for IOC
    if rebalance_side == Side.BUY:
        if rebalance_exchange == Exchange.LIGHTER:
            base_price = ob.lighter_ask if ob and ob.lighter_ask > 0 else (price_data.lighter_price if price_data else Decimal("0"))
        else:
            base_price = ob.x10_ask if ob and ob.x10_ask > 0 else (price_data.x10_price if price_data else Decimal("0"))
    else:
        if rebalance_exchange == Exchange.LIGHTER:
            base_price = ob.lighter_bid if ob and ob.lighter_bid > 0 else (price_data.lighter_price if price_data else Decimal("0"))
        else:
            base_price = ob.x10_bid if ob and ob.x10_bid > 0 else (price_data.x10_price if price_data else Decimal("0"))

    if base_price <= 0:
        return None

    ioc_price = _round_price_to_tick(base_price, tick, rounding="down")

    ioc_request = OrderRequest(
        symbol=trade.symbol,
        exchange=rebalance_exchange,
        side=rebalance_side,
        qty=remaining_qty,
        price=ioc_price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )

    ioc_order = await adapter.place_order(ioc_request)

    if ioc_order:
        _log_rebalance_order_placed(
            trade=trade,
            exchange=rebalance_exchange,
            order_id=str(ioc_order.order_id),
            side=rebalance_side,
            qty=remaining_qty,
            price=ioc_price,
            time_in_force=TimeInForce.IOC,
            post_only=False,
        )

    return ioc_order


def _update_leg_after_fill(
    rebalance_leg: TradeLeg,
    filled_qty: Decimal,
    fee: Decimal,
) -> None:
    """
    Update leg state after a fill.

    Reduces the leg's filled_qty and accumulates fees.
    Ensures filled_qty never goes negative.

    Args:
        rebalance_leg: The TradeLeg to update.
        filled_qty: Quantity that was filled (to subtract).
        fee: Trading fee to accumulate.
    """
    rebalance_leg.filled_qty = max(Decimal("0"), rebalance_leg.filled_qty - filled_qty)
    rebalance_leg.fees += fee


async def _rebalance_trade(self, trade: Trade, leg1_mark_price: Decimal | None = None, leg2_mark_price: Decimal | None = None) -> None:
    """
    Phase 3: Restore delta neutrality without full position close.

    Orchestrates: delta calc → maker order → IOC fallback → state update.

    Args:
        trade: The active trade with delta drift
        leg1_mark_price: Current mark price for leg1 (Lighter)
        leg2_mark_price: Current mark price for leg2 (X10)

    NOTE: This IS a reduce-only operation - it closes part of the larger leg.
    """
    ts = self.settings.trading
    maker_timeout = float(getattr(ts, "rebalance_maker_timeout_seconds", 6.0))
    use_ioc_fallback = bool(getattr(ts, "rebalance_use_ioc_fallback", True))

    # 1. Calculate rebalance target
    rebalance_exchange, rebalance_leg, rebalance_notional, net_delta = _calculate_rebalance_target(
        trade, leg1_mark_price, leg2_mark_price
    )

    # Calculate notionals for logging
    leg1_px = leg1_mark_price if leg1_mark_price and leg1_mark_price > 0 else trade.leg1.entry_price
    leg2_px = leg2_mark_price if leg2_mark_price and leg2_mark_price > 0 else trade.leg2.entry_price
    leg1_notional = trade.leg1.filled_qty * leg1_px
    leg2_notional = trade.leg2.filled_qty * leg2_px

    _log_rebalance_start(
        trade=trade,
        net_delta=net_delta,
        leg1_notional=leg1_notional,
        leg2_notional=leg2_notional,
        rebalance_exchange=rebalance_exchange,
        rebalance_notional=rebalance_notional,
    )

    # 2. Calculate rebalancing quantity
    rebalance_px = leg1_px if rebalance_exchange == Exchange.LIGHTER else leg2_px
    if rebalance_px <= 0:
        logger.warning(f"Cannot rebalance {trade.symbol}: invalid price for {rebalance_exchange.value}")
        return

    rebalance_qty = rebalance_notional / rebalance_px
    adapter = self.lighter if rebalance_exchange == Exchange.LIGHTER else self.x10
    rebalance_side = rebalance_leg.side.inverse()

    # 3. Get price and tick
    market_info = self.market_data.get_market_info(trade.symbol, rebalance_exchange)
    tick = market_info.tick_size if market_info else Decimal("0.01")

    base_price = _get_rebalance_price(self, trade, rebalance_exchange, rebalance_side)
    if not base_price:
        logger.warning(f"Cannot rebalance {trade.symbol}: no price data for {rebalance_exchange.value}")
        return

    order_price = _round_price_to_tick(base_price, tick, rounding="down")

    logger.info(
        f"Rebalancing {trade.symbol}: {rebalance_side.value} {rebalance_qty:.6f} @ {order_price:.2f} "
        f"on {rebalance_exchange.value} (net_delta={net_delta:.2f}, rebalance_notional=${rebalance_notional:.2f})"
    )

    try:
        # 4. Execute maker order
        updated, remaining_qty = await _execute_rebalance_maker(
            self, trade, adapter, rebalance_exchange, rebalance_side,
            rebalance_qty, order_price, maker_timeout
        )

        # Handle maker fill
        if updated and updated.is_filled:
            logger.info(
                f"Rebalance maker filled: {updated.filled_qty}/{rebalance_qty} "
                f"@ {updated.avg_fill_price:.2f} on {rebalance_exchange.value}"
            )
            _update_leg_after_fill(rebalance_leg, updated.filled_qty, updated.fee)
            await self._update_trade(trade)
            _log_rebalance_complete(trade, updated.filled_qty, updated.avg_fill_price, updated.fee)
            return

        # Handle partial fill
        if updated and updated.filled_qty > 0:
            logger.info(f"Maker partially filled: {updated.filled_qty}/{rebalance_qty}, IOC qty: {remaining_qty}")
            _update_leg_after_fill(rebalance_leg, updated.filled_qty, updated.fee)
            await self._update_trade(trade)

        # 5. IOC fallback
        if use_ioc_fallback and remaining_qty > 0:
            logger.info(f"Rebalance maker not filled within {maker_timeout}s, trying IOC fallback")

            ioc_order = await _execute_rebalance_ioc(
                self, trade, adapter, rebalance_exchange, rebalance_side, remaining_qty, tick
            )

            if ioc_order and ioc_order.is_filled:
                logger.info(
                    f"Rebalance IOC filled: {ioc_order.filled_qty}/{remaining_qty} "
                    f"@ {ioc_order.avg_fill_price:.2f} on {rebalance_exchange.value}"
                )
                _update_leg_after_fill(rebalance_leg, ioc_order.filled_qty, ioc_order.fee)
                await self._update_trade(trade)
                _log_rebalance_complete(trade, ioc_order.filled_qty, ioc_order.avg_fill_price, ioc_order.fee)
            elif ioc_order and not ioc_order.is_filled:
                logger.warning(f"Rebalance IOC not filled: {ioc_order.order_id} on {rebalance_exchange.value}")
            else:
                logger.warning(f"Rebalance IOC failed on {rebalance_exchange.value}")

    except Exception as e:
        logger.warning(f"Rebalance failed for {trade.symbol}: {e}")


async def _close_both_legs_coordinated(self, trade: Trade) -> None:
    """
    Phase 3: Coordinated dual-leg close to minimize unhedged exposure.

    Instead of sequential close (Lighter → wait → X10), this function:
    1. Submits POST_ONLY maker orders on BOTH legs simultaneously
    2. Waits for fills with short timeout (6s)
    3. Escalates UNFILLED legs to IOC on BOTH (no unhedged window)
    4. Final MARKET fallback if needed

    Benefits:
    - Minimizes unhedged exposure (both legs close in parallel)
    - Uses maker orders on X10 (post_only flag) for fee savings
    - Faster execution overall

    Args:
        trade: The trade to close using coordinated strategy

    Configuration:
        - coordinated_close_enabled: Enable this feature
        - coordinated_close_maker_timeout_seconds: Timeout for maker phase
        - coordinated_close_ioc_timeout_seconds: Timeout for IOC phase
        - coordinated_close_x10_use_maker: Use maker on X10 (not just IOC)
    """
    ts = self.settings.trading
    coordinated_enabled = bool(getattr(ts, "coordinated_close_enabled", True))
    x10_use_maker = bool(getattr(ts, "coordinated_close_x10_use_maker", True))
    maker_timeout = float(getattr(ts, "coordinated_close_maker_timeout_seconds", 6.0))
    _ioc_timeout = float(getattr(ts, "coordinated_close_ioc_timeout_seconds", 2.0))  # Reserved for future IOC phase
    escalate_to_ioc = bool(getattr(ts, "coordinated_close_escalate_to_ioc", True))

    if not coordinated_enabled:
        logger.info(f"Coordinated close disabled for {trade.symbol}, falling back to sequential")
        await self._close_lighter_smart(trade)
        await self._update_trade(trade)
        logger.info(f"Closing X10 for {trade.symbol}")
        await self._close_x10_smart(trade)
        await self._update_trade(trade)
        return

    # Log coordinated close start
    _log_coordinated_close_start(trade)

    logger.info(f"Coordinated close for {trade.symbol} - parallel maker orders on both legs")

    # Get current positions and prices
    price_data = self.market_data.get_price(trade.symbol)
    ob = self.market_data.get_orderbook(trade.symbol)

    # Get close quantities
    leg1_close_qty = trade.leg1.filled_qty
    leg2_close_qty = trade.leg2.filled_qty

    if leg1_close_qty == 0 and leg2_close_qty == 0:
        logger.info(f"No position to close for {trade.symbol}")
        return

    # Get market info for tick sizes
    lighter_info = self.market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
    x10_info = self.market_data.get_market_info(trade.symbol, Exchange.X10)
    lighter_tick = lighter_info.tick_size if lighter_info else Decimal("0.01")
    x10_tick = x10_info.tick_size if x10_info else Decimal("0.01")

    # Determine close sides
    leg1_close_side = trade.leg1.side.inverse()
    leg2_close_side = trade.leg2.side.inverse()

    # Calculate maker prices
    leg1_maker_price = Decimal("0")
    leg2_maker_price = Decimal("0")

    if leg1_close_qty > 0:
        if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
            leg1_maker_price = ob.lighter_bid if leg1_close_side == Side.SELL else ob.lighter_ask
        elif price_data and price_data.lighter_price > 0:
            leg1_maker_price = price_data.lighter_price

    if leg2_close_qty > 0:
        if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
            leg2_maker_price = ob.x10_bid if leg2_close_side == Side.SELL else ob.x10_ask
        elif price_data and price_data.x10_price > 0:
            leg2_maker_price = price_data.x10_price

    leg1_maker_price = _round_price_to_tick(leg1_maker_price, lighter_tick, rounding="down") if leg1_maker_price > 0 else Decimal("0")
    leg2_maker_price = _round_price_to_tick(leg2_maker_price, x10_tick, rounding="down") if leg2_maker_price > 0 else Decimal("0")

    # Track which legs are still open
    legs_to_close = []
    if leg1_close_qty > 0:
        legs_to_close.append("leg1")
    if leg2_close_qty > 0:
        legs_to_close.append("leg2")

    # Step 1: Submit MAKER orders on BOTH legs simultaneously
    maker_orders: dict[str, Order | None] = {}
    maker_order_ids: dict[str, str] = {}

    try:
        # Submit both maker orders in parallel using dict for clear leg->task mapping
        maker_tasks: dict[str, Awaitable[Order]] = {}
        if leg1_close_qty > 0 and leg1_maker_price > 0:
            logger.info(f"Submitting Lighter maker: {leg1_close_side.value} {leg1_close_qty} @ {leg1_maker_price}")
            maker_tasks["leg1"] = self._submit_maker_order(
                trade, Exchange.LIGHTER, "leg1", leg1_close_side, leg1_close_qty, leg1_maker_price, lighter_tick
            )

        if leg2_close_qty > 0 and leg2_maker_price > 0 and x10_use_maker:
            logger.info(f"Submitting X10 maker: {leg2_close_side.value} {leg2_close_qty} @ {leg2_maker_price}")
            maker_tasks["leg2"] = self._submit_maker_order(
                trade, Exchange.X10, "leg2", leg2_close_side, leg2_close_qty, leg2_maker_price, x10_tick
            )

        if maker_tasks:
            # Execute tasks in parallel and map results directly to legs
            results = await asyncio.gather(*maker_tasks.values(), return_exceptions=True)
            for leg, result in zip(maker_tasks.keys(), results, strict=False):
                if isinstance(result, BaseException):
                    logger.warning(f"Maker order failed for {leg}: {result}")
                    maker_orders[leg] = None
                else:
                    order_result: Order | None = result
                    maker_orders[leg] = order_result
                    if order_result:
                        maker_order_ids[leg] = order_result.order_id
                        logger.info(f"Maker order placed: {leg} = {order_result.order_id}")

        # Wait for fills with timeout
        start_time = time.time()
        filled_legs = set()

        while time.time() - start_time < maker_timeout and legs_to_close:
            await asyncio.sleep(0.3)

            for leg in list(legs_to_close):
                if leg not in maker_order_ids:
                    continue

                order_id = maker_order_ids[leg]
                adapter = self.lighter if leg == "leg1" else self.x10

                try:
                    updated = await adapter.get_order(order_id)
                    if updated:
                        if updated.is_filled or updated.status == OrderStatus.FILLED:
                            logger.info(f"Maker filled for {leg}: {updated.filled_qty} @ {updated.avg_fill_price}")
                            filled_legs.add(leg)
                            legs_to_close.remove(leg)
                            # Update trade leg with exit price and accumulate fees
                            if leg == "leg1":
                                trade.leg1.exit_price = updated.avg_fill_price
                                trade.leg1.fees += updated.fee
                            else:
                                trade.leg2.exit_price = updated.avg_fill_price
                                trade.leg2.fees += updated.fee
                        elif not updated.is_active:
                            logger.warning(f"Maker order {leg} no longer active: {updated.status}")
                            legs_to_close.remove(leg)
                except Exception as e:
                    logger.warning(f"Failed to check maker order for {leg}: {e}")

        if not legs_to_close:
            logger.info(f"All legs filled via maker orders for {trade.symbol}")
            # Log coordinated close complete
            _log_coordinated_close_complete(trade=trade, filled_legs=filled_legs)
            return

        logger.info(f"Maker timeout: {len(legs_to_close)} legs remaining ({', '.join(legs_to_close)})")

        # Step 2: Escalate UNFILLED legs to IOC SYNCHRONOUSLY (no unhedged window)
        if escalate_to_ioc:
            # Log IOC escalation
            _log_coordinated_close_ioc_escalate(trade=trade, legs=list(legs_to_close))

            logger.info(f"Escalating to IOC for unfilled legs: {', '.join(legs_to_close)} (synchronous)")

            # Build IOC tasks for ALL unfilled legs simultaneously
            ioc_tasks = []

            if "leg1" in legs_to_close and leg1_close_qty > 0:
                # Calculate IOC price (cross spread)
                ioc_price = Decimal("0")
                if ob and ob.lighter_bid > 0 and ob.lighter_ask > 0:
                    ioc_price = ob.lighter_ask if leg1_close_side == Side.BUY else ob.lighter_bid
                elif price_data and price_data.lighter_price > 0:
                    ioc_price = price_data.lighter_price

                if ioc_price > 0:
                    ioc_price = _round_price_to_tick(ioc_price, lighter_tick, rounding="down")
                    ioc_tasks.append(self._execute_ioc_close(
                        trade, Exchange.LIGHTER, "leg1", leg1_close_side, leg1_close_qty, ioc_price
                    ))

            if "leg2" in legs_to_close and leg2_close_qty > 0:
                # Calculate IOC price (cross spread)
                ioc_price = Decimal("0")
                if ob and ob.x10_bid > 0 and ob.x10_ask > 0:
                    ioc_price = ob.x10_ask if leg2_close_side == Side.BUY else ob.x10_bid
                elif price_data and price_data.x10_price > 0:
                    ioc_price = price_data.x10_price

                if ioc_price > 0:
                    ioc_price = _round_price_to_tick(ioc_price, x10_tick, rounding="down")
                    ioc_tasks.append(self._execute_ioc_close(
                        trade, Exchange.X10, "leg2", leg2_close_side, leg2_close_qty, ioc_price
                    ))

            # Execute ALL IOC tasks SIMULTANEOUSLY (no unhedged window)
            if ioc_tasks:
                results = await asyncio.gather(*ioc_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(f"IOC close failed: {result}")
                    else:
                        logger.info(f"IOC close completed: {result}")

                # Log coordinated close END after IOC escalation
                _log_coordinated_close_complete(trade=trade, filled_legs=filled_legs)

    except Exception as e:
        logger.warning(f"Coordinated close failed for {trade.symbol}: {e}")
        # Fallback to sequential close
        logger.info(f"Falling back to sequential close for {trade.symbol}")
        await self._close_lighter_smart(trade)
        await self._update_trade(trade)
        await self._close_x10_smart(trade)
        await self._update_trade(trade)


async def _submit_maker_order(
    self,
    trade: Trade,
    exchange: Exchange,
    leg: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
    tick: Decimal,
) -> Order | None:
    """Submit a maker POST_ONLY order for coordinated close."""
    adapter = self.lighter if exchange == Exchange.LIGHTER else self.x10

    # Both exchanges support POST_ONLY TimeInForce
    # X10 adapter converts POST_ONLY to post_only=True flag internally
    request = OrderRequest(
        symbol=trade.symbol,
        exchange=exchange,
        side=side,
        qty=qty,
        price=_round_price_to_tick(price, tick, rounding="down"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=True,
    )

    order = await adapter.place_order(request)

    if order:
        # Log coordinated close specific event
        _log_coordinated_close_maker_placed(
            trade=trade,
            exchange=exchange,
            leg=leg,
            order_id=str(order.order_id),
            side=side,
            qty=qty,
            price=price,
        )

        logger.info(f"Maker order submitted: {exchange.value} {leg} {side.value} {qty} @ {price} = {order.order_id}")

        # Log the close order event (for post-close readback)
        _log_close_order_placed(
            trade=trade,
            exchange=exchange,
            leg=leg,
            order_id=order.order_id,
            client_order_id=getattr(order, "client_order_id", None),
            side=side,
            qty=qty,
            price=price,
            time_in_force=request.time_in_force,
            post_only=True,  # POST_ONLY always means post_only=True
        )

    return order


async def _execute_ioc_close(
    self,
    trade: Trade,
    exchange: Exchange,
    leg: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
) -> None:
    """Execute an IOC close order for coordinated close escalation."""
    adapter = self.lighter if exchange == Exchange.LIGHTER else self.x10

    request = OrderRequest(
        symbol=trade.symbol,
        exchange=exchange,
        side=side,
        qty=qty,
        price=price,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )

    order = await adapter.place_order(request)

    if order and order.is_filled:
        logger.info(f"IOC filled for {leg}: {order.filled_qty} @ {order.avg_fill_price}")
        # Update trade leg with exit price and accumulate fees
        if leg == "leg1":
            trade.leg1.exit_price = order.avg_fill_price
            trade.leg1.fees += order.fee
        else:
            trade.leg2.exit_price = order.avg_fill_price
            trade.leg2.fees += order.fee
    elif order:
        logger.warning(f"IOC not filled for {leg}: {order.order_id}")


async def _close_x10_smart(self, trade: Trade) -> None:
    """Close X10 leg with Limit IOC."""
    if trade.leg2.filled_qty == 0:
        return

    # Broken-hedge safety: if the X10 position is already gone, do NOT place new orders.
    pos_fetch_ok = True
    try:
        pos = await self.x10.get_position(trade.symbol)
    except Exception as e:
        logger.warning(f"Failed to fetch X10 position for close ({trade.symbol}): {e}")
        pos_fetch_ok = False
        pos = None

    qty_threshold = Decimal("0.0001")
    close_reason = trade.close_reason or ""
    missing_x10_expected = "_missing_x10" in close_reason
    if pos_fetch_ok:
        if not pos or pos.qty <= qty_threshold:
            if missing_x10_expected:
                logger.info(f"X10 position not found/empty for {trade.symbol}; skipping X10 close")
                return
            # If the exchange confirms there is no position, do NOT attempt a close using
            # the DB snapshot — reduce-only will be rejected anyway and creates noisy stack traces.
            logger.info(f"X10 position not found/empty for {trade.symbol}; skipping X10 close")
            return
        if getattr(pos, "side", None) and pos.side != trade.leg2.side:
            logger.warning(
                f"X10 position side mismatch for {trade.symbol}: "
                f"expected={trade.leg2.side.value} actual={pos.side.value}; skipping X10 close"
            )
            return

    close_side = trade.leg2.side.inverse()
    qty = (
        pos.qty
        if (pos_fetch_ok and pos and pos.qty > qty_threshold)
        else trade.leg2.filled_qty
    )

    # Get price (prefer X10 L1 to ensure marketable IOC)
    price_data = self.market_data.get_price(trade.symbol)
    ob = self.market_data.get_orderbook(trade.symbol)
    base_price = Decimal("0")
    if close_side == Side.BUY:
        if ob and ob.x10_ask > 0:
            base_price = ob.x10_ask
        elif price_data and price_data.x10_price > 0:
            base_price = price_data.x10_price
        elif ob and ob.x10_bid > 0:
            base_price = ob.x10_bid
    else:
        if ob and ob.x10_bid > 0:
            base_price = ob.x10_bid
        elif price_data and price_data.x10_price > 0:
            base_price = price_data.x10_price
        elif ob and ob.x10_ask > 0:
            base_price = ob.x10_ask

    if base_price <= 0:
        # Fallback to pure market
        await self._close_leg(self.x10, trade, trade.leg2)
        return

    es = self.settings.execution
    base_slippage = _safe_decimal(getattr(es, "x10_close_slippage", None), Decimal("0.005"))
    max_slippage = _safe_decimal(getattr(es, "x10_close_max_slippage", None), Decimal("0.02"))
    close_attempts = int(getattr(es, "x10_close_attempts", 3) or 3)
    close_attempts = max(1, min(close_attempts, 5))

    closed_qty_total = Decimal("0")
    closed_notional_total = Decimal("0")
    closed_fee_total = Decimal("0")

    x10_info = self.market_data.get_market_info(trade.symbol, Exchange.X10)
    tick = x10_info.tick_size if x10_info else Decimal("0.01")

    def _round_price(p: Decimal) -> Decimal:
        if tick <= 0:
            return p
        rounding = "ROUND_CEILING" if close_side == Side.BUY else "ROUND_FLOOR"
        return (p / tick).quantize(Decimal("1"), rounding=rounding) * tick

    remaining_qty = qty
    last_error: Exception | None = None

    for attempt in range(close_attempts):
        slippage = min(base_slippage * Decimal(str(attempt + 1)), max_slippage)
        if close_side == Side.BUY:
            price = base_price * (Decimal("1") + slippage)
        else:
            price = base_price * (Decimal("1") - slippage)
        price = _round_price(price)

        try:
            request = OrderRequest(
                symbol=trade.symbol,
                exchange=Exchange.X10,
                side=close_side,
                qty=remaining_qty,
                order_type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )

            order = await self.x10.place_order(request)

            # T1: Log X10 IOC close order for post-close readback (T3)
            _log_close_order_placed(
                trade=trade,
                exchange=Exchange.X10,
                leg="leg2",
                order_id=str(order.order_id),
                client_order_id=getattr(order, "client_order_id", None),
                side=close_side,
                qty=remaining_qty,
                price=price,
                time_in_force=TimeInForce.IOC,
                post_only=False,
                attempt_id=attempt + 1,  # IOC attempt number
            )

            # Wait a moment for IOC result
            await asyncio.sleep(0.5)
            try:
                filled = await asyncio.wait_for(
                    self.x10.get_order(trade.symbol, order.order_id),
                    timeout=_close_get_order_timeout_seconds(self),
                )
            except TimeoutError:
                filled = None

            if filled and filled.filled_qty > 0:
                filled_qty = filled.filled_qty
                avg_price = filled.avg_fill_price if filled.avg_fill_price > 0 else price
                closed_qty_total += filled_qty
                closed_notional_total += filled_qty * avg_price
                closed_fee_total += filled.fee

                remaining_qty = remaining_qty - filled_qty
                if remaining_qty <= qty_threshold:
                    break
            else:
                last_error = Exception("IOC not filled")

        except Exception as e:
            if _is_x10_reduce_only_missing_position_error(e):
                logger.info(f"X10 reduce-only close rejected (position missing) for {trade.symbol}; treating as closed")
                return
            last_error = e
            logger.warning(f"X10 close attempt {attempt + 1}/{close_attempts} failed: {e}")

        # Check if position is still open; if closed, stop retrying.
        try:
            pos = await self.x10.get_position(trade.symbol)
            if not pos or pos.qty <= qty_threshold or pos.side != trade.leg2.side:
                remaining_qty = Decimal("0")
                break
            remaining_qty = pos.qty
        except Exception as e_pos:
            logger.warning(f"Failed to fetch X10 position after close attempt: {e_pos}")

        # Refresh base price from latest orderbook snapshot if available.
        ob = self.market_data.get_orderbook(trade.symbol)
        if close_side == Side.BUY:
            if ob and ob.x10_ask > 0:
                base_price = ob.x10_ask
        else:
            if ob and ob.x10_bid > 0:
                base_price = ob.x10_bid

    if closed_qty_total > 0:
        trade.leg2.exit_price = (
            closed_notional_total / closed_qty_total if closed_qty_total > 0 else trade.leg2.exit_price
        )
        trade.leg2.fees += closed_fee_total

    if remaining_qty > qty_threshold:
        logger.error(
            f"X10 Smart Close incomplete for {trade.symbol}; remaining={remaining_qty}. "
            f"Last error: {last_error}"
        )
        # Fallback: force market close for remaining size
        await self._close_leg(self.x10, trade, trade.leg2, override_qty=remaining_qty)
        return


async def _close_leg(
    self,
    adapter: ExchangePort,
    trade: Trade,
    leg: TradeLeg,
    override_qty: Decimal | None = None,
    *,
    update_leg: bool = True,
) -> Order | None:
    """Close a single leg using Market Order."""
    # Use override_qty if provided (from smart fallback), otherwise full leg size
    close_qty = override_qty if override_qty is not None else leg.filled_qty

    if close_qty <= 0:
        return None

    logger.info(f"Executing Market Close for {trade.symbol} on {adapter.exchange}: {close_qty}")

    # Place reduce-only market order
    request = OrderRequest(
        symbol=trade.symbol,
        exchange=adapter.exchange,
        side=leg.side.inverse(),
        qty=close_qty,
        order_type=OrderType.MARKET,
        reduce_only=True,
    )

    filled_order: Order | None = None
    try:
        order = await adapter.place_order(request)

        # T1: Log market close order for post-close readback (T3)
        _log_close_order_placed(
            trade=trade,
            exchange=adapter.exchange,
            leg="leg1" if adapter.exchange == Exchange.LIGHTER else "leg2",
            order_id=str(order.order_id),
            client_order_id=getattr(order, "client_order_id", None),
            side=leg.side.inverse(),
            qty=close_qty,
            price=Decimal("0"),  # Market orders have no price
            time_in_force=TimeInForce.IOC,  # Market orders behave like IOC
            post_only=False,
        )

        # Wait for fill
        threshold = Decimal("0.0001")
        last_seen: Order | None = None
        pos_closed = False
        order_id = str(getattr(order, "order_id", "") or "").strip()

        def _order_timeout_seconds() -> float:
            base = _close_get_order_timeout_seconds(self)
            # Lighter signed orders often return `pending_*` IDs and require resolution.
            # Give `get_order` more time in that case so we can capture avg_fill_price/fees
            # for correct realized PnL accounting.
            if adapter.exchange == Exchange.LIGHTER and order_id.startswith("pending_"):
                return max(base, 6.0)
            return base

        for _ in range(20):  # 10 seconds max
            try:
                filled = await asyncio.wait_for(
                    adapter.get_order(trade.symbol, order_id),
                    timeout=_order_timeout_seconds(),
                )
            except TimeoutError:
                filled = None
            if filled:
                last_seen = filled
                # Prefer resolved system order_id for subsequent polling.
                if filled.order_id and not str(filled.order_id).startswith("pending_"):
                    order_id = str(filled.order_id)
            if filled and filled.status.is_terminal():
                if update_leg:
                    leg.exit_price = filled.avg_fill_price
                    leg.fees += filled.fee
                filled_order = filled
                break

            # Fallback: infer completion from position state if order status can't be fetched.
            try:
                pos = await asyncio.wait_for(
                    adapter.get_position(trade.symbol),
                    timeout=_close_get_order_timeout_seconds(self),
                )
            except TimeoutError:
                pos = None
            if not pos or pos.qty <= threshold:
                pos_closed = True
                filled_order = filled_order or filled
                break
            await asyncio.sleep(0.5)

        # If the position is flat but we couldn't fetch order fills (common with Lighter pending IDs),
        # do one best-effort fetch with a longer timeout, then fall back to orderbook-based
        # approximation so realized PnL isn't missing an entire leg.
        if pos_closed and (filled_order is None or filled_order.avg_fill_price <= 0):
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                best_effort = await asyncio.wait_for(
                    adapter.get_order(trade.symbol, order_id),
                    timeout=max(_order_timeout_seconds(), 8.0),
                )
                if best_effort:
                    filled_order = best_effort
                    last_seen = best_effort

        if pos_closed and (filled_order is None or filled_order.avg_fill_price <= 0):
            approx_price = Decimal("0")
            ob = None
            with contextlib.suppress(Exception):
                ob = self.market_data.get_orderbook(trade.symbol) if self.market_data else None
            if ob:
                if adapter.exchange == Exchange.LIGHTER:
                    approx_price = ob.lighter_ask if request.side == Side.BUY else ob.lighter_bid
                else:
                    approx_price = ob.x10_ask if request.side == Side.BUY else ob.x10_bid

            approx_fee = Decimal("0")
            if approx_price > 0:
                with contextlib.suppress(Exception):
                    fees = await adapter.get_fee_schedule(trade.symbol)
                    taker = _safe_decimal(fees.get("taker_fee"), Decimal("0"))
                    approx_fee = (close_qty * approx_price * taker).copy_abs()

            filled_order = Order(
                order_id=order_id or getattr(order, "order_id", "") or "unknown",
                symbol=trade.symbol,
                exchange=adapter.exchange,
                side=request.side,
                order_type=OrderType.MARKET,
                qty=close_qty,
                price=None,
                status=OrderStatus.FILLED,
                filled_qty=close_qty,
                avg_fill_price=approx_price,
                fee=approx_fee,
                client_order_id=getattr(order, "client_order_id", None),
            )

        if update_leg and filled_order and filled_order.avg_fill_price and filled_order.avg_fill_price > 0:
            # Only apply if we haven't already recorded an exit (avoid double counting).
            if getattr(leg, "exit_price", Decimal("0")) <= 0:
                leg.exit_price = filled_order.avg_fill_price
            leg.fees += filled_order.fee

        # If we still don't have a filled order, return the last observed status to aid callers.
        if filled_order is None:
            filled_order = last_seen
    except Exception as e:
        logger.error(f"Market Close failed for {trade.symbol} on {adapter.exchange}: {e}")
    return filled_order


async def _verify_closed(self, trade: Trade) -> None:
    """Verify positions are actually closed on both exchanges."""
    threshold = Decimal("0.0001")
    still_open: list[str] = []

    # Check both exchanges in parallel
    results = await asyncio.gather(
        self.lighter.get_position(trade.symbol),
        self.x10.get_position(trade.symbol),
        return_exceptions=True
    )

    # Handle exceptions from gather with proper typing
    lighter_pos: Position | None = results[0] if not isinstance(results[0], BaseException) else None
    x10_pos: Position | None = results[1] if not isinstance(results[1], BaseException) else None

    # Check Lighter
    if lighter_pos and lighter_pos.qty > threshold:
        still_open.append(f"LIGHTER {lighter_pos.side} qty={lighter_pos.qty}")

    # Check X10
    if x10_pos and x10_pos.qty > threshold:
        still_open.append(f"X10 {x10_pos.side} qty={x10_pos.qty}")

    if still_open:
        details = " | ".join(still_open)
        logger.error(f"Close verification failed for {trade.symbol}: {details}")
        raise PositionsStillOpenError(f"Positions still open for {trade.symbol}: {details}")
