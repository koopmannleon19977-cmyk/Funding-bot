from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.domain.rules import evaluate_exit_rules


def _make_trade_opened_recently(*, hours_ago: int = 1) -> Trade:
    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("1000"),
        entry_apy=Decimal("0.50"),
        entry_spread=Decimal("0.001"),
    )
    trade.opened_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = Decimal("1000")
    trade.leg2.entry_price = Decimal("1000")
    return trade


def test_early_take_profit_exits_when_pnl_exceeds_effective_threshold():
    """
    EARLY_TAKE_PROFIT should only trigger when:
    current_pnl >= early_take_profit_net_usd + (estimated_exit_cost_usd * slippage_multiple)

    With threshold=0.30, exit_cost=0.20, slippage_multiple=1.5:
    raw_buffer = (0.20 * 1.5) = 0.30

    NOTE: The bot enforces a minimum absolute buffer of $0.50 to avoid razor-thin early exits.
    effective_threshold = 0.30 + max(0.30, 0.50) = 0.80
    """
    trade = _make_trade_opened_recently(hours_ago=1)

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0.85"),  # Above effective threshold of 0.80
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),
        min_hold_seconds=172800,  # 48h gate would block normally
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("24"),
        estimated_exit_cost_usd=Decimal("0.20"),
        early_take_profit_enabled=True,
        early_take_profit_net_usd=Decimal("0.30"),
        early_take_profit_slippage_multiple=Decimal("1.5"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is True
    assert decision.reason.startswith("EARLY_TAKE_PROFIT:")
    # Verify the effective threshold is shown in reason
    assert "0.80" in decision.reason or "buffer" in decision.reason


def test_early_take_profit_does_not_exit_when_pnl_below_effective_threshold():
    """
    BUGFIX 2026-01-01: Even if pnl > base threshold, should NOT exit if pnl < effective threshold.
    
    With threshold=0.30, exit_cost=0.20, slippage_multiple=1.5:
    effective_threshold = 0.30 + (0.20 * 1.5) = 0.60
    
    pnl=0.50 is above base (0.30) but below effective (0.60) -> should NOT exit
    """
    trade = _make_trade_opened_recently(hours_ago=1)

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0.50"),  # Above base threshold but below effective
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),
        min_hold_seconds=172800,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("24"),
        estimated_exit_cost_usd=Decimal("0.20"),
        early_take_profit_enabled=True,
        early_take_profit_net_usd=Decimal("0.30"),
        early_take_profit_slippage_multiple=Decimal("1.5"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    assert decision.should_exit is False
    assert "Hold time" in decision.reason


def test_early_take_profit_slippage_buffer_prevents_premature_exit():
    """
    Regression test for POPCAT bug (2026-01-01):
    - Estimated PnL was $1.53, triggered EARLY_TAKE_PROFIT with threshold $1.50
    - Actual realized PnL was -$0.62 due to 3-attempt close with slippage

    With slippage buffer: effective_threshold = $1.50 + ($0.50 * 1.5) = $2.25
    This would have prevented the premature exit.
    """
    trade = _make_trade_opened_recently(hours_ago=1)

    # Simulate POPCAT scenario: pnl=$1.53, threshold=$1.50, exit_cost=$0.50
    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("1.53"),  # What POPCAT showed
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),
        min_hold_seconds=172800,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("24"),
        estimated_exit_cost_usd=Decimal("0.50"),  # Realistic exit cost
        early_take_profit_enabled=True,
        early_take_profit_net_usd=Decimal("1.50"),  # Base threshold
        early_take_profit_slippage_multiple=Decimal("1.5"),  # Makes effective = $2.25
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    # With buffer: $1.53 < $2.25 -> should NOT exit
    assert decision.should_exit is False
    assert "Hold time" in decision.reason


def test_early_take_profit_with_zero_exit_cost_uses_base_threshold():
    """
    When estimated_exit_cost_usd=0, the slippage buffer is 0 but the minimum absolute buffer still applies.
    """
    trade = _make_trade_opened_recently(hours_ago=1)

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=Decimal("0.85"),  # Above effective threshold of 0.80
        current_apy=Decimal("0.20"),
        lighter_rate=Decimal("0.00010"),
        x10_rate=Decimal("0.00030"),
        min_hold_seconds=172800,
        max_hold_hours=Decimal("999"),
        profit_target_usd=Decimal("999"),
        funding_flip_hours=Decimal("24"),
        estimated_exit_cost_usd=Decimal("0"),  # No exit cost estimated
        early_take_profit_enabled=True,
        early_take_profit_net_usd=Decimal("0.30"),
        early_take_profit_slippage_multiple=Decimal("1.5"),
        # NEW: Liquidation distance (defaults for tests)
        leg1_liq_distance_pct=Decimal("1.0"),
        leg2_liq_distance_pct=Decimal("1.0"),
        leg1_mark_price=None,
        leg2_mark_price=None,
    )

    # effective_threshold = 0.30 + max(0 * 1.5, 0.50) = 0.80
    # 0.85 >= 0.80 -> should exit
    assert decision.should_exit is True
    assert decision.reason.startswith("EARLY_TAKE_PROFIT:")
