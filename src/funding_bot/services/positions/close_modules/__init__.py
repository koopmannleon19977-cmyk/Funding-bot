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

from funding_bot.services.positions.close_modules.coordinated import (
    CoordinatedCloseContext as CoordinatedCtx,
)
from funding_bot.services.positions.close_modules.coordinated import (
    build_coordinated_close_context,
    calculate_ioc_price,
    calculate_leg_maker_price,
    close_both_legs_coordinated,
    escalate_unfilled_to_ioc,
    execute_ioc_close,
    submit_coordinated_maker_orders,
    submit_maker_order,
    wait_for_coordinated_fills,
)
from funding_bot.services.positions.close_modules.logging import (
    log_close_order_placed,
    log_coordinated_close_complete,
    log_coordinated_close_ioc_escalate,
    log_coordinated_close_maker_placed,
    log_coordinated_close_start,
    log_final_execution_metrics,
    log_rebalance_complete,
    log_rebalance_order_placed,
    log_rebalance_start,
)
from funding_bot.services.positions.close_modules.rebalance import (
    calculate_rebalance_target,
    execute_rebalance_ioc,
    execute_rebalance_maker,
    get_rebalance_price,
    rebalance_trade,
    update_leg_after_fill,
)

# Re-export main types
from funding_bot.services.positions.close_modules.types import (
    CloseAttemptResult,
    CloseConfig,
    CoordinatedCloseContext,
    GridStepCloseResult,
    GridStepConfig,
    TakerResult,
)
from funding_bot.services.positions.close_modules.types import (
    CloseResult as LighterCloseResult,
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
