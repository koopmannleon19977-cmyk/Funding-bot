from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.models import Side
from funding_bot.services.liquidity_gates import (
    estimate_entry_spread_pct_by_impact,
    estimate_taker_vwap_price_by_impact,
)
from funding_bot.services.market_data import OrderbookDepthSnapshot


def test_estimate_taker_vwap_price_by_impact_happy_path():
    depth = OrderbookDepthSnapshot(
        symbol="ABC",
        x10_bids=[(Decimal("100.00"), Decimal("0.5")), (Decimal("99.95"), Decimal("50"))],
        x10_asks=[(Decimal("100.05"), Decimal("0.5")), (Decimal("100.10"), Decimal("50"))],
    )

    price, metrics = estimate_taker_vwap_price_by_impact(
        depth,
        exchange="X10",
        side=Side.BUY,
        target_qty=Decimal("5"),
        max_price_impact_pct=Decimal("0.0015"),
    )

    assert metrics["ok"] is True
    assert price is not None
    # 0.5 @ 100.05 + 4.5 @ 100.10 = 500.475 / 5 = 100.095
    assert price == Decimal("100.095")


def test_estimate_entry_spread_pct_by_impact_matches_vwap_prices():
    depth = OrderbookDepthSnapshot(
        symbol="ABC",
        lighter_bids=[(Decimal("100.00"), Decimal("0.5")), (Decimal("99.95"), Decimal("50"))],
        lighter_asks=[(Decimal("100.10"), Decimal("0.5")), (Decimal("100.15"), Decimal("50"))],
        x10_bids=[(Decimal("99.90"), Decimal("0.5")), (Decimal("99.85"), Decimal("50"))],
        x10_asks=[(Decimal("100.05"), Decimal("0.5")), (Decimal("100.10"), Decimal("50"))],
    )

    spread, metrics = estimate_entry_spread_pct_by_impact(
        depth,
        long_exchange="X10",
        target_qty=Decimal("5"),
        max_price_impact_pct=Decimal("0.0015"),
    )

    assert metrics["ok"] is True
    # buy(X10 asks) VWAP = 100.095 ; sell(Lighter bids) VWAP = 99.955
    buy = Decimal(metrics["buy_price"])
    sell = Decimal(metrics["sell_price"])
    assert buy == Decimal("100.095")
    assert sell == Decimal("99.955")

    mid = (buy + sell) / 2
    expected = (buy - sell) / mid
    assert spread == expected


def test_estimate_entry_spread_pct_by_impact_rejects_if_insufficient_depth_in_window():
    depth = OrderbookDepthSnapshot(
        symbol="ABC",
        lighter_bids=[(Decimal("100.00"), Decimal("0.5"))],
        lighter_asks=[(Decimal("100.10"), Decimal("0.5"))],
        x10_bids=[(Decimal("99.90"), Decimal("0.5"))],
        x10_asks=[(Decimal("100.05"), Decimal("0.5"))],
    )

    spread, metrics = estimate_entry_spread_pct_by_impact(
        depth,
        long_exchange="X10",
        target_qty=Decimal("5"),
        max_price_impact_pct=Decimal("0"),
    )

    assert metrics["ok"] is False
    assert spread == Decimal("1.0")

