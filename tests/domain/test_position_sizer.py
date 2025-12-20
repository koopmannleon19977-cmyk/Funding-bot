from decimal import Decimal

from src.domain.entities import Opportunity
from src.domain.rules import Constitution
from src.domain.services import PositionSizer
from src.domain.value_objects import Price


def test_sizes_with_balances_and_cap():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    sizer = PositionSizer(rules)
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.50"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    res = sizer.size(opp, max_notional=Decimal("100"), balance_lighter=Decimal("80"), balance_x10=Decimal("90"))
    assert res.notional == Decimal("80")
    assert res.reason is None


def test_rejects_low_apy():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    sizer = PositionSizer(rules)
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.10"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    res = sizer.size(opp, max_notional=Decimal("100"), balance_lighter=Decimal("80"), balance_x10=Decimal("90"))
    assert res.notional == Decimal("0")
    assert res.reason == "apy_below_min"


def test_rejects_wide_spread():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    sizer = PositionSizer(rules)
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.50"),
        spread=Decimal("0.005"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    res = sizer.size(opp, max_notional=Decimal("100"), balance_lighter=Decimal("80"), balance_x10=Decimal("90"))


def test_lot_size_rounds_down_and_rejects_zero():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    sizer = PositionSizer(rules)
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.50"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    # Notional would be 10, but lot size 1 BTC at 50k makes qty round to 0.
    res = sizer.size(
        opp,
        max_notional=Decimal("10"),
        balance_lighter=Decimal("10"),
        balance_x10=Decimal("10"),
        lot_size=Decimal("1"),
    )
    assert res.notional == Decimal("0")
    assert res.reason == "lot_rounding_zero"
