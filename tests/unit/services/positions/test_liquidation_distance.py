"""
Unit tests for liquidation distance monitoring.
"""

from decimal import Decimal

import pytest

from funding_bot.domain.models import Exchange, Side, Trade, TradeLeg
from funding_bot.domain.rules import check_liquidation_distance

pytestmark = pytest.mark.unit


def _make_test_trade() -> Trade:
    """Create a test trade for liquidation distance tests."""
    leg1 = TradeLeg(
        exchange=Exchange.LIGHTER,
        side=Side.SELL,
        qty=Decimal("0.1"),
        filled_qty=Decimal("0.1"),
        entry_price=Decimal("50000"),
    )
    leg2 = TradeLeg(
        exchange=Exchange.X10,
        side=Side.BUY,
        qty=Decimal("0.1"),
        filled_qty=Decimal("0.1"),
        entry_price=Decimal("50000"),
    )
    return Trade(
        trade_id="test_001",
        symbol="BTC-PERP",
        leg1=leg1,
        leg2=leg2,
        target_qty=Decimal("0.1"),
        target_notional_usd=Decimal("5000"),
        entry_apy=Decimal("0.40"),
    )


def test_liquidation_distance_safe_both_legs():
    """Test that liquidation distance check passes when both legs are safe."""
    trade = _make_test_trade()

    # Both legs have 20% distance to liquidation
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),  # 10% threshold
        leg1_liq_distance_pct=Decimal("0.20"),  # 20% safe
        leg2_liq_distance_pct=Decimal("0.15"),  # 15% safe
    )

    assert not result.passed
    assert "15.0%" in result.reason  # Worst leg (15%)
    assert "safe" in result.reason.lower()


def test_liquidation_distance_triggers_one_leg():
    """Test that liquidation distance check triggers when one leg is too close."""
    trade = _make_test_trade()

    # Leg 1 is at 5% (too close), Leg 2 is at 15% (safe)
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),  # 10% threshold
        leg1_liq_distance_pct=Decimal("0.05"),  # 5% - TOO CLOSE
        leg2_liq_distance_pct=Decimal("0.15"),  # 15% safe
    )

    assert result.passed
    assert "5.0%" in result.reason
    assert "Liquidation risk" in result.reason


def test_liquidation_distance_triggers_both_legs():
    """Test that liquidation distance check triggers when both legs are too close."""
    trade = _make_test_trade()

    # Both legs are below threshold
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),  # 10% threshold
        leg1_liq_distance_pct=Decimal("0.08"),  # 8% - TOO CLOSE
        leg2_liq_distance_pct=Decimal("0.07"),  # 7% - TOO CLOSE
    )

    assert result.passed
    assert "7.0%" in result.reason  # Worst leg
    assert "leg1=8.0%" in result.reason
    assert "leg2=7.0%" in result.reason


def test_liquidation_distance_missing_data_defaults():
    """Test that liquidation distance check handles missing data gracefully."""
    trade = _make_test_trade()

    # Both legs at -1 = sentinel for missing liquidation_price
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("-1"),  # Missing (sentinel)
        leg2_liq_distance_pct=Decimal("-1"),  # Missing (sentinel)
    )

    assert not result.passed
    assert "missing" in result.reason.lower()
    assert "both legs" in result.reason.lower()
    assert "BTC-PERP" in result.reason  # Shows symbol


def test_liquidation_distance_one_leg_missing():
    """Test that liquidation distance check reports when one leg has missing data."""
    trade = _make_test_trade()

    # Leg 1 at -1 (missing), Leg 2 at unsafe 5%
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("-1"),  # Missing (sentinel)
        leg2_liq_distance_pct=Decimal("0.05"),  # 5% - BUT leg1 missing, so can't evaluate
    )

    assert not result.passed  # Should report missing data, not trigger on leg2
    assert "missing" in result.reason.lower()
    assert "lighter" in result.reason.lower()  # Reports which leg is missing
    assert "BTC-PERP" in result.reason  # Shows symbol


def test_liquidation_distance_custom_threshold():
    """Test that custom threshold works correctly."""
    trade = _make_test_trade()

    # Both legs at 12% distance, threshold is 15%
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.15"),  # 15% threshold (higher than default)
        leg1_liq_distance_pct=Decimal("0.12"),  # 12% - TOO CLOSE for 15% threshold
        leg2_liq_distance_pct=Decimal("0.12"),
    )

    assert result.passed
    assert "12.0%" in result.reason


def test_liquidation_distance_boundary_case():
    """Test boundary case where distance equals threshold."""
    trade = _make_test_trade()

    # Distance exactly equals threshold (10%)
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.10"),  # Exactly at threshold
        leg2_liq_distance_pct=Decimal("0.20"),
    )

    # Should NOT pass (only triggers when LESS than threshold)
    assert not result.passed
    assert "safe" in result.reason.lower()


def test_liquidation_distance_worst_case_leg():
    """Test that the worst-case leg is used for decision."""
    trade = _make_test_trade()

    # Leg 1: 25% safe, Leg 2: 8% unsafe -> should use leg2
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.25"),  # Very safe
        leg2_liq_distance_pct=Decimal("0.08"),  # Unsafe
    )

    assert result.passed
    assert "8.0%" in result.reason  # Worst leg


def test_liquidation_distance_formatting():
    """Test that the result message is properly formatted."""
    trade = _make_test_trade()

    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.05"),
        leg2_liq_distance_pct=Decimal("0.20"),
    )

    assert "leg1=5.0%" in result.reason
    assert "leg2=20.0%" in result.reason
    assert "10.0%" in result.reason  # Threshold


def test_liquidation_distance_x10_leg_missing():
    """Test that liquidation distance check reports when X10 leg has missing data."""
    trade = _make_test_trade()

    # Leg 1 at safe 20%, Leg 2 at -1 (missing)
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.20"),  # Safe
        leg2_liq_distance_pct=Decimal("-1"),  # Missing (sentinel)
    )

    assert not result.passed  # Should report missing data
    assert "missing" in result.reason.lower()
    assert "x10" in result.reason.lower()  # Reports which leg is missing
    assert "BTC-PERP" in result.reason  # Shows symbol


def test_liquidation_distance_triggered_leg_identified():
    """Test that the reason message identifies which leg triggered the exit."""
    trade = _make_test_trade()

    # Leg 1 triggers (5% < 10%), Leg 2 is safe (20%)
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.05"),  # Triggers
        leg2_liq_distance_pct=Decimal("0.20"),  # Safe
    )

    assert result.passed
    assert "Lighter" in result.reason  # Identifies the triggering leg
    assert "5.0%" in result.reason  # Shows the triggered distance


def test_liquidation_distance_x10_leg_triggers():
    """Test that X10 leg trigger is correctly identified."""
    trade = _make_test_trade()

    # Leg 1 is safe (20%), Leg 2 triggers (5% < 10%)
    result = check_liquidation_distance(
        trade,
        min_distance_pct=Decimal("0.10"),
        leg1_liq_distance_pct=Decimal("0.20"),  # Safe
        leg2_liq_distance_pct=Decimal("0.05"),  # Triggers
    )

    assert result.passed
    assert "X10" in result.reason  # Identifies the triggering leg
    assert "5.0%" in result.reason  # Shows the triggered distance
