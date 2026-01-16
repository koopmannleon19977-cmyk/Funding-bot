"""
Close operations module (refactored).

This module provides refactored, modular versions of the close operations.
The main close.py still contains the active implementation.

To migrate to these modules:
1. Import from close_modules instead of close.py
2. Update function signatures (self -> manager parameter)
3. Run tests to verify compatibility

Example:
    # Old (from close.py):
    from funding_bot.services.positions.close import _rebalance_trade

    # New (from close_modules):
    from funding_bot.services.positions.close_modules.rebalance import rebalance_trade
"""

from __future__ import annotations

# Re-export main types
from funding_bot.services.positions.close_modules.types import (
    CloseConfig,
    GridStepConfig,
    CloseResult as LighterCloseResult,
    CloseAttemptResult,
    GridStepCloseResult,
    TakerResult,
    CoordinatedCloseContext,
)

from funding_bot.services.positions.close_modules.logging import (
    log_final_execution_metrics,
    log_close_order_placed,
    log_rebalance_start,
    log_rebalance_order_placed,
    log_rebalance_complete,
    log_coordinated_close_start,
    log_coordinated_close_maker_placed,
    log_coordinated_close_ioc_escalate,
    log_coordinated_close_complete,
)

from funding_bot.services.positions.close_modules.rebalance import (
    calculate_rebalance_target,
    get_rebalance_price,
    execute_rebalance_maker,
    execute_rebalance_ioc,
    update_leg_after_fill,
    rebalance_trade,
)

from funding_bot.services.positions.close_modules.coordinated import (
    CoordinatedCloseContext as CoordinatedCtx,
    build_coordinated_close_context,
    calculate_leg_maker_price,
    calculate_ioc_price,
    submit_maker_order,
    submit_coordinated_maker_orders,
    wait_for_coordinated_fills,
    execute_ioc_close,
    escalate_unfilled_to_ioc,
    close_both_legs_coordinated,
)

__all__ = [
    # Types
    "CloseConfig",
    "GridStepConfig",
    "LighterCloseResult",
    "CloseAttemptResult",
    "GridStepCloseResult",
    "TakerResult",
    "CoordinatedCloseContext",
    "CoordinatedCtx",
    # Logging
    "log_final_execution_metrics",
    "log_close_order_placed",
    "log_rebalance_start",
    "log_rebalance_order_placed",
    "log_rebalance_complete",
    "log_coordinated_close_start",
    "log_coordinated_close_maker_placed",
    "log_coordinated_close_ioc_escalate",
    "log_coordinated_close_complete",
    # Rebalance
    "calculate_rebalance_target",
    "get_rebalance_price",
    "execute_rebalance_maker",
    "execute_rebalance_ioc",
    "update_leg_after_fill",
    "rebalance_trade",
    # Coordinated
    "build_coordinated_close_context",
    "calculate_leg_maker_price",
    "calculate_ioc_price",
    "submit_maker_order",
    "submit_coordinated_maker_orders",
    "wait_for_coordinated_fills",
    "execute_ioc_close",
    "escalate_unfilled_to_ioc",
    "close_both_legs_coordinated",
]
