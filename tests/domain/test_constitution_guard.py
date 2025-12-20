from decimal import Decimal

from src.domain.entities import Opportunity
from src.domain.rules import Constitution
from src.domain.services import ConstitutionGuard
from src.domain.value_objects import Price


def make_opp(**overrides) -> Opportunity:
    base = {
        "symbol": "BTC-USD",
        "expected_apy": Decimal("0.50"),
        "spread": Decimal("0.001"),
        "liquidity": Decimal("10000"),
        "bid": Price("50000"),
        "ask": Price("50010"),
    }
    base.update(overrides)
    return Opportunity(**base)


def test_entry_rejects_low_apy():
    guard = ConstitutionGuard(Constitution())
    res = guard.check_entry(
        make_opp(expected_apy=Decimal("0.10")),
        desired_notional_usd=Decimal("100"),
        est_fees_usd=Decimal("1"),
        hold_hours=Decimal("8"),
    )
    assert not res.ok
    assert res.reason == "apy_below_min"


def test_entry_rejects_spread_and_liquidity():
    guard = ConstitutionGuard(Constitution(max_spread_filter_percent=Decimal("0.002")))
    res = guard.check_entry(
        make_opp(spread=Decimal("0.01")),
        desired_notional_usd=Decimal("5000"),
        est_fees_usd=Decimal("1"),
        hold_hours=Decimal("4"),
    )
    assert not res.ok
    assert res.reason == "spread_too_wide"

    res2 = guard.check_entry(
        make_opp(),
        desired_notional_usd=Decimal("15000"),
        est_fees_usd=Decimal("1"),
        hold_hours=Decimal("4"),
    )
    assert not res2.ok
    assert res2.reason == "insufficient_liquidity"


def test_entry_rejects_slow_breakeven():
    guard = ConstitutionGuard(Constitution(max_breakeven_hours=Decimal("8")))
    res = guard.check_entry(
        make_opp(expected_apy=Decimal("0.35")),
        desired_notional_usd=Decimal("100"),
        est_fees_usd=Decimal("5"),
        hold_hours=Decimal("8"),
    )
    assert not res.ok
    assert res.reason == "breakeven_too_slow"


def test_entry_passes_when_profitable():
    guard = ConstitutionGuard(Constitution(min_profit_exit_usd=Decimal("0.10")))
    res = guard.check_entry(
        make_opp(expected_apy=Decimal("0.80")),
        desired_notional_usd=Decimal("10000"),
        est_fees_usd=Decimal("5"),
        hold_hours=Decimal("24"),
    )
    assert res.ok


def test_maintenance_flags_drop_flip_and_vol():
    guard = ConstitutionGuard(Constitution(min_maintenance_apy=Decimal("0.20"), funding_flip_hours_threshold=Decimal("4"), volatility_panic_threshold=Decimal("8")))

    low_apy = guard.check_maintenance(
        funding_apy=Decimal("0.10"),
        funding_flip_hours=Decimal("0"),
        volatility_pct_24h=Decimal("5"),
        age_hours=Decimal("1"),
    )
    assert not low_apy.ok and low_apy.reason == "apy_below_maintenance"

    flip = guard.check_maintenance(
        funding_apy=Decimal("0.30"),
        funding_flip_hours=Decimal("5"),
        volatility_pct_24h=Decimal("5"),
        age_hours=Decimal("1"),
    )
    assert not flip.ok and flip.reason == "funding_flip"

    vol = guard.check_maintenance(
        funding_apy=Decimal("0.30"),
        funding_flip_hours=Decimal("2"),
        volatility_pct_24h=Decimal("10"),
        age_hours=Decimal("1"),
    )
    assert not vol.ok and vol.reason == "volatility_panic"

    age = guard.check_maintenance(
        funding_apy=Decimal("0.30"),
        funding_flip_hours=Decimal("2"),
        volatility_pct_24h=Decimal("5"),
        age_hours=Decimal("80"),
    )
    assert not age.ok and age.reason == "max_hold_exceeded"


def test_maintenance_passes_when_safe():
    guard = ConstitutionGuard(Constitution())
    res = guard.check_maintenance(
        funding_apy=Decimal("0.40"),
        funding_flip_hours=Decimal("1"),
        volatility_pct_24h=Decimal("5"),
        age_hours=Decimal("10"),
    )
    assert res.ok
