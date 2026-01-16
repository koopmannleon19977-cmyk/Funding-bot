
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    Opportunity,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.services.execution import ExecutionEngine


@pytest.mark.asyncio
class TestExecutionFlow:

    async def test_successful_execution_flow(self):
        """Verify state transitions for a successful trade."""
        # Setup Mocks
        lighter = AsyncMock()
        x10 = AsyncMock()
        store = AsyncMock()
        event_bus = AsyncMock()

        # Market Data Mock - Base MagicMock (Sync)
        market_data = MagicMock()
        # Explicitly make strict async methods AsyncMock
        market_data.get_fresh_orderbook = AsyncMock()

        settings = MagicMock()

        # Configure Mocks
        lighter.get_available_balance.return_value.available = Decimal("1000")
        lighter.get_available_balance.return_value.total = Decimal("1000")
        x10.get_available_balance.return_value.available = Decimal("1000")
        x10.get_available_balance.return_value.total = Decimal("1000")
        store.list_open_trades.return_value = [] # BEFORE execution

        # Configure Settings
        settings.trading.max_spread_filter_percent = Decimal("0.01")
        settings.trading.max_spread_cap_percent = Decimal("1.0")
        settings.trading.depth_gate_enabled = False
        settings.trading.leverage_multiplier = Decimal("1")
        settings.trading.x10_leverage_multiplier = Decimal("1")
        settings.trading.max_open_trades = 5
        settings.risk.max_exposure_pct = Decimal("100")
        settings.risk.max_trade_size_pct = Decimal("100")
        settings.execution.maker_order_max_retries = 1
        settings.execution.maker_order_timeout_seconds = 10
        settings.execution.microfill_max_unhedged_usd = Decimal("10")
        settings.execution.taker_order_slippage = Decimal("0.01")

        # Market Data Mocks - SYNC Return Values (accessible directly)
        market_data.get_market_info.return_value.step_size = Decimal("0.0001")
        market_data.get_market_info.return_value.tick_size = Decimal("0.01")
        market_data.get_market_info.return_value.min_qty = Decimal("0")
        market_data.get_market_info.return_value.lighter_max_leverage = Decimal("10")
        market_data.get_market_info.return_value.x10_max_leverage = Decimal("20")

        market_data.get_price.return_value.mid_price = Decimal("2000")
        market_data.get_price.return_value.lighter_price = Decimal("2000")
        market_data.get_price.return_value.x10_price = Decimal("2000")
        fresh_ob = MagicMock()
        market_data.get_fresh_orderbook.return_value = fresh_ob
        fresh_ob.entry_spread_pct.return_value = Decimal("0.001") # Low spread
        fresh_ob.lighter_bid = Decimal("1999")
        fresh_ob.lighter_ask = Decimal("2001")
        fresh_ob.x10_bid = Decimal("1999")
        fresh_ob.x10_ask = Decimal("2001")
        market_data.get_orderbook.return_value = fresh_ob

        # Order Placement Mocks
        # Leg 1
        leg1_order = Order(
            order_id="l1_id",
            symbol="ETH",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            qty=Decimal("1"),
            status=OrderStatus.FILLED,
            order_type=OrderType.LIMIT,
            avg_fill_price=Decimal("2000"),
            filled_qty=Decimal("1"),
            fee=Decimal("1")
        )
        lighter.place_order.return_value = leg1_order
        lighter.get_order.return_value = leg1_order # Immediate fill for test

        # Leg 2
        leg2_order = Order(
            order_id="l2_id",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("1"),
            status=OrderStatus.FILLED,
            order_type=OrderType.LIMIT,
            avg_fill_price=Decimal("2000"),
            filled_qty=Decimal("1"),
            fee=Decimal("1")
        )
        x10.place_order.return_value = leg2_order
        x10.get_order.return_value = leg2_order
        lighter.get_position.return_value = Position(
            symbol="ETH",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            qty=Decimal("1"),
            entry_price=Decimal("2000"),
        )
        x10.get_position.return_value = Position(
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("1"),
            entry_price=Decimal("2000"),
        )

        # Engine
        engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

        # Opportunity
        opp = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(),
            lighter_rate=Decimal("0"),
            x10_rate=Decimal("0.001"),
            net_funding_hourly=Decimal("0.001"),
            apy=Decimal("0.5"), # 50%
            spread_pct=Decimal("0.001"),
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1999"),
            lighter_best_ask=Decimal("2001"),
            x10_best_bid=Decimal("1999"),
            x10_best_ask=Decimal("2001"),
            suggested_qty=Decimal("1"),
            suggested_notional=Decimal("2000"),
            expected_value_usd=Decimal("10"),
            breakeven_hours=Decimal("10"),
            long_exchange=Exchange.LIGHTER,
            short_exchange=Exchange.X10
        )

        # Patch internal wait methods to avoid sleep
        engine._wait_for_fill = AsyncMock(return_value=leg1_order)
        # We need to handle the second call for Leg 2 differently if needed,
        # but AsyncMock return_value is static unless side_effect used.
        # Let's use side_effect for _wait_for_fill
        engine._wait_for_fill.side_effect = [leg1_order, leg2_order]

        # Execute
        result = await engine.execute(opp)

        # Validation
        if not result.success:
            print(f"Execution Failed: {result.error}")
        assert result.success is True, f"Execution failed: {result.error}"
        assert result.trade is not None
        assert result.trade.status == TradeStatus.OPEN
        assert result.trade.execution_state == ExecutionState.COMPLETE
        assert result.trade.leg1.filled_qty == Decimal("1")
        assert result.trade.leg2.filled_qty == Decimal("1")

    async def test_rollback_on_leg2_failure(self):
        """Verify rollback triggers if Leg 2 fails."""
        # Setup Mocks (Similar to above)
        lighter = AsyncMock()
        x10 = AsyncMock()
        store = AsyncMock()
        event_bus = AsyncMock()

        # Market Data Mock (MagicMock Base)
        market_data = MagicMock()
        market_data.get_fresh_orderbook = AsyncMock()

        settings = MagicMock()

        # ... (Balance & Market Data Mocks same as above) ...
        lighter.get_available_balance.return_value.available = Decimal("1000")
        lighter.get_available_balance.return_value.total = Decimal("1000")
        x10.get_available_balance.return_value.available = Decimal("1000")
        x10.get_available_balance.return_value.total = Decimal("1000")
        store.list_open_trades.return_value = []

        # Configure Settings
        settings.trading.max_spread_filter_percent = Decimal("0.01")
        settings.trading.max_spread_cap_percent = Decimal("1.0")
        settings.trading.depth_gate_enabled = False
        settings.trading.leverage_multiplier = Decimal("1")
        settings.trading.x10_leverage_multiplier = Decimal("1")
        settings.trading.max_open_trades = 5
        settings.risk.max_exposure_pct = Decimal("100")
        settings.risk.max_trade_size_pct = Decimal("100")
        settings.execution.maker_order_max_retries = 1
        settings.execution.maker_order_timeout_seconds = 10
        settings.execution.microfill_max_unhedged_usd = Decimal("10")
        settings.execution.taker_order_slippage = Decimal("0.01")

        # Market Data Sync Returns
        market_data.get_market_info.return_value.step_size = Decimal("0.0001")
        market_data.get_market_info.return_value.tick_size = Decimal("0.01") # FIX
        market_data.get_market_info.return_value.min_qty = Decimal("0") # FIX
        market_data.get_market_info.return_value.lighter_max_leverage = Decimal("10")
        market_data.get_market_info.return_value.x10_max_leverage = Decimal("20")

        market_data.get_price.return_value.mid_price = Decimal("2000")
        market_data.get_price.return_value.lighter_price = Decimal("2000")
        market_data.get_price.return_value.x10_price = Decimal("2000")

        fresh_ob = MagicMock()
        market_data.get_fresh_orderbook.return_value = fresh_ob
        fresh_ob.entry_spread_pct.return_value = Decimal("0.001")
        fresh_ob.lighter_bid = Decimal("1999")
        fresh_ob.lighter_ask = Decimal("2001")
        fresh_ob.x10_bid = Decimal("1999")
        fresh_ob.x10_ask = Decimal("2001")
        market_data.get_orderbook.return_value = fresh_ob

        # Engine
        engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

        # Orders
        leg1_order = Order(
            order_id="l1_id", symbol="ETH", exchange=Exchange.LIGHTER, side=Side.BUY,
            qty=Decimal("1"), status=OrderStatus.FILLED, order_type=OrderType.LIMIT, avg_fill_price=Decimal("2000"), filled_qty=Decimal("1"), fee=Decimal("1")
        )

        # Mock Leg 1 Fill
        lighter.place_order.return_value = leg1_order

        # Mock Leg 2 FAIL
        x10.place_order.side_effect = Exception("API Error")

        # Mock Rollback Order
        rollback_order = Order(
            order_id="rb_id", symbol="ETH", exchange=Exchange.LIGHTER, side=Side.SELL,
            qty=Decimal("1"), status=OrderStatus.FILLED, order_type=OrderType.LIMIT, avg_fill_price=Decimal("1995"), filled_qty=Decimal("1"), fee=Decimal("1")
        )

        # Helper mocks
        engine._wait_for_fill = AsyncMock(side_effect=[leg1_order, rollback_order])

        # Opportunity (Same dummy data)
        opp = Opportunity(
            symbol="ETH",
            timestamp=datetime.now(),
            lighter_rate=Decimal("0"),
            x10_rate=Decimal("0.001"),
            net_funding_hourly=Decimal("0.001"),
            apy=Decimal("0.5"),
            spread_pct=Decimal("0.001"),
            mid_price=Decimal("2000"),
            lighter_best_bid=Decimal("1999"),
            lighter_best_ask=Decimal("2001"),
            x10_best_bid=Decimal("1999"),
            x10_best_ask=Decimal("2001"),
            suggested_qty=Decimal("1"),
            suggested_notional=Decimal("2000"),
            expected_value_usd=Decimal("10"),
            breakeven_hours=Decimal("10"),
            long_exchange=Exchange.LIGHTER,
            short_exchange=Exchange.X10
        )

        # Execute
        result = await engine.execute(opp)

        # Validation
        assert result.success is False
        assert result.trade.status == TradeStatus.FAILED
        # Check that rollback was called (Leg 1 closed)
        assert result.trade.execution_state == ExecutionState.ROLLBACK_DONE
        assert result.trade.leg1.filled_qty == Decimal("1")
        # Ensure lighter placed a SELL order for rollback (original was BUY)
        # Check calls to lighter.place_order. Should be 2 (Entry + Rollback)
        assert lighter.place_order.call_count == 2

        # Verify Rollback args
        rollback_call = lighter.place_order.call_args_list[1]
        req = rollback_call[0][0]
        assert req.side == Side.SELL
        assert req.reduce_only is True

    async def test_pre_attempt_position_assimilation_does_not_double_count_fill(self):
        lighter = AsyncMock()
        x10 = AsyncMock()
        store = AsyncMock()
        event_bus = AsyncMock()

        settings = MagicMock()
        settings.execution.smart_pricing_enabled = False
        settings.execution.maker_order_max_retries = 2
        settings.execution.maker_order_timeout_seconds = 10
        settings.execution.maker_attempt_timeout_min_seconds = 0.01
        settings.execution.maker_attempt_timeout_schedule = "equal"
        settings.execution.maker_order_max_aggressiveness = Decimal("1.0")
        settings.execution.maker_force_post_only = True
        settings.execution.escalate_to_taker_enabled = False
        settings.execution.escalate_to_taker_after_seconds = 0
        settings.execution.escalate_to_taker_slippage = Decimal("0")
        settings.execution.escalate_to_taker_fill_timeout_seconds = 0
        settings.execution.microfill_max_unhedged_usd = Decimal("0")

        market_data = MagicMock()
        market_data.get_market_info.return_value.tick_size = Decimal("0.01")
        market_data.get_market_info.return_value.step_size = Decimal("0.0001")
        market_data.get_market_info.return_value.min_qty = Decimal("0")

        price = MagicMock()
        price.lighter_price = Decimal("100")
        price.mid_price = Decimal("100")
        market_data.get_price.return_value = price

        ob = MagicMock()
        ob.lighter_bid = Decimal("99")
        ob.lighter_ask = Decimal("101")
        ob.lighter_bid_qty = Decimal("1000")
        ob.lighter_ask_qty = Decimal("1000")
        market_data.get_orderbook.return_value = ob

        lighter.ensure_orderbook_ws = AsyncMock()
        lighter.get_market_info = AsyncMock(return_value=MagicMock(step_size=Decimal("0.0001")))

        lighter.get_position = AsyncMock(
            side_effect=[
                None,  # initial snapshot
                Position(symbol="TEST", exchange=Exchange.LIGHTER, side=Side.BUY, qty=Decimal("0"), entry_price=Decimal("0")),  # ghost fill check (attempt 1)
                Position(symbol="TEST", exchange=Exchange.LIGHTER, side=Side.BUY, qty=Decimal("1"), entry_price=Decimal("0")),  # pre-attempt (attempt 2)
                Position(symbol="TEST", exchange=Exchange.LIGHTER, side=Side.BUY, qty=Decimal("1"), entry_price=Decimal("0")),  # reconcile
            ]
        )

        placed = Order(
            order_id="o1",
            symbol="TEST",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("2"),
            price=Decimal("100"),
            status=OrderStatus.OPEN,
        )
        lighter.place_order.return_value = placed
        lighter.modify_order = AsyncMock(return_value=True)
        lighter.get_order = AsyncMock(
            return_value=Order(
                order_id="o1",
                symbol="TEST",
                exchange=Exchange.LIGHTER,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                qty=Decimal("1"),
                price=Decimal("100"),
                status=OrderStatus.FILLED,
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
                fee=Decimal("0"),
            )
        )

        engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

        opp = Opportunity(
            symbol="TEST",
            timestamp=datetime.now(),
            lighter_rate=Decimal("0"),
            x10_rate=Decimal("0"),
            net_funding_hourly=Decimal("0"),
            apy=Decimal("0"),
            spread_pct=Decimal("0"),
            mid_price=Decimal("100"),
            lighter_best_bid=Decimal("99"),
            lighter_best_ask=Decimal("101"),
            x10_best_bid=Decimal("99"),
            x10_best_ask=Decimal("101"),
            suggested_qty=Decimal("2"),
            suggested_notional=Decimal("200"),
            expected_value_usd=Decimal("0"),
            breakeven_hours=Decimal("0"),
            long_exchange=Exchange.LIGHTER,
            short_exchange=Exchange.X10,
        )

        trade = Trade.create(
            symbol="TEST",
            leg1_exchange=Exchange.LIGHTER,
            leg1_side=Side.BUY,
            leg2_exchange=Exchange.X10,
            target_qty=Decimal("2"),
            target_notional_usd=Decimal("200"),
        )
        trade.leg1.qty = Decimal("2")
        trade.leg2.qty = Decimal("2")

        # Attempt 1: no fill observed. Attempt 2: order snapshot reports filled_qty=1, but that fill was
        # already assimilated via position delta; must NOT be double-counted.
        engine._wait_for_fill = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                Order(
                    order_id="o1",
                    symbol="TEST",
                    exchange=Exchange.LIGHTER,
                    side=Side.BUY,
                    order_type=OrderType.LIMIT,
                    qty=Decimal("2"),
                    price=Decimal("100"),
                    status=OrderStatus.OPEN,
                    filled_qty=Decimal("0"),
                    avg_fill_price=Decimal("0"),
                    fee=Decimal("0"),
                ),
                Order(
                    order_id="o1",
                    symbol="TEST",
                    exchange=Exchange.LIGHTER,
                    side=Side.BUY,
                    order_type=OrderType.LIMIT,
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    status=OrderStatus.FILLED,
                    filled_qty=Decimal("1"),
                    avg_fill_price=Decimal("100"),
                    fee=Decimal("0"),
                ),
            ]
        )

        await engine._execute_leg1(trade, opp)

        assert trade.execution_state == ExecutionState.LEG1_FILLED
        assert trade.leg1.filled_qty == Decimal("1")
        assert trade.target_qty == Decimal("1")

