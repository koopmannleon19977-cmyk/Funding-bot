import logging
from typing import Any, Union, Optional, Dict

logger = logging.getLogger(__name__)


# ============================================================================
# SECURITY: Sensitive Data Masking for Logging
# ============================================================================

# Keywords that indicate sensitive data (case-insensitive)
SENSITIVE_KEYWORDS = ('key', 'secret', 'token', 'password', 'api_key', 'apikey', 'private', 'credential')


def mask_sensitive_data(data: Any, mask_value: str = "***MASKED***") -> Any:
    """
    Recursively mask sensitive data (API keys, secrets, tokens) for safe logging.
    
    Args:
        data: The data to mask (dict, list, or other).
        mask_value: The string to replace sensitive values with.
    
    Returns:
        A copy of the data with sensitive values masked.
    
    Example:
        >>> mask_sensitive_data({"X-Api-Key": "secret123", "name": "test"})
        {"X-Api-Key": "***MASKED***", "name": "test"}
    """
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            # Check if key contains any sensitive keyword
            key_lower = str(key).lower().replace('-', '_').replace(' ', '_')
            is_sensitive = any(kw in key_lower for kw in SENSITIVE_KEYWORDS)
            
            if is_sensitive and value is not None:
                # Mask the value
                masked[key] = mask_value
            elif isinstance(value, (dict, list)):
                # Recurse into nested structures
                masked[key] = mask_sensitive_data(value, mask_value)
            else:
                masked[key] = value
        return masked
    
    elif isinstance(data, list):
        return [mask_sensitive_data(item, mask_value) for item in data]
    
    else:
        # Return non-container types as-is
        return data

def safe_float(val: Any, default: float = 0.0, raise_on_error: bool = False) -> float:
    """
    Safely convert a value to float.
    
    Args:
        val: The value to convert.
        default: The value to return if conversion fails (default 0.0).
        raise_on_error: If True, raise exception instead of returning default.

    Returns:
        float: The converted value or default.
    """
    if val is None:
        if raise_on_error:
             raise ValueError("safe_float received None")
        return default

    if isinstance(val, (int, float)):
        return float(val)

    try:
        # Handle string "None" or empty string explicitly if needed, 
        # but float() handles simple strings. 
        # 'None' string casts to error in float(), so caught below.
        s_val = str(val).strip()
        if not s_val or s_val.lower() == "none":
             if raise_on_error:
                 raise ValueError(f"safe_float received invalid string: '{val}'")
             return default
             
        return float(s_val)
    except (ValueError, TypeError) as e:
        if raise_on_error:
            raise e
        logger.warning(f"⚠️ safe_float failed for value '{val}' (type: {type(val)}). Returning default {default}")
        return default
