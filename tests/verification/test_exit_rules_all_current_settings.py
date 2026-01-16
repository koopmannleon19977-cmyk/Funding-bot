from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.config.settings import Settings
from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.domain.models import TradeStatus
from funding_bot.domain.rules import evaluate_exit_rules


def _settings() -> Settings:
    # Loads src/funding_bot/config/config.yaml (current repo settings).
    return Settings.from_yaml(env="prod")


def _mk_trade(
    *,
    age: timedelta,
    entry_apy: Decimal,
    notional_usd: Decimal = Decimal("400"),
    side: Side = Side.BUY,
) -> Trade:
    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=side,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=notional_usd,
        entry_apy=entry_apy,
    )
    trade.opened_at = datetime.now(UTC) - age
    trade.status = TradeStatus.OPEN
    # Initialize leg fields for delta_bound and other exit rules
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")
    return trade


def _decide(
    s: Settings,
    *,
    trade: Trade,
    current_pnl: Decimal = Decimal("0"),
    price_pnl: Decimal | None = Decimal("0"),
    current_apy: Decimal | None = None,
    lighter_rate: Decimal = Decimal("0"),
    x10_rate: Decimal = Decimal("0"),
    best_opportunity_apy: Decimal = Decimal("0"),
    estimated_exit_cost_usd: Decimal = Decimal("0.10"),
    high_water_mark: Decimal = Decimal("0"),
) -> object:
    if current_apy is None:
        current_apy = s.trading.min_apy_filter
    return evaluate_exit_rules(
        trade=trade,
        current_pnl=current_pnl,
        current_apy=current_apy,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        min_hold_seconds=s.trading.min_hold_seconds,
        max_hold_hours=s.trading.max_hold_hours,
        profit_target_usd=s.trading.min_profit_exit_usd,
        funding_flip_hours=s.trading.funding_flip_hours_threshold,
        high_water_mark=high_water_mark,
        # REMOVED 2026-01-10: trailing_stop parameters - replaced by ATR strategy
        best_opportunity_apy=best_opportunity_apy,
        opportunity_cost_diff=s.trading.opportunity_cost_apy_diff,
        estimated_exit_cost_usd=estimated_exit_cost_usd,
        price_pnl=price_pnl,
        early_take_profit_enabled=s.trading.early_take_profit_enabled,
        early_take_profit_net_usd=s.trading.early_take_profit_net_usd,
        early_take_profit_slippage_multiple=s.trading.early_take_profit_slippage_multiple,
        early_edge_exit_enabled=s.trading.early_edge_exit_enabled,
        early_edge_exit_min_age_seconds=s.trading.early_edge_exit_min_age_seconds,
        exit_ev_enabled=s.trading.exit_ev_enabled,
        exit_ev_horizon_hours=s.trading.exit_ev_horizon_hours,
        exit_ev_exit_cost_multiple=s.trading.exit_ev_exit_cost_multiple,
        exit_ev_skip_profit_target_when_edge_good=s.trading.exit_ev_skip_profit_target_when_edge_good,
        exit_ev_skip_opportunity_cost_when_edge_good=s.trading.exit_ev_skip_opportunity_cost_when_edge_good,
        # NEW v3.1 LEAN: Emergency Exits (Layer 1)
        liquidation_distance_monitoring_enabled=s.trading.liquidation_distance_monitoring_enabled,
        liquidation_distance_min_pct=s.trading.liquidation_distance_min_pct,
        # NEW: Liquidation distance per leg (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),  # 100% = safe (default)
        leg2_liq_distance_pct=Decimal("1.0"),  # 100% = safe (default)
        # NEW: Current mark prices per leg (None = use entry_price)
        leg1_mark_price=None,
        leg2_mark_price=None,
        delta_bound_enabled=s.trading.delta_bound_enabled,
        delta_bound_max_delta_pct=s.trading.delta_bound_max_delta_pct,
        # NEW v3.1 LEAN: ATR Trailing Stop (Layer 2)
        atr_trailing_enabled=s.trading.atr_trailing_enabled,
        atr_period=s.trading.atr_period,
        atr_multiplier=s.trading.atr_multiplier,
        atr_min_activation_usd=s.trading.atr_min_activation_usd,
        # NEW v3.1 LEAN: Funding Velocity Exit (Layer 3)
        funding_velocity_exit_enabled=s.trading.funding_velocity_exit_enabled,
        velocity_threshold_hourly=s.trading.velocity_threshold_hourly,
        acceleration_threshold=s.trading.acceleration_threshold,
        velocity_lookback_hours=s.trading.velocity_lookback_hours,
    )


