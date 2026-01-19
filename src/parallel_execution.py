"""Compatibility wrapper exposing parallel execution utilities under a flat module path."""

from src.application.parallel_execution import (
    ExecutionState,
    ParallelExecutionManager,
    calculate_common_quantity,
)

__all__ = ["ParallelExecutionManager", "ExecutionState", "calculate_common_quantity"]
