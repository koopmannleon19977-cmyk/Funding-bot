# src/utils/__init__.py
"""
Utility modules for the funding bot.

Re-exports all functions from the original utils.py module 
and the new async_utils submodule.
"""

# Import everything from the original utils.py (now accessed as a sibling)
# Since we're inside a utils/ package, we need to import from parent and back
import sys
import os

# Get the parent directory's utils.py
_parent_dir = os.path.dirname(os.path.dirname(__file__))
_utils_py_path = os.path.join(_parent_dir, 'utils.py')

# Use importlib to load the original utils.py as a separate module
import importlib.util
_spec = importlib.util.spec_from_file_location("_original_utils", _utils_py_path)
_original_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_original_utils)

# Re-export everything from the original utils.py
safe_float = _original_utils.safe_float
safe_int = _original_utils.safe_int
safe_decimal = _original_utils.safe_decimal
decimal_to_float = _original_utils.decimal_to_float
quantize_usd = _original_utils.quantize_usd
quantize_rate = _original_utils.quantize_rate
quantize_value = _original_utils.quantize_value
mask_sensitive_data = _original_utils.mask_sensitive_data
FINANCIAL_PRECISION = _original_utils.FINANCIAL_PRECISION
USD_PRECISION = _original_utils.USD_PRECISION
RATE_PRECISION = _original_utils.RATE_PRECISION
SENSITIVE_KEYWORDS = _original_utils.SENSITIVE_KEYWORDS

# Import from the new async_utils submodule
from .async_utils import (
    safe_gather,
    cancel_task_safely,
    retrieve_task_exception,
    wait_for_tasks_with_cleanup,
)

__all__ = [
    # From original utils.py
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
