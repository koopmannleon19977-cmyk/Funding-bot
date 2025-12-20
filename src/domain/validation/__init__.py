"""
Domain validation layer.

This package contains domain-level validation logic:
- Orderbook validation
- Trade validation
- Data validation
"""

from .orderbook_validator import (
    OrderbookValidator,
    OrderbookValidationResult,
    OrderbookQuality,
    ExchangeProfile,
    get_orderbook_validator,
)

__all__ = [
    'OrderbookValidator',
    'OrderbookValidationResult',
    'OrderbookQuality',
    'ExchangeProfile',
    'get_orderbook_validator',
]
