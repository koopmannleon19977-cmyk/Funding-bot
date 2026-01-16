# Close Operations Module

This module provides refactored components for position closing operations.

## Module Structure

```
close/
├── __init__.py      # Public exports
├── types.py         # Dataclasses: CloseConfig, GridStepConfig, CloseResult, etc.
├── logging.py       # Structured logging helpers for close events
├── rebalance.py     # Delta neutrality restoration (reduce-only operations)
├── coordinated.py   # Dual-leg coordinated close (parallel maker → IOC escalate)
└── README.md        # This file
```

## Migration Status

These modules contain **refactored versions** of functions from `close.py`.
The original `close.py` (3,169 lines) remains the active implementation.

### To Complete Migration

1. Update `close.py` imports to use these modules
2. Replace inline functions with module imports
3. Run full test suite to verify compatibility
4. Remove duplicated code from `close.py`

## Usage

```python
# New modular imports (future)
from funding_bot.services.positions.close.logging import log_final_execution_metrics
from funding_bot.services.positions.close.rebalance import rebalance_trade
from funding_bot.services.positions.close.coordinated import close_both_legs_coordinated

# Current usage (backward compatible)
from funding_bot.services.positions.close import _close_impl  # Still in close.py
```

## Module Details

### types.py
- `CloseConfig`: Configuration for Lighter close operations
- `GridStepConfig`: Configuration for grid step close
- `CloseResult`: Result of close attempt
- `CloseAttemptResult`: Single attempt result
- `GridStepCloseResult`: Grid step result
- `TakerResult`: Taker fallback result
- `CoordinatedCloseContext`: Context for coordinated close

### logging.py
- `log_final_execution_metrics()`: Final trade metrics after close
- `log_close_order_placed()`: Order placement events
- `log_rebalance_*()`: Rebalance operation events
- `log_coordinated_close_*()`: Coordinated close events

### rebalance.py
- `calculate_rebalance_target()`: Determine which leg to rebalance
- `get_rebalance_price()`: Get appropriate rebalance price
- `execute_rebalance_maker()`: Execute maker order with timeout
- `execute_rebalance_ioc()`: IOC fallback for rebalance
- `update_leg_after_fill()`: Update leg state after fill
- `rebalance_trade()`: Main orchestrator

### coordinated.py
- `build_coordinated_close_context()`: Build operation context
- `calculate_leg_maker_price()`: Calculate maker price for leg
- `calculate_ioc_price()`: Calculate IOC (cross spread) price
- `submit_maker_order()`: Submit POST_ONLY maker order
- `submit_coordinated_maker_orders()`: Submit both legs in parallel
- `wait_for_coordinated_fills()`: Wait for fills with timeout
- `execute_ioc_close()`: Execute IOC close order
- `escalate_unfilled_to_ioc()`: Escalate unfilled legs to IOC
- `close_both_legs_coordinated()`: Main orchestrator
