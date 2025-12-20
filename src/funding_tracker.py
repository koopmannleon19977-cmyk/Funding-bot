# Compatibility shim - This file has been moved to application/services/
# Import from new location for better organization
from src.application.services.funding_tracker import (
    FundingTracker,
    safe_float,
)

__all__ = ['FundingTracker', 'safe_float']
