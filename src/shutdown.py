# Compatibility shim - This file has been moved to application/lifecycle/
# Import from new location for better organization
from src.application.lifecycle.shutdown import (
    ShutdownOrchestrator,
    ShutdownResult,
    get_shutdown_orchestrator,
)

__all__ = ['ShutdownOrchestrator', 'ShutdownResult', 'get_shutdown_orchestrator']
