import logging
from typing import Any, Union, Optional

logger = logging.getLogger(__name__)

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
