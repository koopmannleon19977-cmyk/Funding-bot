"""
Domain Error Taxonomy.

All domain-specific exceptions with clear categorization.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class DomainError(Exception):
    """
    Base class for all domain errors.

    Includes structured error info for logging and debugging.
    """

    error_code: str = "DOMAIN_ERROR"

    def __init__(
        self,
        message: str,
        *,
        symbol: str | None = None,
        exchange: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.symbol = symbol
        self.exchange = exchange
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for logging."""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "details": self.details,
        }


# =============================================================================
# Validation Errors
# =============================================================================


class ValidationError(DomainError):
    """Invalid input or state."""

    error_code = "VALIDATION_ERROR"


class InvalidSymbolError(ValidationError):
    """Symbol not found or blacklisted."""

    error_code = "INVALID_SYMBOL"


class InvalidQuantityError(ValidationError):
    """Quantity outside allowed bounds."""

    error_code = "INVALID_QUANTITY"


class InvalidPriceError(ValidationError):
    """Price outside allowed bounds."""

    error_code = "INVALID_PRICE"


# =============================================================================
# Balance & Risk Errors
# =============================================================================


class InsufficientBalanceError(DomainError):
    """Not enough balance for operation."""

    error_code = "INSUFFICIENT_BALANCE"

    def __init__(
        self,
        message: str,
        *,
        required: Decimal,
        available: Decimal,
        **kwargs: Any,
    ):
        super().__init__(message, **kwargs)
        self.required = required
        self.available = available
        self.details["required"] = str(required)
        self.details["available"] = str(available)


class ExposureLimitError(DomainError):
    """Would exceed exposure limits."""

    error_code = "EXPOSURE_LIMIT"


class MaxTradesReachedError(DomainError):
    """Maximum open trades reached."""

    error_code = "MAX_TRADES_REACHED"


# =============================================================================
# Order Errors
# =============================================================================


class OrderError(DomainError):
    """Base class for order-related errors."""

    error_code = "ORDER_ERROR"


class OrderRejectedError(OrderError):
    """Order rejected by exchange."""

    error_code = "ORDER_REJECTED"


class OrderTimeoutError(OrderError):
    """Order timed out (not filled within limit)."""

    error_code = "ORDER_TIMEOUT"


class OrderNotFoundError(OrderError):
    """Order not found on exchange."""

    error_code = "ORDER_NOT_FOUND"


class OrderCancelFailedError(OrderError):
    """Failed to cancel order."""

    error_code = "ORDER_CANCEL_FAILED"


# =============================================================================
# Execution Errors
# =============================================================================


class ExecutionError(DomainError):
    """Error during trade execution."""

    error_code = "EXECUTION_ERROR"


class Leg1FailedError(ExecutionError):
    """First leg (maker) failed."""

    error_code = "LEG1_FAILED"


class Leg2FailedError(ExecutionError):
    """Second leg (hedge) failed."""

    error_code = "LEG2_FAILED"


class Leg1HedgeEvaporatedError(ExecutionError):
    """Leg 1 canceled because hedge liquidity evaporated."""

    error_code = "LEG1_HEDGE_EVAPORATED"


class MicrofillError(ExecutionError):
    """Partial fill too small to hedge."""

    error_code = "MICROFILL"


class SizingError(ExecutionError):
    """Error calculating trade size."""

    error_code = "SIZING_ERROR"


class PreflightCheckError(ExecutionError):
    """Preflight checks failed before execution."""

    error_code = "PREFLIGHT_CHECK_ERROR"


class WaitForFillError(ExecutionError):
    """Timeout or error waiting for order fill."""

    error_code = "WAIT_FOR_FILL_ERROR"


class OrderbookDataError(ExecutionError):
    """Missing or invalid orderbook data."""

    error_code = "ORDERBOOK_DATA_ERROR"


# =============================================================================
# Rollback Errors
# =============================================================================


class RollbackError(DomainError):
    """Error during rollback."""

    error_code = "ROLLBACK_ERROR"


class RollbackFailedError(RollbackError):
    """Rollback could not complete."""

    error_code = "ROLLBACK_FAILED"


class UnhedgedExposureError(RollbackError):
    """Position left unhedged after rollback failure."""

    error_code = "UNHEDGED_EXPOSURE"


# =============================================================================
# Reconciliation Errors
# =============================================================================


class ReconciliationError(DomainError):
    """Error during position reconciliation."""

    error_code = "RECONCILIATION_ERROR"


class ZombiePositionError(ReconciliationError):
    """Position in DB but not on exchange."""

    error_code = "ZOMBIE_POSITION"


class GhostPositionError(ReconciliationError):
    """Position on exchange but not in DB."""

    error_code = "GHOST_POSITION"


# =============================================================================
# Exchange/API Errors
# =============================================================================


class ExchangeError(DomainError):
    """Error from exchange API."""

    error_code = "EXCHANGE_ERROR"


class RateLimitError(ExchangeError):
    """Rate limit exceeded."""

    error_code = "RATE_LIMIT"


class ConnectionError(ExchangeError):
    """Connection to exchange failed."""

    error_code = "CONNECTION_ERROR"


class WebSocketError(ExchangeError):
    """WebSocket connection error."""

    error_code = "WEBSOCKET_ERROR"

