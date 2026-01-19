from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.domain.rules import evaluate_exit_rules


def _make_recent_trade(*, entry_apy: Decimal = Decimal("0.50"), notional: Decimal = Decimal("1000")) -> Trade:
    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=notional,
        entry_apy=entry_apy,
        entry_spread=Decimal("0.001"),
    )
    trade.opened_at = datetime.now(UTC) - timedelta(hours=1)
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = notional
    trade.leg2.entry_price = notional
    return trade


def test_early_edge_exit_exits_on_apy_crash_before_min_hold():
    """
    REMOVED 2026-01-10: APY Crash Smart Exit was replaced by FundingVelocityExitStrategy.
    This test now validates early_edge_exit which handles funding flip (not APY ratio crash).

    The funding flip exit triggers when:
    - Funding has flipped negative (current_diff_hourly < 0 for BUY position)
    - AND projected loss over horizon > exit cost (exit is cheaper than holding)
    """
    trade = _make_recent_trade(entry_apy=Decimal("0.50"))

    # For Side.BUY: current_diff_hourly = x10_rate - lighter_rate
    # We want a negative diff: lighter > x10 (we pay funding)
    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("5.00"),  # Some profit to lock in
        current_apy=Decimal("0.10"),
        lighter_rate=Decimal("0.00020"),  # Lighter higher
        x10_rate=Decimal("0.00000"),  # X10 lower (for BUY, we pay)
        min_hold_seconds=86400,  # 24h gate
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("0.50"),
        early_edge_exit_enabled=True,
        early_edge_exit_min_age_seconds=1,
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert "EARLY_EDGE:" in decision.reason


def test_early_edge_exit_exits_on_funding_flip_before_min_hold():
    trade = _make_recent_trade(entry_apy=Decimal("0.50"), notional=Decimal("1000"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0"),
        current_apy=Decimal("0.50"),
        lighter_rate=Decimal("0.00020"),
        x10_rate=Decimal("0.00000"),  # x10 - lighter < 0 (we bleed funding)
        min_hold_seconds=86400,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("12"),
        estimated_exit_cost_usd=Decimal("1.0"),
        early_edge_exit_enabled=True,
        early_edge_exit_min_age_seconds=1,
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert decision.reason.startswith("EARLY_EDGE:")
