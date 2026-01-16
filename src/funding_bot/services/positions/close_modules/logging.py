"""
Structured logging helpers for close operations.

Provides consistent logging format for all close-related events,
enabling monitoring, dashboards, and trade reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import Exchange, Side, TimeInForce, Trade
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


def log_final_execution_metrics(
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
        "CLOSE_FINAL: %s %s VWAP(leg1)=%.4f VWAP(leg2)=%.4f "
        "fees(leg1)=$%.2f fees(leg2)=$%.2f net=$%.2f",
        trade.symbol,
        trade.trade_id[:8],
        leg1_vwap if leg1_vwap > 0 else 0,
        leg2_vwap if leg2_vwap > 0 else 0,
        leg1_fees,
        leg2_fees,
        total_pnl,
        extra={"structured_data": log_data},
    )


def log_close_order_placed(
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


def log_rebalance_start(
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
    logger.info(
        f"Rebalance start logged: {rebalance_exchange.value} "
        f"notional=${rebalance_notional:.2f}"
    )


def log_rebalance_order_placed(
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
    logger.info(
        f"Rebalance order logged: {exchange.value} order_id={order_id} "
        f"side={side.value} qty={qty}"
    )


def log_rebalance_complete(
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


def log_coordinated_close_start(trade: Trade) -> None:
    """Log coordinated close start event to trade.events."""
    trade.events.append({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "COORDINATED_CLOSE_START",
    })
    logger.info(f"Coordinated close start logged for {trade.symbol}")


def log_coordinated_close_maker_placed(
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
    logger.info(
        f"Coordinated close maker logged: {exchange.value} {leg} order_id={order_id}"
    )


def log_coordinated_close_ioc_escalate(
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


def log_coordinated_close_complete(
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
