from decimal import Decimal

import pytest

from src.domain.services import PnLCalculator
from src.domain.value_objects import Price


def test_unrealized_delta_neutral():
    # Long entry 100, current 110 => +10; Short entry 101, current 99 => +2; total +12 * qty
    pnl = PnLCalculator.unrealized(
        long_entry=Price("100"),
        short_entry=Price("101"),
        long_current=Price("110"),
        short_current=Price("99"),
        quantity=Decimal("2"),
    )
    assert pnl == Decimal("24")


def test_unrealized_requires_positive_qty():
    with pytest.raises(ValueError):
        PnLCalculator.unrealized(
            long_entry=Price("100"),
            short_entry=Price("101"),
            long_current=Price("110"),
            short_current=Price("99"),
            quantity=Decimal("0"),
        )


def test_realized():
    assert PnLCalculator.realized(Decimal("5"), Decimal("1.5")) == Decimal("3.5")
