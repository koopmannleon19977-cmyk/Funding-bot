# Compatibility shim - This file has been moved to application/execution/
# Import from new location for better organization
from src.application.execution.batch_manager import (
    LighterBatchManager,
)

__all__ = ['LighterBatchManager']
