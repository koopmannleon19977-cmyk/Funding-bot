# Compatibility shim - This file has been moved to domain/services/
# Import from new location for better organization
from src.domain.services.adaptive_threshold import (
    AdaptiveThresholdManager,
    get_threshold_manager,
)

__all__ = [
    'AdaptiveThresholdManager',
    'get_threshold_manager',
]
