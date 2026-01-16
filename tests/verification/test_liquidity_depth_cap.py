from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.models import Side
from funding_bot.services.liquidity_gates import calculate_l1_depth_cap
from funding_bot.services.market_data import OrderbookSnapshot


def test_calculate_l1_depth_cap_respects_utilization_and_multiple():
    ob = OrderbookSnapshot(
        symbol="ABC",
        lighter_bid=Decimal("100"),
        lighter_ask=Decimal("101"),
        x10_bid=Decimal("99"),
        x10_ask=Decimal("100"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("8"),
        x10_ask_qty=Decimal("8"),
    )

    # Lighter BUY uses lighter_ask_qty, X10 SELL uses x10_bid_qty.
    # Mid price for sizing = 100.
    cap = calculate_l1_depth_cap(
        ob,
        lighter_side=Side.BUY,
        x10_side=Side.SELL,
        mid_price=Decimal("100"),
        min_l1_notional_usd=Decimal("150"),
        min_l1_notional_multiple=Decimal("0.5"),
        max_l1_qty_utilization=Decimal("0.7"),
        safety_buffer=None,
    )

    # Utilization caps:
    # - Lighter: 10 * 0.7 * 100 = 700
    # - X10:     8 * 0.7 * 100 = 560  (binding)
    assert cap.passed is True
    assert cap.max_notional_usd == Decimal("560")
    assert cap.max_qty == Decimal("5.6")


def test_calculate_l1_depth_cap_fails_when_min_notional_usd_not_met():
    ob = OrderbookSnapshot(
        symbol="ABC",
        lighter_bid=Decimal("100"),
        lighter_ask=Decimal("101"),
        x10_bid=Decimal("1"),
        x10_ask=Decimal("2"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("50"),  # $50 notional on bid
        x10_ask_qty=Decimal("50"),
    )

    cap = calculate_l1_depth_cap(
        ob,
        lighter_side=Side.SELL,  # lighter_bid side
        x10_side=Side.BUY,       # x10_ask side ($100 notional) still below min below? depends on price; use bid here.
        mid_price=Decimal("100"),
        min_l1_notional_usd=Decimal("150"),
        min_l1_notional_multiple=Decimal("0"),
        max_l1_qty_utilization=Decimal("1.0"),
        safety_buffer=None,
    )

    assert cap.passed is False
    assert cap.max_notional_usd == Decimal("0")
