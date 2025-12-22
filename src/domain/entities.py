from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from .value_objects import Price, Side, TradeStatus


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    expected_apy: Decimal
    spread: Decimal
    liquidity: Decimal
    bid: Price
    ask: Price


@dataclass(frozen=True)
class Trade:
    id: str
    symbol: str
    status: TradeStatus
    leg1_exchange: str
    leg1_side: Side
    leg1_entry_price: Price
    leg1_quantity: Decimal
    leg2_exchange: str
    leg2_side: Side
    leg2_entry_price: Price
    leg2_quantity: Decimal
    entry_time: datetime
    expected_apy: Decimal
    close_time: datetime | None = None

    def mark_closed(self, close_time: datetime) -> "Trade":
        return replace(self, status=TradeStatus.CLOSED, close_time=close_time)
