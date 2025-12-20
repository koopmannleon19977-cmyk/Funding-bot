"""
Application execution layer.

This package handles trade execution logic:
- Parallel execution of hedged trades
- Batch order management
- Order state machines
"""

from .parallel_execution import (
    ParallelExecutionManager,
    ExecutionState,
    TradeExecution,
    calculate_common_quantity,
)
from .batch_manager import LighterBatchManager

__all__ = [
    'ParallelExecutionManager',
    'ExecutionState',
    'TradeExecution',
    'calculate_common_quantity',
    'LighterBatchManager',
]
