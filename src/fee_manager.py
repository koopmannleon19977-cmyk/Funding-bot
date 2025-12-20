# Compatibility shim - This file has been moved to domain/services/
# Import from new location for better organization
from src.domain.services.fee_manager import (
    FeeManager,
    FeeSchedule,
    get_fee_manager,
    init_fee_manager,
    stop_fee_manager,
)

__all__ = [
    'FeeManager',
    'FeeSchedule',
    'get_fee_manager',
    'init_fee_manager',
    'stop_fee_manager',
]
