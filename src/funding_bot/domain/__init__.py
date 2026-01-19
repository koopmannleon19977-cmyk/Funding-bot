"""
Domain Layer: Core business entities, value objects, and rules.

This layer has NO external dependencies (no SDK types, no DB types).
All types here are canonical and used throughout the application.
"""

from funding_bot.domain.errors import (
    DomainError,
    ExecutionError,
    InsufficientBalanceError,
    OrderRejectedError,
    ReconciliationError,
    RollbackError,
    ValidationError,
)
from funding_bot.domain.events import (
    DomainEvent,
    FundingCollected,
    LegFilled,
    RollbackInitiated,
    TradeClosed,
    TradeOpened,
    TradeStateChanged,
)
from funding_bot.domain.models import (
    Balance,
    Exchange,
    ExecutionState,
    FundingRate,
    MarketInfo,
    Opportunity,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
    TradeLeg,
    TradeStatus,
)

__all__ = [
    # Enums
    "Exchange",
    "Side",
    "OrderType",
    "TimeInForce",
    "OrderStatus",
    "TradeStatus",
    "ExecutionState",
    # Models
    "Position",
    "Order",
    "OrderRequest",
    "Trade",
    "TradeLeg",
    "Opportunity",
    "FundingRate",
    "MarketInfo",
    "Balance",
    # Events
    "DomainEvent",
    "TradeOpened",
    "TradeClosed",
    "TradeStateChanged",
    "LegFilled",
    "RollbackInitiated",
    "FundingCollected",
    # Errors
    "DomainError",
    "ValidationError",
    "InsufficientBalanceError",
    "OrderRejectedError",
    "ExecutionError",
    "RollbackError",
    "ReconciliationError",
]
