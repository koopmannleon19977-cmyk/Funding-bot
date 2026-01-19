from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Side, Trade, TradeStatus
from funding_bot.services.market_data import OrderbookDepthSnapshot, PriceSnapshot
from funding_bot.services.positions import PositionManager


def _make_open_trade() -> Trade:
    trade = Trade.create(
        symbol="ETH",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("100"),
        entry_apy=Decimal("0.50"),
        entry_spread=Decimal("0.001"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(hours=1)
    trade.last_funding_update = datetime.now(UTC)

    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")
    return trade


@pytest.mark.asyncio
async def test_evaluate_exit_depth_path_does_not_break_exit_cost_estimation(caplog):
    """
    Regression: when early_take_profit is enabled and we use a fresh DEPTH snapshot,
    `_evaluate_exit` must not throw/complain due to an uninitialized `orderbook` variable.
    """
    trade = _make_open_trade()

    ts = MagicMock()
    ts.early_take_profit_enabled = True
    ts.early_take_profit_net_usd = Decimal("999")  # ensure no early-TP exit in this test
    ts.early_take_profit_slippage_multiple = Decimal("1.5")
    ts.min_hold_seconds = 172800
    ts.max_hold_hours = Decimal("999")
    ts.min_profit_exit_usd = Decimal("999")
    ts.min_apy_filter = Decimal("0.30")
    ts.funding_flip_hours_threshold = Decimal("24")
    ts.volatility_panic_threshold = Decimal("10.0")
    # REMOVED 2026-01-10: trailing_stop parameters - replaced by ATR strategy
    ts.opportunity_cost_apy_diff = Decimal("1.0")
    # NEW v3.1 LEAN: delta_bound parameters
    ts.delta_bound_enabled = True
    ts.delta_bound_max_delta_pct = Decimal("0.03")
    # NEW v3.1 LEAN: ATR trailing parameters
    ts.atr_trailing_enabled = True
    ts.atr_period = 14
    ts.atr_multiplier = Decimal("2.0")
    ts.atr_min_activation_usd = Decimal("3.0")
    # NEW v3.1 LEAN: Funding velocity parameters
    ts.funding_velocity_exit_enabled = True
    ts.velocity_threshold_hourly = Decimal("-0.0015")
    ts.acceleration_threshold = Decimal("-0.0008")
    ts.velocity_lookback_hours = 6
    ts.early_edge_exit_enabled = False
    ts.early_edge_exit_min_age_seconds = 3600
    ts.exit_ev_enabled = False
    ts.exit_ev_horizon_hours = Decimal("12")
    ts.exit_ev_exit_cost_multiple = Decimal("1.2")
    ts.exit_ev_skip_profit_target_when_edge_good = True
    ts.exit_ev_skip_opportunity_cost_when_edge_good = True

    settings = MagicMock()
    settings.trading = ts

    store = AsyncMock()
    store.update_trade = AsyncMock()
    event_bus = AsyncMock()
    lighter = MagicMock()
    x10 = MagicMock()

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="ETH",
        lighter_price=Decimal("100"),
        x10_price=Decimal("100"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_funding.return_value = MagicMock(
        apy=Decimal("0"),
        lighter_rate=MagicMock(rate_hourly=Decimal("0")),
        x10_rate=MagicMock(rate_hourly=Decimal("0")),
    )
    market_data.get_fresh_orderbook_depth = AsyncMock(
        return_value=OrderbookDepthSnapshot(
            symbol="ETH",
            lighter_bids=[(Decimal("99"), Decimal("10"))],
            lighter_asks=[(Decimal("101"), Decimal("10"))],
            x10_bids=[(Decimal("99"), Decimal("10"))],
            x10_asks=[(Decimal("101"), Decimal("10"))],
            lighter_updated=now,
            x10_updated=now,
        )
    )
    market_data.get_fresh_orderbook = AsyncMock(side_effect=AssertionError("unexpected L1 fallback"))
    market_data.get_fee_schedule = MagicMock(
        return_value={"lighter_taker": Decimal("0.0002"), "x10_taker": Decimal("0.000225")}
    )
    market_data.get_orderbook = MagicMock(side_effect=AssertionError("unexpected cached orderbook access"))

    mgr = PositionManager(settings, lighter, x10, store, event_bus, market_data, opportunity_engine=None)

    caplog.set_level(logging.WARNING)
    decision = await mgr._evaluate_exit(trade)

    assert decision is not None
    assert "Failed to estimate exit costs" not in caplog.text


@pytest.mark.asyncio
async def test_early_tp_uses_execution_buffer_to_avoid_false_positive_exit():
    """
    Regression (FARTCOIN 2026-01-04):
    Early-TP was triggered on a small projected profit, but the actual close path
    (market/taker + IOC slippage padding) realized a loss.

    `_evaluate_exit` must pass an execution buffer so EARLY_TAKE_PROFIT does not bypass
    min-hold on razor-thin signals.
    """
    trade = _make_open_trade()
    trade.symbol = "FARTCOIN"
    trade.target_notional_usd = Decimal("182.85")
    trade.leg1.filled_qty = Decimal("497")
    trade.leg2.filled_qty = Decimal("497")
    trade.leg1.entry_price = Decimal("0.3568")
    trade.leg2.entry_price = Decimal("0.3570")

    ts = MagicMock()
    ts.early_take_profit_enabled = True
    ts.early_take_profit_net_usd = Decimal("0.80")
    ts.early_take_profit_slippage_multiple = Decimal("2.5")
    ts.early_tp_fast_close_enabled = True
    ts.early_tp_fast_close_use_taker_directly = True
    ts.min_hold_seconds = 172800
    ts.max_hold_hours = Decimal("999")
    ts.min_profit_exit_usd = Decimal("999")
    ts.min_apy_filter = Decimal("0.30")
    ts.funding_flip_hours_threshold = Decimal("24")
    ts.volatility_panic_threshold = Decimal("10.0")
    # REMOVED 2026-01-10: trailing_stop parameters - replaced by ATR strategy
    ts.opportunity_cost_apy_diff = Decimal("1.0")
    # NEW v3.1 LEAN: delta_bound parameters
    ts.delta_bound_enabled = True
    ts.delta_bound_max_delta_pct = Decimal("0.03")
    # NEW v3.1 LEAN: ATR trailing parameters
    ts.atr_trailing_enabled = True
    ts.atr_period = 14
    ts.atr_multiplier = Decimal("2.0")
    ts.atr_min_activation_usd = Decimal("3.0")
    # NEW v3.1 LEAN: Funding velocity parameters
    ts.funding_velocity_exit_enabled = True
    ts.velocity_threshold_hourly = Decimal("-0.0015")
    ts.acceleration_threshold = Decimal("-0.0008")
    ts.velocity_lookback_hours = 6
    ts.early_edge_exit_enabled = False
    ts.early_edge_exit_min_age_seconds = 3600
    ts.exit_ev_enabled = False
    ts.exit_ev_horizon_hours = Decimal("12")
    ts.exit_ev_exit_cost_multiple = Decimal("1.2")
    ts.exit_ev_skip_profit_target_when_edge_good = True
    ts.exit_ev_skip_opportunity_cost_when_edge_good = True

    es = MagicMock()
    es.x10_close_slippage = Decimal("0.005")  # 0.5% IOC padding
    es.leg1_escalate_to_taker_slippage = Decimal("0.0015")  # 0.15% market/taker drift proxy

    settings = MagicMock()
    settings.trading = ts
    settings.execution = es

    store = AsyncMock()
    store.update_trade = AsyncMock()
    event_bus = AsyncMock()
    lighter = MagicMock()
    x10 = MagicMock()

    market_data = MagicMock()
    now = datetime.now(UTC)
    market_data.get_price.return_value = PriceSnapshot(
        symbol="FARTCOIN",
        lighter_price=Decimal("0.3690"),
        x10_price=Decimal("0.3690"),
        lighter_updated=now,
        x10_updated=now,
    )
    market_data.get_funding.return_value = MagicMock(
        apy=Decimal("0"),
        lighter_rate=MagicMock(rate_hourly=Decimal("0")),
        x10_rate=MagicMock(rate_hourly=Decimal("0")),
    )

    # Construct a depth snapshot where "if closed now" price_pnl ~= +$1.36 (just above the old $1.30 threshold).
    market_data.get_fresh_orderbook_depth = AsyncMock(
        return_value=OrderbookDepthSnapshot(
            symbol="FARTCOIN",
            lighter_bids=[(Decimal("0.370537"), Decimal("10000"))],
            lighter_asks=[(Decimal("0.370600"), Decimal("10000"))],
            x10_bids=[(Decimal("0.367900"), Decimal("10000"))],
            x10_asks=[(Decimal("0.368000"), Decimal("10000"))],
            lighter_updated=now,
            x10_updated=now,
        )
    )
    market_data.get_fresh_orderbook = AsyncMock(side_effect=AssertionError("unexpected L1 fallback"))
    market_data.get_fee_schedule = AsyncMock(return_value={"lighter_taker": Decimal("0"), "x10_taker": Decimal("0")})
    market_data.get_orderbook = MagicMock(side_effect=AssertionError("unexpected cached orderbook access"))

    mgr = PositionManager(settings, lighter, x10, store, event_bus, market_data, opportunity_engine=None)
    decision = await mgr._evaluate_exit(trade)

    # With the execution buffer applied, Early-TP should NOT trigger; min-hold gate should block instead.
    assert decision.should_exit is False
    assert "Hold time" in decision.reason
