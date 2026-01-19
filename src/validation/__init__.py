from decimal import Decimal

from src.core.orderbook_validator import (
    OrderbookDepthLevel,
    OrderbookQuality,
    OrderbookValidationResult,
    OrderbookValidator,
)
from src.utils import safe_decimal


def validate_orderbook_for_maker(
    symbol: str,
    side: str,
    trade_size_usd: Decimal,
    bids,
    asks,
    **kwargs,
) -> OrderbookValidationResult:
    """Convenience function used by legacy callers/tests."""
    validator = OrderbookValidator(**kwargs)
    return validator.validate_for_maker_order(
        symbol=symbol,
        side=side,
        trade_size_usd=safe_decimal(trade_size_usd),
        bids=bids,
        asks=asks,
        orderbook_timestamp=kwargs.get("orderbook_timestamp"),
    )


__all__ = [
    "OrderbookValidator",
    "OrderbookValidationResult",
    "OrderbookQuality",
    "OrderbookDepthLevel",
    "validate_orderbook_for_maker",
]
