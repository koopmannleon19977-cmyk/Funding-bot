from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Decision(Enum):
    TAKE = "take"
    REJECT = "reject"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Price:
    value: Decimal

    def __init__(self, value):
        val = Decimal(str(value))
        if val <= 0:
            raise ValueError("Price must be positive")
        object.__setattr__(self, "value", val)

    def __float__(self) -> float:
        return float(self.value)

    def __add__(self, other):
        return Price(self.value + Decimal(str(other)))

    def __sub__(self, other):
        return Price(self.value - Decimal(str(other)))


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    total: Decimal
    reason: str | None = None


@dataclass(frozen=True)
class SizeResult:
    notional: Decimal
    reason: str | None = None


@dataclass(frozen=True)
class RiskResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class InvariantResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class MaintenanceDecision:
    keep_open: bool
    reason: str | None = None


@dataclass(frozen=True)
class FundingEvent:
    timestamp: datetime
    amount: Decimal
