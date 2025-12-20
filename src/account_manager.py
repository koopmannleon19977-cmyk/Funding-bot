# Compatibility shim - This file has been moved to application/services/
# Import from new location for better organization
from src.application.services.account_manager import (
    AccountManager,
    get_account_manager,
)

__all__ = ['AccountManager', 'get_account_manager']
