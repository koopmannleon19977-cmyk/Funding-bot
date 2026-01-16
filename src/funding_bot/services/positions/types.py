"""
Shared types for position management.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class PositionsStillOpenError(RuntimeError):
    """Raised when a trade close was attempted but positions are still open."""


@dataclass
class CloseResult:
    """Result of closing a trade."""

    success: bool
    realized_pnl: Decimal = Decimal("0")
    reason: str = ""
    error: str | None = None
