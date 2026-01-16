from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, ExecutionState, Opportunity
from funding_bot.services.execution import ExecutionEngine
from funding_bot.services.market_data import OrderbookDepthSnapshot, OrderbookSnapshot


@pytest.mark.asyncio
async def test_fast_hedge_skips_post_leg1_impact_depth_fetch():
    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    lighter.get_available_balance.return_value.available = Decimal("1000")
    lighter.get_available_balance.return_value.total = Decimal("1000")
    x10.get_available_balance.return_value.available = Decimal("1000")
    x10.get_available_balance.return_value.total = Decimal("1000")

    store.list_open_trades.return_value = []
    store.create_execution_attempt.return_value = "att1"
    store.update_execution_attempt.return_value = True

    settings = MagicMock()
    settings.live_trading = True
    settings.websocket.market_data_streams_enabled = False

    settings.trading.desired_notional_usd = Decimal("500")
    settings.trading.max_spread_filter_percent = Decimal("0.01")
    settings.trading.max_spread_cap_percent = Decimal("1.0")
    settings.trading.depth_gate_enabled = True
    settings.trading.depth_gate_mode = "IMPACT"
    settings.trading.depth_gate_levels = 20
    settings.trading.depth_gate_max_price_impact_percent = Decimal("0.0015")
    settings.trading.min_l1_notional_usd = Decimal("1")
    settings.trading.min_l1_notional_multiple = Decimal("0")
    settings.trading.max_l1_qty_utilization = Decimal("1.0")
    settings.trading.leverage_multiplier = Decimal("1")
    settings.trading.x10_leverage_multiplier = Decimal("1")
    settings.trading.max_open_trades = 5

    settings.risk.max_exposure_pct = Decimal("100")
    settings.risk.max_trade_size_pct = Decimal("100")

    settings.execution.ws_fill_wait_enabled = False
    settings.execution.hedge_depth_preflight_enabled = False
    settings.execution.hedge_depth_salvage_enabled = False
    settings.execution.smart_pricing_enabled = False
    settings.execution.hedge_submit_immediately_on_fill_enabled = True

    settings.execution.maker_order_max_retries = 1
    settings.execution.maker_order_timeout_seconds = 10
    settings.execution.microfill_max_unhedged_usd = Decimal("10")
    settings.execution.taker_order_slippage = Decimal("0.01")

    market_data = MagicMock()
    market_data.get_price.return_value.mid_price = Decimal("2000")
    market_data.get_price.return_value.lighter_price = Decimal("2000")
    market_data.get_price.return_value.x10_price = Decimal("2000")

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.step_size = Decimal("0.0001")
        info.tick_size = Decimal("0.01")
        info.min_qty = Decimal("0")
        info.lighter_max_leverage = Decimal("10")
        info.x10_max_leverage = Decimal("20")
        return info

    market_data.get_market_info.side_effect = _market_info

    fresh_ob = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("1999"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1999"),
        x10_ask=Decimal("2001"),
        lighter_bid_qty=Decimal("10"),
        lighter_ask_qty=Decimal("10"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("10"),
    )
    market_data.get_orderbook.return_value = fresh_ob
    market_data.get_fresh_orderbook = AsyncMock(return_value=fresh_ob)
    market_data.get_fresh_orderbook_depth = AsyncMock(
        return_value=OrderbookDepthSnapshot(
            symbol="ETH",
            lighter_bids=[(Decimal("1999"), Decimal("10"))],
            lighter_asks=[(Decimal("2001"), Decimal("10"))],
            x10_bids=[(Decimal("1999"), Decimal("10"))],
            x10_asks=[(Decimal("2001"), Decimal("10"))],
        )
    )

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    async def _leg1_fill(trade, _opp, attempt_id=None):
        trade.execution_state = ExecutionState.LEG1_FILLED
        trade.leg1.order_id = "l1"
        trade.leg1.qty = trade.target_qty
        trade.leg1.filled_qty = trade.target_qty
        trade.leg1.entry_price = Decimal("2000")
        trade.leg1.fees = Decimal("0")

    async def _leg2_fill(trade, _opp, leg1_filled_monotonic=None, attempt_id=None):
        trade.execution_state = ExecutionState.LEG2_SUBMITTED
        trade.leg2.order_id = "l2"
        trade.leg2.qty = trade.target_qty
        trade.leg2.filled_qty = trade.target_qty
        trade.leg2.entry_price = Decimal("2000")
        trade.leg2.fees = Decimal("0")

    engine._execute_leg1 = AsyncMock(side_effect=_leg1_fill)
    engine._execute_leg2 = AsyncMock(side_effect=_leg2_fill)

    opp = Opportunity(
        symbol="ETH",
        timestamp=datetime.now(),
        lighter_rate=Decimal("0.0"),
        x10_rate=Decimal("0.0"),
        net_funding_hourly=Decimal("0.0"),
        apy=Decimal("0.5"),
        spread_pct=Decimal("0.0"),
        mid_price=Decimal("2000"),
        lighter_best_bid=fresh_ob.lighter_bid,
        lighter_best_ask=fresh_ob.lighter_ask,
        x10_best_bid=fresh_ob.x10_bid,
        x10_best_ask=fresh_ob.x10_ask,
        suggested_qty=Decimal("0.1"),
        suggested_notional=Decimal("200"),
        expected_value_usd=Decimal("1.0"),
        breakeven_hours=Decimal("1.0"),
        long_exchange=Exchange.LIGHTER,
        short_exchange=Exchange.X10,
    )

    result = await engine.execute(opp)

    assert result.success is True
    # One depth snapshot is fetched up-front for IMPACT mode; fast hedge mode must not fetch again post-Leg1.
    assert market_data.get_fresh_orderbook_depth.await_count == 1
