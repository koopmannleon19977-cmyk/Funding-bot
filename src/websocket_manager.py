# Compatibility shim - This file has been moved to infrastructure/messaging/
# Import from new location for better organization
from src.infrastructure.messaging.websocket_manager import (
    WebSocketManager,
    WSConfig,
    WSState,
    WSMetrics,
    ManagedWebSocket,
    get_websocket_manager,
    init_websocket_manager,
)

# Backward compatibility alias
WSConnectionState = WSState

__all__ = [
    'WebSocketManager',
    'WSConfig',
    'WSState',
    'WSConnectionState',  # Backward compatibility
    'WSMetrics',
    'ManagedWebSocket',
    'get_websocket_manager',
    'init_websocket_manager',
]
