"""
Unit tests for delta bound checking with mark price (CURRENT notional).
"""

from decimal import Decimal

from funding_bot.domain.models import Exchange, Side, Trade, TradeLeg
from funding_bot.domain.rules import check_delta_bound

import pytest

pytestmark = pytest.mark.unit


def _make_test_trade(leg1_qty: Decimal = Decimal("0.1"), leg2_qty: Decimal = Decimal("0.1")) -> Trade:
    """Create a test trade for delta bound tests."""
    leg1 = TradeLeg(
        exchange=Exchange.LIGHTER,
        side=Side.SELL,
        qty=leg1_qty,
        filled_qty=leg1_qty,
        entry_price=Decimal("50000"),
    )
    leg2 = TradeLeg(
        exchange=Exchange.X10,
        side=Side.BUY,
        qty=leg2_qty,
        filled_qty=leg2_qty,
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


def test_delta_bound_with_mark_price_uses_current_notional():
    """Test that delta bound uses CURRENT notional (mark price) not entry price."""
    trade = _make_test_trade()

    # Entry price was 50000, current mark price is 55000 (10% higher)
    # With mark price: leg1_notional = 0.1 * 55000 = 5500
    #                leg2_notional = 0.1 * 55000 = 5500
    # Delta should be balanced (both 5500)
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("55000"),
        leg2_mark_price=Decimal("55000"),
    )

    assert not result.passed
    assert "0.00%" in result.reason  # Perfect delta neutral


def test_delta_bound_uses_mark_price_when_provided():
    """Test that mark price is used when provided and valid."""
    trade = _make_test_trade()

    # Entry = 50000, but mark = 48000 (price dropped)
    # With mark: leg1 = 0.1 * 48000 = 4800, leg2 = 0.1 * 48000 = 4800
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("48000"),  # Current mark
        leg2_mark_price=Decimal("48000"),  # Current mark
    )

    assert not result.passed
    # Verify it used 48000 not 50000 in the reason message
    # The reason should contain the delta values calculated at mark price
    assert "48000" in result.reason or "0.00%" in result.reason


def test_delta_bound_fallback_to_entry_price_when_mark_missing():
    """Test that entry price is used when mark price is None or zero."""
    trade = _make_test_trade()

    # No mark prices provided - should fall back to entry price (50000)
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert not result.passed  # Should still work with entry price


def test_delta_bound_fallback_to_entry_price_when_mark_zero():
    """Test that entry price is used when mark price is 0."""
    trade = _make_test_trade()

    # Mark price is 0 - should fall back to entry price
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("0"),
        leg2_mark_price=Decimal("0"),
    )

    assert not result.passed  # Should still work with entry price fallback


def test_delta_bound_triggers_on_imbalance_with_mark_price():
    """Test that delta bound triggers on imbalance using current mark prices."""
    # Imbalanced quantities
    trade = _make_test_trade(
        leg1_qty=Decimal("0.10"),
        leg2_qty=Decimal("0.09"),  # 10% less on leg2
    )

    # At mark price 50000:
    # leg1_notional = 0.10 * 50000 = 5000
    # leg2_notional = 0.09 * 50000 = 4500
    # net_delta = | -5000 + 4500 | = 500
    # total_notional = 5000 + 4500 = 9500
    # delta_drift_pct = 500 / 9500 = 5.26% > 3%
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("50000"),
        leg2_mark_price=Decimal("50000"),
    )

    assert result.passed
    assert "5.26%" in result.reason  # Approximately 5.26% drift


def test_delta_bound_with_mixed_mark_and_entry():
    """Test behavior when only one leg has mark price."""
    trade = _make_test_trade(
        leg1_qty=Decimal("0.10"),
        leg2_qty=Decimal("0.10"),
    )

    # Only leg1 has mark price, leg2 uses entry
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("55000"),  # Current mark
        leg2_mark_price=None,  # Falls back to entry (50000)
    )

    # With different prices: leg1 = 0.1 * 55000 = 5500, leg2 = 0.1 * 50000 = 5000
    # net_delta = | -5500 + 5000 | = 500
    # total_notional = 5500 + 5000 = 10500
    # delta_drift_pct = 500 / 10500 = 4.76% > 3%
    assert result.passed  # Should detect imbalance from price difference


def test_delta_bound_handles_price_volatility():
    """Test that delta bound correctly detects imbalance from price volatility."""
    trade = _make_test_trade(
        leg1_qty=Decimal("0.10"),
        leg2_qty=Decimal("0.10"),
    )

    # Lighter price moved up 5%, X10 price stayed same
    # This creates a temporary delta imbalance
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("52500"),  # Up 5%
        leg2_mark_price=Decimal("50000"),  # Unchanged
    )

    # leg1 = 0.1 * 52500 = 5250, leg2 = 0.1 * 50000 = 5000
    # net_delta = | -5250 + 5000 | = 250
    # total_notional = 5250 + 5000 = 10250
    # delta_drift_pct = 250 / 10250 = 2.44% < 3%
    assert not result.passed  # Should be within threshold


def test_delta_bound_formatting_includes_prices():
    """Test that the result message includes the prices used."""
    trade = _make_test_trade()

    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("52000"),
        leg2_mark_price=Decimal("51000"),
    )

    # The reason shows delta values calculated with those prices
    # 52000 * 0.1 = 5200, 51000 * 0.1 = 5100
    assert ("5200" in result.reason or "5100" in result.reason)


def test_delta_bound_zero_qty():
    """Test that delta bound handles zero quantity gracefully."""
    trade = _make_test_trade(
        leg1_qty=Decimal("0"),
        leg2_qty=Decimal("0.1"),
    )

    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("50000"),
        leg2_mark_price=Decimal("50000"),
    )

    assert not result.passed
    assert "Zero notional" in result.reason


def test_delta_bound_verbose_reason_with_mark():
    """Test that verbose reason includes mark prices when used."""
    trade = _make_test_trade()

    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("50000"),
        leg2_mark_price=Decimal("50000"),
    )

    # The reason shows delta values calculated at mark price (50000 * 0.1 = 5000)
    assert "5000" in result.reason


def test_delta_bound_imbalance_threshold_exact():
    """Test boundary case where imbalance equals threshold."""
    trade = _make_test_trade(
        leg1_qty=Decimal("0.10"),
        leg2_qty=Decimal("0.094"),  # Creates ~3% imbalance at 50000
    )

    # leg1 = 0.10 * 50000 = 5000
    # leg2 = 0.094 * 50000 = 4700
    # net_delta = | -5000 + 4700 | = 300
    # total_notional = 5000 + 4700 = 9700
    # delta_drift_pct = 300 / 9700 = 3.09% > 3%
    result = check_delta_bound(
        trade,
        max_delta_pct=Decimal("0.03"),
        leg1_mark_price=Decimal("50000"),
        leg2_mark_price=Decimal("50000"),
    )

    assert result.passed  # Should trigger at 3.09%
