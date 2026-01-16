"""
Shared types for position management.

Contains exceptions and result types used by position management operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


# =============================================================================
# Position Management Exceptions
# =============================================================================


class PositionError(Exception):
    """Base class for position management errors."""

    error_code: str = "POSITION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        symbol: str | None = None,
        trade_id: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.symbol = symbol
        self.trade_id = trade_id
        self.details = details or {}


class PositionsStillOpenError(PositionError):
    """Raised when a trade close was attempted but positions are still open."""

    error_code = "POSITIONS_STILL_OPEN"


class CloseTimeoutError(PositionError):
    """Close operation timed out."""

    error_code = "CLOSE_TIMEOUT"


class CloseOrderFailedError(PositionError):
    """Failed to place or fill close order."""

    error_code = "CLOSE_ORDER_FAILED"


class RebalanceError(PositionError):
    """Error during rebalance operation."""

    error_code = "REBALANCE_ERROR"


class CoordinatedCloseError(PositionError):
    """Error during coordinated close operation."""

    error_code = "COORDINATED_CLOSE_ERROR"


class ReadbackError(PositionError):
    """Error during post-close readback."""

    error_code = "READBACK_ERROR"


class PriceDataError(PositionError):
    """Missing or invalid price data for close operation."""

    error_code = "PRICE_DATA_ERROR"


# =============================================================================
# Result Types
# =============================================================================


@dataclass
class CloseResult:
    """Result of closing a trade."""

    success: bool
    realized_pnl: Decimal = Decimal("0")
    reason: str = ""
    error: str | None = None
