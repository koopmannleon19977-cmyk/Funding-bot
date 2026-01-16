"""
Sizing helpers for opportunity scanning.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def _get_available_equity_for_sizing(self) -> Decimal:
    """
    Fetch available equity used for opportunity sizing caps.

    Important: This value must be used consistently in both:
    - `scan()` (candidate generation)
    - `get_best_opportunity()` (fresh re-validation)
    Otherwise we can "validate" a small affordable candidate but then re-evaluate it
    as a much larger notional and trigger unnecessary execution attempts.
    """
    available_equity = Decimal("0")
    try:
        # We use Lighter's available balance as the primary constraint (cross-margin baseline).
        if hasattr(self.market_data.lighter, "get_available_balance"):
            balance = await self.market_data.lighter.get_available_balance()
            available_equity = balance.available if hasattr(balance, "available") else Decimal("0")
            logger.debug(f"Fetched available equity for sizing: ${available_equity:.2f}")
        else:
            # Fallback for mocks/tests
            available_equity = Decimal("1000")
    except Exception as e:
        logger.warning(f"Failed to fetch available balance for sizing: {e}")
        available_equity = Decimal("0")
    return available_equity
