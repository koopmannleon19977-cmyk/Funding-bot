from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.domain.rules import evaluate_exit_rules


def _make_open_trade(*, side_leg1: Side, notional: Decimal = Decimal("1000")) -> Trade:
    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=side_leg1,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=notional,
        entry_apy=Decimal("0.50"),
        entry_spread=Decimal("0.001"),
    )
    trade.opened_at = datetime.now(UTC) - timedelta(hours=100)
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = notional
    trade.leg2.entry_price = notional
    return trade


def test_opportunity_rotation_cost_aware_exits_when_gain_covers_roundtrip():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),  # net_rate = +0.00020 -> Hold (NetEV)
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        high_water_mark=Decimal("0"),
        best_opportunity_apy=Decimal("1.40"),
        opportunity_cost_diff=Decimal("0.50"),
        estimated_exit_cost_usd=Decimal("1.0"),
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("24.0"),
        exit_ev_exit_cost_multiple=Decimal("1.3"),
        exit_ev_skip_opportunity_cost_when_edge_good=False,
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert decision.reason.startswith("Opportunity Rotation:")


def test_opportunity_rotation_cost_aware_holds_when_gain_too_small():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),  # net_rate = +0.00020 -> Hold (NetEV)
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        high_water_mark=Decimal("0"),
        best_opportunity_apy=Decimal("0.60"),
        opportunity_cost_diff=Decimal("0.05"),
        estimated_exit_cost_usd=Decimal("1.0"),
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("24.0"),
        exit_ev_exit_cost_multiple=Decimal("1.3"),
        exit_ev_skip_opportunity_cost_when_edge_good=False,
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is False
    assert "Hold (NetEV)" in decision.reason
