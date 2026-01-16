"""
Prometheus metrics for observability.

Provides metrics for monitoring close operations, rebalancing,
and other position management activities.

Usage:
    from funding_bot.observability.metrics import (
        close_operations_total,
        close_duration_seconds,
        record_close_operation,
    )

    # Record a close operation
    record_close_operation(
        symbol="ETH",
        reason="ECON_EXIT",
        success=True,
        duration_seconds=2.5,
        pnl_usd=15.50,
    )

Note: Metrics are only available if prometheus_client is installed.
Falls back to no-op if not installed (for dev/test environments).
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

# Try to import prometheus_client, fall back to no-op if not available
try:
    from prometheus_client import Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


# =============================================================================
# Metric Definitions
# =============================================================================

if PROMETHEUS_AVAILABLE:
    # Close operation metrics
    close_operations_total = Counter(
        "funding_bot_close_operations_total",
        "Total number of close operations",
        ["symbol", "reason", "success"],
    )

    close_duration_seconds = Histogram(
        "funding_bot_close_duration_seconds",
        "Duration of close operations in seconds",
        ["symbol", "reason"],
        buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
    )

    close_pnl_usd = Histogram(
        "funding_bot_close_pnl_usd",
        "Realized PnL of close operations in USD",
        ["symbol"],
        buckets=(-100, -50, -20, -10, -5, 0, 5, 10, 20, 50, 100, 200, 500),
    )

    # Rebalance metrics
    rebalance_operations_total = Counter(
        "funding_bot_rebalance_operations_total",
        "Total number of rebalance operations",
        ["symbol", "exchange", "success"],
    )

    rebalance_notional_usd = Histogram(
        "funding_bot_rebalance_notional_usd",
        "Notional value of rebalance operations in USD",
        ["symbol"],
        buckets=(10, 50, 100, 200, 500, 1000, 2000, 5000),
    )

    # Coordinated close metrics
    coordinated_close_total = Counter(
        "funding_bot_coordinated_close_total",
        "Total coordinated close operations",
        ["symbol", "outcome"],  # outcome: maker_filled, ioc_escalated, fallback
    )

    coordinated_close_maker_fill_rate = Gauge(
        "funding_bot_coordinated_close_maker_fill_rate",
        "Rate of maker fills in coordinated closes (0-1)",
        ["symbol"],
    )

    # Active positions gauge
    active_positions = Gauge(
        "funding_bot_active_positions",
        "Number of active positions",
        ["symbol"],
    )

    # Order metrics
    close_orders_total = Counter(
        "funding_bot_close_orders_total",
        "Total close orders placed",
        ["symbol", "exchange", "order_type", "outcome"],
    )

    # Fee metrics
    close_fees_usd = Counter(
        "funding_bot_close_fees_usd_total",
        "Total fees paid on close operations in USD",
        ["symbol", "exchange"],
    )

    # =========================================================================
    # Execution Metrics
    # =========================================================================

    # Execution operation metrics
    execution_total = Counter(
        "funding_bot_execution_total",
        "Total trade executions",
        ["symbol", "outcome"],  # outcome: success, leg1_failed, leg2_failed, rollback
    )

    execution_duration_seconds = Histogram(
        "funding_bot_execution_duration_seconds",
        "Duration of trade executions in seconds",
        ["symbol", "outcome"],
        buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0),
    )

    # Leg metrics
    leg1_orders_total = Counter(
        "funding_bot_leg1_orders_total",
        "Total Leg1 (maker) orders placed",
        ["symbol", "outcome"],  # outcome: filled, partial, cancelled, timeout
    )

    leg1_fill_rate = Gauge(
        "funding_bot_leg1_fill_rate",
        "Fill rate for Leg1 orders (0-1)",
        ["symbol"],
    )

    leg2_orders_total = Counter(
        "funding_bot_leg2_orders_total",
        "Total Leg2 (hedge) orders placed",
        ["symbol", "outcome"],  # outcome: filled, partial, failed
    )

    leg2_fill_rate = Gauge(
        "funding_bot_leg2_fill_rate",
        "Fill rate for Leg2 orders (0-1)",
        ["symbol"],
    )

    # Hedge latency
    hedge_latency_seconds = Histogram(
        "funding_bot_hedge_latency_seconds",
        "Latency from Leg1 fill to Leg2 submission",
        ["symbol"],
        buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0),
    )

    # Rollback metrics
    rollback_total = Counter(
        "funding_bot_rollback_total",
        "Total rollback operations",
        ["symbol", "success"],
    )

    # Sizing metrics
    trade_notional_usd = Histogram(
        "funding_bot_trade_notional_usd",
        "Notional value of executed trades in USD",
        ["symbol"],
        buckets=(100, 500, 1000, 2000, 5000, 10000, 20000, 50000),
    )

    # Active executions gauge
    active_executions = Gauge(
        "funding_bot_active_executions",
        "Number of active trade executions",
        ["symbol"],
    )

else:
    # No-op implementations when prometheus_client is not available
    class NoOpMetric:
        """No-op metric for when prometheus_client is not installed."""

        def labels(self, *args: Any, **kwargs: Any) -> NoOpMetric:
            return self

        def inc(self, amount: float = 1) -> None:
            pass

        def dec(self, amount: float = 1) -> None:
            pass

        def set(self, value: float) -> None:
            pass

        def observe(self, value: float) -> None:
            pass

    close_operations_total = NoOpMetric()  # type: ignore
    close_duration_seconds = NoOpMetric()  # type: ignore
    close_pnl_usd = NoOpMetric()  # type: ignore
    rebalance_operations_total = NoOpMetric()  # type: ignore
    rebalance_notional_usd = NoOpMetric()  # type: ignore
    coordinated_close_total = NoOpMetric()  # type: ignore
    coordinated_close_maker_fill_rate = NoOpMetric()  # type: ignore
    active_positions = NoOpMetric()  # type: ignore
    close_orders_total = NoOpMetric()  # type: ignore
    close_fees_usd = NoOpMetric()  # type: ignore
    # Execution metrics no-ops
    execution_total = NoOpMetric()  # type: ignore
    execution_duration_seconds = NoOpMetric()  # type: ignore
    leg1_orders_total = NoOpMetric()  # type: ignore
    leg1_fill_rate = NoOpMetric()  # type: ignore
    leg2_orders_total = NoOpMetric()  # type: ignore
    leg2_fill_rate = NoOpMetric()  # type: ignore
    hedge_latency_seconds = NoOpMetric()  # type: ignore
    rollback_total = NoOpMetric()  # type: ignore
    trade_notional_usd = NoOpMetric()  # type: ignore
    active_executions = NoOpMetric()  # type: ignore


# =============================================================================
# Helper Functions
# =============================================================================


def record_close_operation(
    symbol: str,
    reason: str,
    success: bool,
    duration_seconds: float,
    pnl_usd: float | Decimal = 0,
) -> None:
    """
    Record a close operation in metrics.

    Args:
        symbol: Trading symbol (e.g., "ETH")
        reason: Close reason (e.g., "ECON_EXIT", "EMERGENCY")
        success: Whether the close was successful
        duration_seconds: Time taken to complete the close
        pnl_usd: Realized PnL in USD
    """
    close_operations_total.labels(
        symbol=symbol, reason=reason, success=str(success).lower()
    ).inc()

    close_duration_seconds.labels(symbol=symbol, reason=reason).observe(
        duration_seconds
    )

    if pnl_usd:
        pnl_float = float(pnl_usd) if isinstance(pnl_usd, Decimal) else pnl_usd
        close_pnl_usd.labels(symbol=symbol).observe(pnl_float)


def record_rebalance_operation(
    symbol: str,
    exchange: str,
    success: bool,
    notional_usd: float | Decimal = 0,
) -> None:
    """
    Record a rebalance operation in metrics.

    Args:
        symbol: Trading symbol
        exchange: Exchange name ("LIGHTER" or "X10")
        success: Whether the rebalance was successful
        notional_usd: Notional value rebalanced in USD
    """
    rebalance_operations_total.labels(
        symbol=symbol, exchange=exchange, success=str(success).lower()
    ).inc()

    if notional_usd:
        notional_float = (
            float(notional_usd) if isinstance(notional_usd, Decimal) else notional_usd
        )
        rebalance_notional_usd.labels(symbol=symbol).observe(notional_float)


def record_coordinated_close(
    symbol: str,
    outcome: str,  # "maker_filled", "ioc_escalated", "fallback"
    maker_legs_filled: int = 0,
    total_legs: int = 2,
) -> None:
    """
    Record a coordinated close operation in metrics.

    Args:
        symbol: Trading symbol
        outcome: Close outcome type
        maker_legs_filled: Number of legs filled via maker orders
        total_legs: Total number of legs (usually 2)
    """
    coordinated_close_total.labels(symbol=symbol, outcome=outcome).inc()

    if total_legs > 0:
        fill_rate = maker_legs_filled / total_legs
        coordinated_close_maker_fill_rate.labels(symbol=symbol).set(fill_rate)


def record_close_order(
    symbol: str,
    exchange: str,
    order_type: str,  # "maker", "ioc", "taker"
    outcome: str,  # "filled", "partial", "cancelled", "failed"
) -> None:
    """
    Record a close order in metrics.

    Args:
        symbol: Trading symbol
        exchange: Exchange name
        order_type: Type of order
        outcome: Order outcome
    """
    close_orders_total.labels(
        symbol=symbol, exchange=exchange, order_type=order_type, outcome=outcome
    ).inc()


def record_close_fees(
    symbol: str,
    exchange: str,
    fees_usd: float | Decimal,
) -> None:
    """
    Record close operation fees in metrics.

    Args:
        symbol: Trading symbol
        exchange: Exchange name
        fees_usd: Fees paid in USD
    """
    fees_float = float(fees_usd) if isinstance(fees_usd, Decimal) else fees_usd
    close_fees_usd.labels(symbol=symbol, exchange=exchange).inc(fees_float)


def update_active_positions(symbol: str, count: int) -> None:
    """
    Update the active positions gauge.

    Args:
        symbol: Trading symbol
        count: Current number of active positions
    """
    active_positions.labels(symbol=symbol).set(count)


@contextmanager
def track_close_duration(
    symbol: str, reason: str
) -> Generator[dict[str, Any], None, None]:
    """
    Context manager to track close operation duration.

    Usage:
        with track_close_duration("ETH", "ECON_EXIT") as ctx:
            # perform close operation
            ctx["success"] = True
            ctx["pnl_usd"] = 15.50

    Args:
        symbol: Trading symbol
        reason: Close reason

    Yields:
        Dict to store operation results (success, pnl_usd)
    """
    start_time = time.time()
    ctx: dict[str, Any] = {"success": False, "pnl_usd": 0}

    try:
        yield ctx
    finally:
        duration = time.time() - start_time
        record_close_operation(
            symbol=symbol,
            reason=reason,
            success=ctx.get("success", False),
            duration_seconds=duration,
            pnl_usd=ctx.get("pnl_usd", 0),
        )


# =============================================================================
# Execution Helper Functions
# =============================================================================


def record_execution(
    symbol: str,
    outcome: str,  # "success", "leg1_failed", "leg2_failed", "rollback"
    duration_seconds: float,
    notional_usd: float | Decimal = 0,
) -> None:
    """
    Record a trade execution in metrics.

    Args:
        symbol: Trading symbol (e.g., "ETH")
        outcome: Execution outcome
        duration_seconds: Time taken to complete execution
        notional_usd: Trade notional value in USD
    """
    execution_total.labels(symbol=symbol, outcome=outcome).inc()
    execution_duration_seconds.labels(symbol=symbol, outcome=outcome).observe(
        duration_seconds
    )

    if notional_usd:
        notional_float = (
            float(notional_usd) if isinstance(notional_usd, Decimal) else notional_usd
        )
        trade_notional_usd.labels(symbol=symbol).observe(notional_float)


def record_leg1_order(
    symbol: str,
    outcome: str,  # "filled", "partial", "cancelled", "timeout"
    fill_rate: float = 0,
) -> None:
    """
    Record a Leg1 (maker) order in metrics.

    Args:
        symbol: Trading symbol
        outcome: Order outcome
        fill_rate: Fill rate (0-1)
    """
    leg1_orders_total.labels(symbol=symbol, outcome=outcome).inc()
    if fill_rate > 0:
        leg1_fill_rate.labels(symbol=symbol).set(fill_rate)


def record_leg2_order(
    symbol: str,
    outcome: str,  # "filled", "partial", "failed"
    fill_rate: float = 0,
) -> None:
    """
    Record a Leg2 (hedge) order in metrics.

    Args:
        symbol: Trading symbol
        outcome: Order outcome
        fill_rate: Fill rate (0-1)
    """
    leg2_orders_total.labels(symbol=symbol, outcome=outcome).inc()
    if fill_rate > 0:
        leg2_fill_rate.labels(symbol=symbol).set(fill_rate)


def record_hedge_latency(symbol: str, latency_seconds: float) -> None:
    """
    Record hedge latency (Leg1 fill to Leg2 submission).

    Args:
        symbol: Trading symbol
        latency_seconds: Latency in seconds
    """
    hedge_latency_seconds.labels(symbol=symbol).observe(latency_seconds)


def record_rollback(symbol: str, success: bool) -> None:
    """
    Record a rollback operation in metrics.

    Args:
        symbol: Trading symbol
        success: Whether rollback was successful
    """
    rollback_total.labels(symbol=symbol, success=str(success).lower()).inc()


def update_active_executions(symbol: str, count: int) -> None:
    """
    Update the active executions gauge.

    Args:
        symbol: Trading symbol
        count: Current number of active executions
    """
    active_executions.labels(symbol=symbol).set(count)


@contextmanager
def track_execution_duration(
    symbol: str,
) -> Generator[dict[str, Any], None, None]:
    """
    Context manager to track execution duration.

    Usage:
        with track_execution_duration("ETH") as ctx:
            # perform execution
            ctx["outcome"] = "success"
            ctx["notional_usd"] = 5000

    Args:
        symbol: Trading symbol

    Yields:
        Dict to store execution results (outcome, notional_usd)
    """
    start_time = time.time()
    ctx: dict[str, Any] = {"outcome": "leg1_failed", "notional_usd": 0}

    try:
        yield ctx
    finally:
        duration = time.time() - start_time
        record_execution(
            symbol=symbol,
            outcome=ctx.get("outcome", "leg1_failed"),
            duration_seconds=duration,
            notional_usd=ctx.get("notional_usd", 0),
        )
