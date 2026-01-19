"""Observability: logging, metrics, alerts."""

from funding_bot.observability.logging import (
    LOG_TAG_HEALTH,
    LOG_TAG_PROFIT,
    LOG_TAG_SCAN,
    LOG_TAG_TRADE,
    get_logger,
    setup_logging,
)
from funding_bot.observability.metrics import (
    record_close_operation,
    record_coordinated_close,
    record_execution,
    record_hedge_latency,
    record_leg1_order,
    record_leg2_order,
    record_rebalance_operation,
    record_rollback,
    track_close_duration,
    track_execution_duration,
    update_active_executions,
    update_active_positions,
)

__all__ = [
    # Logging
    "setup_logging",
    "get_logger",
    "LOG_TAG_TRADE",
    "LOG_TAG_PROFIT",
    "LOG_TAG_HEALTH",
    "LOG_TAG_SCAN",
    # Metrics helpers
    "record_close_operation",
    "record_rebalance_operation",
    "record_coordinated_close",
    "record_execution",
    "record_leg1_order",
    "record_leg2_order",
    "record_hedge_latency",
    "record_rollback",
    "track_close_duration",
    "track_execution_duration",
    "update_active_positions",
    "update_active_executions",
]