def _assert_exit(decision: object, prefix: str) -> None:
    assert getattr(decision, "should_exit") is True
    assert str(getattr(decision, "reason")).startswith(prefix)


def _assert_hold(decision: object, prefix: str) -> None:
    assert getattr(decision, "should_exit") is False
    assert str(getattr(decision, "reason")).startswith(prefix)


def test_emergency_catastrophic_funding_flip_exits_even_before_min_hold():
    s = _settings()

    trade = _mk_trade(age=timedelta(minutes=5), entry_apy=Decimal("0.60"))
    # For Side.BUY: current_diff_hourly = x10 - lighter
    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("0"),
        price_pnl=Decimal("0"),
        lighter_rate=Decimal("0.0010"),
        x10_rate=Decimal("0.0000"),
    )
    _assert_exit(decision, "EMERGENCY: Catastrophic funding flip")


def test_early_take_profit_exits_before_min_hold_with_current_threshold():
    s = _settings()

    assert s.trading.early_take_profit_enabled is True
    trade = _mk_trade(age=timedelta(minutes=5), entry_apy=Decimal("0.60"))

    est_exit_cost = Decimal("0.35")
    slippage_buffer = est_exit_cost * s.trading.early_take_profit_slippage_multiple
    effective_threshold = s.trading.early_take_profit_net_usd + max(slippage_buffer, Decimal("0.50"))

    decision = _decide(
        s,
        trade=trade,
        current_pnl=effective_threshold + Decimal("0.01"),
        price_pnl=effective_threshold + Decimal("0.01"),
        estimated_exit_cost_usd=est_exit_cost,
    )
    _assert_exit(decision, "EARLY_TAKE_PROFIT:")


def test_early_edge_exit_triggers_on_apy_crash_after_min_age():
    """
    Test early_edge_exit bypasses min_hold when funding flips after min age.

    Note: APY Crash Smart Exit was removed 2026-01-10. This test now validates
    early_edge_exit which checks for funding flip (not APY ratio crash).

    The funding flip exit triggers when:
    - Funding has flipped negative (current_diff_apy < 0)
    - AND projected loss over horizon > exit cost (exit is cheaper than holding)
    """
    s = _settings()
    assert s.trading.early_edge_exit_enabled is True

    # Use slightly older trade to ensure age gate passes (timing issues)
    # Also use higher notional to ensure projected loss > exit cost
    trade = _mk_trade(age=timedelta(hours=2, seconds=10), entry_apy=Decimal("0.60"), notional_usd=Decimal("1000"))
    # Early-edge-exit bypasses min_hold once the trade is older than the early-edge min age.
    # For Side.BUY: current_diff_hourly = x10_rate - lighter_rate
    # Entry had positive funding (lighter > x10), now flipped negative (lighter < x10)
    # With notional=1000: projected_loss = 0.00010 * 1000 * 8 = $0.80 > $0.50 exit cost
    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("5.00"),  # Good profit to lock in
        price_pnl=Decimal("0"),
        current_apy=Decimal("0.0"),
        lighter_rate=Decimal("0.00010"),  # Lighter positive
        x10_rate=Decimal("0.00000"),      # X10 zero (lighter > x10 means we PAY for Side.BUY)
        estimated_exit_cost_usd=Decimal("0.50"),
    )
    _assert_exit(decision, "EARLY_EDGE:")


def test_early_edge_exit_triggers_on_funding_flip_after_min_age():
    s = _settings()
    assert s.trading.early_edge_exit_enabled is True

    trade = _mk_trade(age=timedelta(hours=2), entry_apy=Decimal("0.60"))
    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("1.00"),
        price_pnl=Decimal("0"),
        estimated_exit_cost_usd=Decimal("0.10"),
        current_apy=s.trading.min_apy_filter,
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00000"),
    )
    _assert_exit(decision, "EARLY_EDGE: Funding flipped")


def test_min_hold_gate_blocks_standard_rules_when_not_early_tp():
    s = _settings()
    trade = _mk_trade(age=timedelta(minutes=10), entry_apy=Decimal("0.60"))

    decision = _decide(
        s,
        trade=trade,
        # high pnl but price_pnl below early-tp threshold => early-tp won't fire
        current_pnl=s.trading.min_profit_exit_usd * Decimal("10"),
        price_pnl=Decimal("0"),
        current_apy=Decimal("0.01"),
        estimated_exit_cost_usd=Decimal("0.10"),
    )
    _assert_hold(decision, "Hold time")


