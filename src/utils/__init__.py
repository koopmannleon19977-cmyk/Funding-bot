# src/utils/__init__.py
"""
Utility modules for the funding bot.

This package consolidates all utility functions:
- helpers.py: Core helper functions (safe_float, safe_decimal, etc.)
- async_utils.py: Async utilities for graceful shutdown
"""

# Import from helpers (formerly utils.py)
from .helpers import (
    # Decimal utilities
    safe_decimal,
    decimal_to_float,
    quantize_usd,
    quantize_rate,
    quantize_value,
    FINANCIAL_PRECISION,
    USD_PRECISION,
    RATE_PRECISION,
    # Safe conversions
    safe_float,
    safe_int,
    # Security
    mask_sensitive_data,
    SENSITIVE_KEYWORDS,
)

# Import from async_utils
from .async_utils import (
    safe_gather,
    cancel_task_safely,
    retrieve_task_exception,
    wait_for_tasks_with_cleanup,
)

__all__ = [
    # From helpers
    'safe_float',
    'safe_int',
    'safe_decimal',
    'decimal_to_float',
    'quantize_usd',
    'quantize_rate',
    'quantize_value',
    'mask_sensitive_data',
    'FINANCIAL_PRECISION',
    'USD_PRECISION',
    'RATE_PRECISION',
    'SENSITIVE_KEYWORDS',
    # From async_utils
    'safe_gather',
    'cancel_task_safely',
    'retrieve_task_exception',
    'wait_for_tasks_with_cleanup',
]
