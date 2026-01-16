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


def test_net_ev_exit_positive_edge_too_small_exits():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.10"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00011"),  # net_rate = +0.00001
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("0.50"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("1.0"),
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("12.0"),
        exit_ev_exit_cost_multiple=Decimal("1.2"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert "NetEV" in decision.reason


def test_net_ev_hold_skips_profit_target_when_edge_good():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("10.0"),  # profit target would normally trigger
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),  # net_rate = +0.00020 -> $0.20/h
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("0.50"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("1.0"),
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("12.0"),  # $2.4 projected >= $1.2 threshold
        exit_ev_exit_cost_multiple=Decimal("1.2"),
        exit_ev_skip_profit_target_when_edge_good=True,
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is False
    assert "Hold (NetEV)" in decision.reason


def test_net_ev_exit_negative_edge_large_loss_exits():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.05"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00000"),  # net_rate = -0.00010 -> -$0.10/h
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("0.5"),
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("12.0"),  # projected loss $1.2 >= $0.6 threshold
        exit_ev_exit_cost_multiple=Decimal("1.2"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert ("funding loss" in decision.reason.lower()) or ("funding flip" in decision.reason.lower())


def test_net_ev_hold_negative_edge_small_loss_holds():
    trade = _make_open_trade(side_leg1=Side.BUY, notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.05"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00009"),  # net_rate = -0.00001 -> -$0.01/h
        min_hold_seconds=1,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("1.0"),  # threshold $1.2 > loss $0.12
        exit_ev_enabled=True,
        exit_ev_horizon_hours=Decimal("12.0"),
        exit_ev_exit_cost_multiple=Decimal("1.2"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is False
    assert "Hold (NetEV)" in decision.reason
