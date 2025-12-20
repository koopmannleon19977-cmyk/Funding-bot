# Compatibility shim - This file has been moved to domain/services/
# Import from new location for better organization
from src.domain.services.volatility_monitor import (
    VolatilityMonitor,
    get_volatility_monitor,
)

__all__ = ['VolatilityMonitor', 'get_volatility_monitor']
