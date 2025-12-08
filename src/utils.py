import logging
from typing import Any, Union, Optional, Dict
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

logger = logging.getLogger(__name__)


# ============================================================================
# DECIMAL PRECISION: Financial calculations require Decimal, not float
# ============================================================================

# Standard precision for financial calculations (8 decimal places)
FINANCIAL_PRECISION = Decimal('0.00000001')
# Display precision for USD values (2 decimal places)
USD_PRECISION = Decimal('0.01')
# Precision for rates/percentages (6 decimal places)
RATE_PRECISION = Decimal('0.000001')


def safe_decimal(
    val: Any, 
    default: Decimal = Decimal('0'), 
    precision: Decimal = None,
    raise_on_error: bool = False
) -> Decimal:
    """
    Safely convert any value to Decimal for financial calculations.
    
    CRITICAL: Use this for ALL financial calculations to avoid float precision errors.
    Float: 0.1 + 0.2 = 0.30000000000000004
    Decimal: Decimal('0.1') + Decimal('0.2') = Decimal('0.3')
    
    Args:
        val: The value to convert (int, float, str, Decimal, None)
        default: Value to return if conversion fails (default: Decimal('0'))
        precision: Optional Decimal for quantization (e.g., Decimal('0.01') for cents)
        raise_on_error: If True, raise exception instead of returning default
    
    Returns:
        Decimal: The converted value, optionally quantized
    
    Example:
        >>> safe_decimal(0.1) + safe_decimal(0.2)
        Decimal('0.3')
        >>> safe_decimal('123.456', precision=USD_PRECISION)
        Decimal('123.46')
        >>> safe_decimal(None)
        Decimal('0')
    """
    if val is None:
        if raise_on_error:
            raise ValueError("safe_decimal received None")
        return default
    
    # Already a Decimal - just quantize if needed
    if isinstance(val, Decimal):
        result = val
    elif isinstance(val, (int, float)):
        # CRITICAL: Convert float to string first to preserve displayed value
        # float(0.1) is actually 0.1000000000000000055511151231257827021181583404541015625
        # str(0.1) is "0.1" which Decimal parses correctly
        try:
            result = Decimal(str(val))
        except InvalidOperation:
            if raise_on_error:
                raise ValueError(f"Cannot convert {val} to Decimal")
            return default
    elif isinstance(val, str):
        try:
            s_val = val.strip()
            if not s_val or s_val.lower() == 'none':
                if raise_on_error:
                    raise ValueError(f"safe_decimal received invalid string: '{val}'")
                return default
            result = Decimal(s_val)
        except InvalidOperation:
            if raise_on_error:
                raise ValueError(f"Cannot convert string '{val}' to Decimal")
            logger.warning(f"⚠️ safe_decimal failed for value '{val}' (type: {type(val)}). Returning default {default}")
            return default
    else:
        # Try string conversion as last resort
        try:
            result = Decimal(str(val))
        except (InvalidOperation, ValueError):
            if raise_on_error:
                raise ValueError(f"Cannot convert {type(val).__name__} '{val}' to Decimal")
            logger.warning(f"⚠️ safe_decimal failed for value '{val}' (type: {type(val)}). Returning default {default}")
            return default
    
    # Apply precision if specified
    if precision is not None:
        result = result.quantize(precision, rounding=ROUND_HALF_UP)
    
    return result


def decimal_to_float(val: Decimal) -> float:
    """
    Convert Decimal to float for APIs that require float.
    
    Use sparingly - only at API boundaries where float is required.
    Internal calculations should stay in Decimal.
    """
    return float(val)


def quantize_usd(val: Decimal) -> Decimal:
    """Quantize to 2 decimal places (cents) for USD display."""
    return val.quantize(USD_PRECISION, rounding=ROUND_HALF_UP)


def quantize_rate(val: Decimal) -> Decimal:
    """Quantize to 6 decimal places for rates/percentages."""
    return val.quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)


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

def safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to int."""
    if val is None:
        return default
    try:
        if isinstance(val, (int, float, Decimal)):
            return int(val)
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default
