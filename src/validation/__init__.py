# src/validation/__init__.py
"""
Validation module for trade execution safety checks.

This module provides:
- Orderbook validation for Maker orders
- Depth and spread checks
- Staleness detection
- Exchange-specific validation profiles
"""

from src.validation.orderbook_validator import (
    OrderbookValidator,
    OrderbookValidationResult,
    OrderbookQuality,
    OrderbookDepthLevel,
    ExchangeProfile,
    ValidationProfile,
    VALIDATION_PROFILES,
    get_orderbook_validator,
    validate_orderbook_for_maker,
)

__all__ = [
    "OrderbookValidator",
    "OrderbookValidationResult",
    "OrderbookQuality",
    "OrderbookDepthLevel",
    "ExchangeProfile",
    "ValidationProfile",
    "VALIDATION_PROFILES",
    "get_orderbook_validator",
    "validate_orderbook_for_maker",
]
