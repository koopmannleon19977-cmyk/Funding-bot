"""
Position management package (facade).
"""

from __future__ import annotations

from funding_bot.services.positions.manager import PositionManager
from funding_bot.services.positions.types import CloseResult, PositionsStillOpenError

__all__ = ["PositionManager", "CloseResult", "PositionsStillOpenError"]