def test_max_hold_exits_after_min_hold():
    s = _settings()
    trade = _mk_trade(age=timedelta(hours=float(s.trading.max_hold_hours) + 1), entry_apy=Decimal("0.60"))

    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("0"),
        price_pnl=Decimal("0"),
        current_apy=s.trading.min_apy_filter,
        lighter_rate=Decimal("0.0"),
        x10_rate=Decimal("0.00010"),
    )
    _assert_exit(decision, "Max hold time reached")


# REMOVED 2026-01-10: test_volatility_panic_exits_after_min_hold
# Reason: volatility_panic_threshold removed - market-neutral strategy doesn't need price volatility checks
# We're long one exchange, short the other - price movements affect both legs equally.

def test_exit_ev_negative_funding_loss_triggers_exit_without_funding_flip():
    s = _settings()
    assert s.trading.exit_ev_enabled is True

    trade = _mk_trade(age=timedelta(hours=49), entry_apy=Decimal("0.60"), notional_usd=Decimal("400"))
    est_exit_cost = Decimal("0.01")
    # Keep current_diff_apy above -5% buffer so check_funding_flip won't trigger.
    # target current_diff_apy ~= -4.38% => diff_hourly=-0.0438/8760 = -5e-6
    lighter_rate = Decimal("0.000005")
    x10_rate = Decimal("0.0")

    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("0"),
        price_pnl=Decimal("0"),
        current_apy=s.trading.min_apy_filter,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        estimated_exit_cost_usd=est_exit_cost,
    )
    _assert_exit(decision, "NetEV: projected")


def test_exit_ev_positive_but_too_small_triggers_exit():
    s = _settings()
    trade = _mk_trade(age=timedelta(hours=49), entry_apy=Decimal("0.60"), notional_usd=Decimal("400"))
    est_exit_cost = Decimal("0.02")
    # Positive net funding but too small vs exit cost threshold.
    lighter_rate = Decimal("0.0000000")
    x10_rate = Decimal("0.0000020")

    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("0"),
        price_pnl=Decimal("0"),
        current_apy=s.trading.min_apy_filter,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        estimated_exit_cost_usd=est_exit_cost,
    )
    _assert_exit(decision, "NetEV: projected")


# REMOVED 2026-01-10: test_trailing_stop_triggers_exit
# Reason: trailing_stop_activation_usd and trailing_stop_callback_pct removed
# Replaced by ATRTrailingStopStrategy (volatility-adaptive, professional strategy)

def test_profit_target_is_skipped_when_exit_ev_holds_edge_good():
    s = _settings()
    assert s.trading.exit_ev_enabled is True
    assert s.trading.exit_ev_skip_profit_target_when_edge_good is True

    trade = _mk_trade(age=timedelta(hours=49), entry_apy=Decimal("0.60"), notional_usd=Decimal("400"))

    # Make NetEV return "Hold (NetEV)" (edge good) and avoid other exits.
    decision = _decide(
        s,
        trade=trade,
        current_pnl=s.trading.min_profit_exit_usd + Decimal("10.0"),
        price_pnl=Decimal("0"),
        current_apy=s.trading.min_apy_filter,
        lighter_rate=Decimal("0.0"),
        x10_rate=Decimal("0.00020"),  # strong positive net funding
        estimated_exit_cost_usd=Decimal("0.10"),
        best_opportunity_apy=Decimal("0.0"),
        high_water_mark=Decimal("0"),
    )

    # With current settings, profit target is not evaluated when NetEV holds edge good.
    _assert_hold(decision, "Hold (NetEV):")


def test_opportunity_cost_rotation_triggers_with_exit_ev_cost_aware_threshold():
    s = _settings()
    assert s.trading.exit_ev_enabled is True
    assert s.trading.exit_ev_skip_opportunity_cost_when_edge_good is False

    trade = _mk_trade(age=timedelta(hours=49), entry_apy=Decimal("0.60"), notional_usd=Decimal("400"))

    decision = _decide(
        s,
        trade=trade,
        current_pnl=Decimal("0"),
        price_pnl=Decimal("0"),
        current_apy=s.trading.min_apy_filter,
        # Make NetEV hold (edge good)
        lighter_rate=Decimal("0.0"),
        x10_rate=Decimal("0.00020"),
        estimated_exit_cost_usd=Decimal("0.10"),
        best_opportunity_apy=Decimal("1.20"),
    )
    _assert_exit(decision, "Opportunity Rotation:")
