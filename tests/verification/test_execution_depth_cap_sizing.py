from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Opportunity, Position, Side, TradeStatus
from funding_bot.services.execution import ExecutionEngine
from funding_bot.services.market_data import OrderbookSnapshot


@pytest.mark.asyncio
async def test_execution_sizes_down_to_pass_strict_depth_preflight():
    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    # Balances for preflight + sizing
    lighter.get_available_balance.return_value.available = Decimal("1000")
    lighter.get_available_balance.return_value.total = Decimal("1000")
    x10.get_available_balance.return_value.available = Decimal("1000")
    x10.get_available_balance.return_value.total = Decimal("1000")

    store.list_open_trades.return_value = []
    store.create_execution_attempt.return_value = "att1"
    store.update_execution_attempt.return_value = True
    store.create_trade.return_value = True
    store.update_trade.return_value = True

    settings = MagicMock()
    settings.live_trading = True

    # Spread gating (must pass)
    settings.trading.max_spread_filter_percent = Decimal("0.01")
    settings.trading.max_spread_cap_percent = Decimal("1.0")

    # Depth gating enabled so sizing cap is applied
    settings.trading.depth_gate_enabled = True
    settings.trading.min_l1_notional_usd = Decimal("0")
    settings.trading.min_l1_notional_multiple = Decimal("0")
    settings.trading.max_l1_qty_utilization = Decimal("0.7")

    # Sizing / risk
    settings.trading.leverage_multiplier = Decimal("1")
    settings.trading.x10_leverage_multiplier = Decimal("1")
    settings.trading.max_open_trades = 1
    settings.risk.max_exposure_pct = Decimal("100")
    settings.risk.max_trade_size_pct = Decimal("100")

    # Strict hedge preflight enabled (mult reduces max utilization)
    settings.execution.hedge_depth_preflight_enabled = True
    settings.execution.hedge_depth_preflight_multiplier = Decimal("1.5")
    settings.execution.hedge_depth_preflight_checks = 1
    settings.execution.hedge_depth_preflight_delay_seconds = Decimal("0")

    # Required by execution engine (even though we patch leg execution)
    settings.execution.maker_order_max_retries = 1
    settings.execution.maker_order_timeout_seconds = 1
    settings.execution.microfill_max_unhedged_usd = Decimal("10")
    settings.execution.taker_order_slippage = Decimal("0.01")

    market_data = MagicMock()
    market_data.get_fresh_orderbook = AsyncMock()

    market_data.get_price.return_value.mid_price = Decimal("100")
    market_data.get_price.return_value.lighter_price = Decimal("100")
    market_data.get_price.return_value.x10_price = Decimal("100")

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.step_size = Decimal("0.0001")
        info.tick_size = Decimal("0.01")
        info.min_qty = Decimal("0")
        info.lighter_max_leverage = Decimal("10")
        info.x10_max_leverage = Decimal("10")
        return info

    market_data.get_market_info.side_effect = _market_info

    # Lighter SELL uses lighter_bid/lighter_bid_qty, X10 BUY uses x10_ask/x10_ask_qty.
    # With strict preflight: max_util = 0.7 / 1.5 = 0.466..., so cap should size below 46.6% of L1 qty.
    fresh_ob = OrderbookSnapshot(
        symbol="ABC",
        lighter_bid=Decimal("100"),
        lighter_ask=Decimal("100.1"),
        x10_bid=Decimal("99.9"),
        x10_ask=Decimal("100"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("10"),
    )
    market_data.get_fresh_orderbook.return_value = fresh_ob
    market_data.get_orderbook.return_value = fresh_ob

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    lighter.get_position.return_value = Position(
        symbol="ABC",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
    )
    x10.get_position.return_value = Position(
        symbol="ABC",
        exchange=Exchange.X10,
        side=Side.SELL,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
    )

    async def _fake_leg1(trade, opp, *, attempt_id=None):
        trade.leg1.order_id = "l1"
        trade.leg1.filled_qty = trade.target_qty
        trade.leg1.entry_price = Decimal("100")
        trade.leg1.fees = Decimal("0")

    async def _fake_leg2(trade, opp, **_kwargs):
        trade.leg2.order_id = "l2"
        trade.leg2.filled_qty = trade.target_qty
        trade.leg2.entry_price = Decimal("100")
        trade.leg2.fees = Decimal("0")

    engine._execute_leg1 = AsyncMock(side_effect=_fake_leg1)
    engine._execute_leg2 = AsyncMock(side_effect=_fake_leg2)

    opp = Opportunity(
        symbol="ABC",
        timestamp=datetime.now(),
        lighter_rate=Decimal("0.0001"),
        x10_rate=Decimal("0.0"),
        net_funding_hourly=Decimal("0.0001"),
        apy=Decimal("0.5"),
        spread_pct=Decimal("0.0"),
        mid_price=Decimal("100"),
        lighter_best_bid=fresh_ob.lighter_bid,
        lighter_best_ask=fresh_ob.lighter_ask,
        x10_best_bid=fresh_ob.x10_bid,
        x10_best_ask=fresh_ob.x10_ask,
        suggested_qty=Decimal("10"),
        suggested_notional=Decimal("1000"),
        expected_value_usd=Decimal("10"),
        breakeven_hours=Decimal("1"),
        long_exchange=Exchange.X10,
        short_exchange=Exchange.LIGHTER,
    )

    result = await engine.execute(opp)

    assert result.success is True
    assert result.trade is not None
    assert result.trade.status == TradeStatus.OPEN

    strict_max_util = Decimal("0.7") / Decimal("1.5")
    assert (result.trade.target_qty / fresh_ob.x10_ask_qty) <= strict_max_util
    assert store.create_trade.await_count == 1
