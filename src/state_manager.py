# Compatibility shim - This file has been moved to infrastructure/persistence/
# Import from new location for better organization
from src.infrastructure.persistence.state_manager import (
    InMemoryStateManager,
    TradeState,
    TradeStatus,
    get_state_manager,
    close_state_manager,
)

__all__ = [
    'InMemoryStateManager',
    'TradeState',
    'TradeStatus',
    'get_state_manager',
    'close_state_manager',
]
