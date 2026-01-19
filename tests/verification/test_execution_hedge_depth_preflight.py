from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import Exchange, Opportunity
from funding_bot.services.execution import ExecutionEngine
from funding_bot.services.market_data import OrderbookSnapshot


@pytest.mark.asyncio
async def test_hedge_depth_preflight_rejects_before_trade_persist_or_orders():
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

    settings = MagicMock()
    settings.live_trading = True
    settings.trading.max_spread_filter_percent = Decimal("0.01")
    settings.trading.max_spread_cap_percent = Decimal("1.0")
    settings.trading.depth_gate_enabled = True
    settings.trading.min_l1_notional_usd = Decimal("1000")
    settings.trading.min_l1_notional_multiple = Decimal("0")
    settings.trading.max_l1_qty_utilization = Decimal("1.0")
    settings.trading.leverage_multiplier = Decimal("1")
    settings.trading.x10_leverage_multiplier = Decimal("1")
    settings.trading.max_open_trades = 5
    settings.risk.max_exposure_pct = Decimal("100")
    settings.risk.max_trade_size_pct = Decimal("100")
    settings.execution.maker_order_max_retries = 1
    settings.execution.maker_order_timeout_seconds = 1
    settings.execution.microfill_max_unhedged_usd = Decimal("10")
    settings.execution.taker_order_slippage = Decimal("0.01")

    # Enable strict hedge preflight so it can fail while the base depth gate passes.
    settings.execution.hedge_depth_preflight_enabled = True
    settings.execution.hedge_depth_preflight_multiplier = Decimal("1.5")  # required notional becomes $1500
    settings.execution.hedge_depth_preflight_checks = 1
    settings.execution.hedge_depth_preflight_delay_seconds = Decimal("0")

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

    # Lighter SELL uses lighter_bid/lighter_bid_qty, X10 BUY uses x10_ask/x10_ask_qty.
    # Base depth gate requires >= $1000 notional -> pass with $1200.
    # Strict preflight multiplier 1.5 requires >= $1500 -> fail.
    fresh_ob = OrderbookSnapshot(
        symbol="ZEC",
        lighter_bid=Decimal("2000"),
        lighter_ask=Decimal("2001"),
        x10_bid=Decimal("1999"),
        x10_ask=Decimal("2000"),
        lighter_bid_qty=Decimal("0.6"),  # $1200
        lighter_ask_qty=Decimal("0.6"),
        x10_bid_qty=Decimal("0.6"),
        x10_ask_qty=Decimal("0.6"),  # $1200
    )
    market_data.get_fresh_orderbook = AsyncMock(return_value=fresh_ob)
    market_data.get_orderbook.return_value = fresh_ob

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    opp = Opportunity(
        symbol="ZEC",
        timestamp=datetime.now(),
        lighter_rate=Decimal("0.0001"),
        x10_rate=Decimal("0.0"),
        net_funding_hourly=Decimal("0.0001"),
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
        long_exchange=Exchange.X10,
        short_exchange=Exchange.LIGHTER,
    )

    result = await engine.execute(opp)

    assert result.success is False
    assert result.error == "Insufficient hedge depth"
    assert store.create_trade.await_count == 0
    assert lighter.place_order.await_count == 0
    assert x10.place_order.await_count == 0
