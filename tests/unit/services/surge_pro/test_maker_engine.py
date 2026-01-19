"""Tests for Surge Pro Maker Engine."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from funding_bot.services.surge_pro.maker_engine import SurgeProMakerEngine


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.surge_pro = MagicMock()
    settings.surge_pro.order_mode = "maker"
    settings.surge_pro.symbols = ["BTC", "ETH"]
    settings.surge_pro.entry_imbalance_threshold = Decimal("0.25")
    settings.surge_pro.exit_imbalance_threshold = Decimal("0.10")
    settings.surge_pro.max_spread_bps = Decimal("10")
    settings.surge_pro.maker_entry_timeout_s = 2.0
    settings.surge_pro.maker_exit_timeout_s = 1.5
    settings.surge_pro.maker_exit_max_retries = 3
    settings.surge_pro.max_trade_notional_usd = Decimal("200")
    settings.surge_pro.max_open_trades = 3
    settings.surge_pro.daily_loss_cap_usd = Decimal("5")
    settings.surge_pro.hourly_loss_pause_usd = Decimal("2")
    settings.surge_pro.loss_streak_pause_count = 5
    settings.surge_pro.min_fill_rate_percent = 20
    settings.surge_pro.pause_duration_minutes = 30
    settings.surge_pro.stop_loss_bps = Decimal("20")
    settings.surge_pro.take_profit_bps = Decimal("15")
    settings.surge_pro.cooldown_seconds = 5
    settings.surge_pro.paper_mode = False
    settings.live_trading = True
    return settings


@pytest.fixture
def mock_x10():
    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            base_asset="BTC",
            symbol="BTC-USD",
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }
    return x10


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestMakerEntry:
    """Test maker entry flow."""

    @pytest.mark.asyncio
    async def test_entry_uses_post_only_order(self, mock_settings, mock_x10, mock_store):
        """Entry should use POST_ONLY time-in-force."""
        # Setup orderbook with imbalance
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50005"), Decimal("0.3"))],  # More bids than asks = buy signal
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(
            return_value={
                "best_bid": "50000",
                "best_ask": "50005",
            }
        )

        # Capture placed order
        placed_order = None

        async def capture_order(request):
            nonlocal placed_order
            placed_order = request
            return Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50001"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50001"),
                filled_qty=Decimal("0.004"),
            )
        )
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_store.list_open_surge_trades = AsyncMock(return_value=[])
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Trigger entry
        await engine._maybe_enter_maker("BTC")

        # Verify POST_ONLY was used
        assert placed_order is not None
        assert placed_order.time_in_force == TimeInForce.POST_ONLY

    @pytest.mark.asyncio
    async def test_no_entry_when_imbalance_below_threshold(self, mock_settings, mock_x10, mock_store):
        """Should not enter when imbalance is below threshold."""
        # Setup orderbook with weak imbalance (below 0.25 threshold)
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50005"), Decimal("0.9"))],  # Only slight imbalance
            }
        )

        mock_x10.place_order = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        # Should not place any order
        mock_x10.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_direction_from_imbalance(self, mock_settings, mock_x10, mock_store):
        """Positive imbalance (more bids) should trigger BUY, negative should trigger SELL."""
        # Test BUY signal (more bids than asks)
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50005"), Decimal("0.2"))],
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        placed_side = None

        async def capture_side(request):
            nonlocal placed_side
            placed_side = request.side
            return Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_side
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50001"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50001"),
                filled_qty=Decimal("0.004"),
            )
        )
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        assert placed_side == Side.BUY

    @pytest.mark.asyncio
    async def test_sell_signal_when_more_asks(self, mock_settings, mock_x10, mock_store):
        """Negative imbalance (more asks) should trigger SELL."""
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("0.2"))],
                "asks": [(Decimal("50005"), Decimal("1.0"))],  # More asks than bids
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        placed_side = None

        async def capture_side(request):
            nonlocal placed_side
            placed_side = request.side
            return Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_side
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50004"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50004"),
                filled_qty=Decimal("0.004"),
            )
        )
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        assert placed_side == Side.SELL

    @pytest.mark.asyncio
    async def test_no_entry_when_spread_too_wide(self, mock_settings, mock_x10, mock_store):
        """Should not enter when spread exceeds max_spread_bps."""
        # Strong imbalance but wide spread (50 bps vs 10 bps max)
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50250"), Decimal("0.2"))],  # 50 bps spread
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50250"})

        mock_x10.place_order = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        mock_x10.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_unfilled_order_is_cancelled(self, mock_settings, mock_x10, mock_store):
        """When order doesn't fill within timeout, it should be cancelled."""
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50005"), Decimal("0.2"))],
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        order = Order(
            order_id="123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.004"),
            price=Decimal("50001"),
            time_in_force=TimeInForce.POST_ONLY,
            status=OrderStatus.OPEN,  # Not filled
            avg_fill_price=Decimal("0"),
            filled_qty=Decimal("0"),
        )

        async def place_order(request):
            return order

        mock_x10.place_order = place_order
        mock_x10.get_order = AsyncMock(return_value=order)  # Always returns OPEN status
        mock_x10.cancel_order = AsyncMock()

        # Use very short timeout
        mock_settings.surge_pro.maker_entry_timeout_s = 0.1

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        # Should have tried to cancel
        mock_x10.cancel_order.assert_called()

    @pytest.mark.asyncio
    async def test_creates_surge_trade_on_fill(self, mock_settings, mock_x10, mock_store):
        """Should create SurgeTrade record when order fills."""
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1.0"))],
                "asks": [(Decimal("50005"), Decimal("0.2"))],
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        async def place_order(request):
            return Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = place_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50001"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50001"),
                filled_qty=Decimal("0.004"),
            )
        )
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine._maybe_enter_maker("BTC")

        mock_store.create_surge_trade.assert_called_once()
        trade = mock_store.create_surge_trade.call_args[0][0]
        assert trade.symbol == "BTC"
        assert trade.exchange == Exchange.X10
        assert trade.side == Side.BUY


class TestImbalanceCalculation:
    """Test orderbook imbalance calculation."""

    def test_positive_imbalance_more_bids(self):
        """More bid volume should yield positive imbalance."""
        from funding_bot.services.surge_pro.maker_engine import compute_imbalance

        bids = [(Decimal("100"), Decimal("10"))]
        asks = [(Decimal("101"), Decimal("5"))]

        imbalance = compute_imbalance(bids, asks, 10)

        # (10 - 5) / (10 + 5) = 5 / 15 = 0.333...
        assert imbalance > Decimal("0")
        assert abs(imbalance - Decimal("0.333333")) < Decimal("0.001")

    def test_negative_imbalance_more_asks(self):
        """More ask volume should yield negative imbalance."""
        from funding_bot.services.surge_pro.maker_engine import compute_imbalance

        bids = [(Decimal("100"), Decimal("5"))]
        asks = [(Decimal("101"), Decimal("10"))]

        imbalance = compute_imbalance(bids, asks, 10)

        # (5 - 10) / (5 + 10) = -5 / 15 = -0.333...
        assert imbalance < Decimal("0")

    def test_zero_imbalance_equal_volumes(self):
        """Equal volumes should yield zero imbalance."""
        from funding_bot.services.surge_pro.maker_engine import compute_imbalance

        bids = [(Decimal("100"), Decimal("10"))]
        asks = [(Decimal("101"), Decimal("10"))]

        imbalance = compute_imbalance(bids, asks, 10)

        assert imbalance == Decimal("0")

    def test_empty_orderbook_returns_zero(self):
        """Empty orderbook should return zero imbalance."""
        from funding_bot.services.surge_pro.maker_engine import compute_imbalance

        imbalance = compute_imbalance([], [], 10)

        assert imbalance == Decimal("0")


class TestSpreadCalculation:
    """Test spread calculation in basis points."""

    def test_spread_calculation(self):
        """Spread should be calculated correctly in bps."""
        from funding_bot.services.surge_pro.maker_engine import compute_spread_bps

        spread = compute_spread_bps(Decimal("100"), Decimal("100.10"))

        # (100.10 - 100) / 100 * 10000 = 10 bps
        assert spread == Decimal("10")

    def test_tight_spread(self):
        """Tight spread should be small."""
        from funding_bot.services.surge_pro.maker_engine import compute_spread_bps

        spread = compute_spread_bps(Decimal("50000"), Decimal("50001"))

        # (1 / 50000) * 10000 = 0.2 bps
        assert spread == Decimal("0.2")

    def test_zero_bid_returns_large_spread(self):
        """Zero bid should return very large spread to prevent entry."""
        from funding_bot.services.surge_pro.maker_engine import compute_spread_bps

        spread = compute_spread_bps(Decimal("0"), Decimal("100"))

        assert spread == Decimal("999999")


class TestMakerExit:
    """Test maker exit flow with taker fallback."""

    @pytest.mark.asyncio
    async def test_exit_tries_maker_first(self, mock_settings, mock_x10, mock_store):
        """Exit should attempt POST_ONLY first."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_x10.get_orderbook_l1 = AsyncMock(
            return_value={
                "best_bid": "50000",
                "best_ask": "50005",
            }
        )
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50002"))

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id=f"order_{len(placed_orders)}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="order_1",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50004"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50004"),
                filled_qty=Decimal("0.004"),
            )
        )
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Create open trade (LONG)
        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        # Exit
        await engine._close_trade_maker(trade, "SIGNAL_FLIP")

        # First order should be POST_ONLY
        assert len(placed_orders) >= 1
        assert placed_orders[0].time_in_force == TimeInForce.POST_ONLY
        assert placed_orders[0].side == Side.SELL  # Opposite of LONG

    @pytest.mark.asyncio
    async def test_exit_falls_back_to_taker(self, mock_settings, mock_x10, mock_store):
        """Exit should use IOC taker after maker retries exhausted."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.maker_exit_max_retries = 2

        mock_x10.get_orderbook_l1 = AsyncMock(
            return_value={
                "best_bid": "50000",
                "best_ask": "50005",
            }
        )
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50002"))
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="unfilled",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50004"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.OPEN,  # Never fills
            )
        )
        mock_x10.cancel_order = AsyncMock()

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id=f"order_{len(placed_orders)}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED if request.time_in_force == TimeInForce.IOC else OrderStatus.OPEN,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        # Use short timeout for test
        mock_settings.surge_pro.maker_exit_timeout_s = 0.1

        await engine._close_trade_maker(trade, "SIGNAL_FLIP")

        # Should have maker attempts + final taker
        taker_orders = [o for o in placed_orders if o.time_in_force == TimeInForce.IOC]
        assert len(taker_orders) >= 1, "Should fall back to taker"

    @pytest.mark.asyncio
    async def test_exit_updates_trade_record(self, mock_settings, mock_x10, mock_store):
        """Exit should update the trade record with exit details."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_x10.get_orderbook_l1 = AsyncMock(
            return_value={
                "best_bid": "50000",
                "best_ask": "50005",
            }
        )

        async def place_order(request):
            return Order(
                order_id="exit_123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50004"),
                filled_qty=request.qty,
                fee=Decimal("0.02"),
            )

        mock_x10.place_order = place_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="exit_123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.004"),
                price=Decimal("50004"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50004"),
                filled_qty=Decimal("0.004"),
                fee=Decimal("0.02"),
            )
        )

        updated_trades = []

        async def capture_update(trade):
            updated_trades.append(trade)

        mock_store.update_surge_trade = capture_update

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            fees=Decimal("0.01"),  # Entry fee
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        await engine._close_trade_maker(trade, "SIGNAL_FLIP")

        # Trade should be updated
        assert len(updated_trades) == 1
        closed_trade = updated_trades[0]
        assert closed_trade.status == SurgeTradeStatus.CLOSED
        assert closed_trade.exit_price == Decimal("50004")
        assert closed_trade.exit_reason == "SIGNAL_FLIP"
        assert closed_trade.exit_order_id == "exit_123"
        assert closed_trade.closed_at is not None

    @pytest.mark.asyncio
    async def test_exit_taker_uses_aggressive_price(self, mock_settings, mock_x10, mock_store):
        """Taker fallback should use aggressive price with slippage."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_x10.get_orderbook_l1 = AsyncMock(
            return_value={
                "best_bid": "50000",
                "best_ask": "50005",
            }
        )

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id=f"order_{len(placed_orders)}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # LONG trade exits by SELLing - should use discounted bid price
        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        await engine._close_trade_taker(trade, "TAKER_FALLBACK")

        # Should place IOC order
        assert len(placed_orders) == 1
        taker_order = placed_orders[0]
        assert taker_order.time_in_force == TimeInForce.IOC
        assert taker_order.side == Side.SELL
        # Price should be below best_bid (aggressive sell)
        assert taker_order.price < Decimal("50000")


class TestMainLoop:
    """Test main tick loop."""

    @pytest.mark.asyncio
    async def test_tick_checks_risk_guard_first(self, mock_settings, mock_x10, mock_store):
        """Main tick should check RiskGuard before entering."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-6.00"))  # Over cap
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
        mock_store.list_open_surge_trades = AsyncMock(return_value=[])
        mock_x10.list_positions = AsyncMock(return_value=[])

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Should not place any orders when risk blocked
        mock_x10.place_order = AsyncMock()

        await engine.tick()

        mock_x10.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_respects_max_open_trades(self, mock_settings, mock_x10, mock_store):
        """Tick should not enter when at max open trades."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.max_open_trades = 2

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Already at max trades - both with prices near entry (no exit trigger)
        open_trades = [
            SurgeTrade(
                trade_id="t1",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("0.01"),
                entry_price=Decimal("50000"),
                status=SurgeTradeStatus.OPEN,
                opened_at=datetime.now(UTC),
            ),
            SurgeTrade(
                trade_id="t2",
                symbol="ETH",
                exchange=Exchange.X10,
                side=Side.SELL,
                qty=Decimal("0.1"),
                entry_price=Decimal("3000"),
                status=SurgeTradeStatus.OPEN,
                opened_at=datetime.now(UTC),
            ),
        ]
        mock_store.list_open_surge_trades = AsyncMock(return_value=open_trades)
        mock_x10.list_positions = AsyncMock(return_value=[])

        # Return prices near entry (no SL/TP trigger)
        async def get_mark_price(symbol):
            if symbol == "BTC":
                return Decimal("50000")
            elif symbol == "ETH":
                return Decimal("3000")
            return Decimal("100")

        mock_x10.get_mark_price = get_mark_price
        # Balanced orderbook (no signal flip)
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("100"), Decimal("10"))],
                "asks": [(Decimal("101"), Decimal("10"))],  # Equal = no flip
            }
        )
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        mock_x10.place_order = AsyncMock()

        await engine.tick()

        # Should not place new entry orders (at max)
        mock_x10.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_skips_symbol_already_in_trade(self, mock_settings, mock_x10, mock_store):
        """Tick should not enter same symbol twice."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.max_open_trades = 3
        mock_settings.surge_pro.symbols = ["BTC"]

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Already have BTC trade
        open_trades = [
            SurgeTrade(
                trade_id="t1",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("0.01"),
                entry_price=Decimal("50000"),
                status=SurgeTradeStatus.OPEN,
                opened_at=datetime.now(UTC),
            ),
        ]
        mock_store.list_open_surge_trades = AsyncMock(return_value=open_trades)
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50000"))
        mock_x10.get_orderbook_depth = AsyncMock(return_value={"bids": [], "asks": []})
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        mock_x10.place_order = AsyncMock()

        await engine.tick()

        mock_x10.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_respects_cooldown(self, mock_settings, mock_x10, mock_store):
        """Tick should skip symbols on cooldown."""
        mock_settings.surge_pro.max_open_trades = 3
        mock_settings.surge_pro.symbols = ["BTC"]
        mock_settings.surge_pro.cooldown_seconds = 60

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
        mock_store.list_open_surge_trades = AsyncMock(return_value=[])

        mock_x10.list_positions = AsyncMock(return_value=[])

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Set BTC on cooldown
        import time

        engine._cooldowns["BTC"] = time.time() + 60

        mock_x10.place_order = AsyncMock()

        await engine.tick()

        mock_x10.place_order.assert_not_called()


class TestExitLogic:
    """Test exit condition evaluation."""

    @pytest.mark.asyncio
    async def test_exit_on_stop_loss(self, mock_settings, mock_x10, mock_store):
        """Should exit immediately on stop loss hit."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.stop_loss_bps = Decimal("20")  # 20 bps = 0.2%

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Long trade at 50000, current price at 49850 (30 bps down - stop hit)
        trade = SurgeTrade(
            trade_id="t1",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.01"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        mock_store.list_open_surge_trades = AsyncMock(return_value=[trade])
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("49850"))  # 30 bps down
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "49850", "best_ask": "49855"})
        mock_x10.get_orderbook_depth = AsyncMock(return_value={"bids": [], "asks": []})

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id="exit_order",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine.tick()

        # Should have placed exit order (IOC for stop loss)
        assert len(placed_orders) >= 1
        # Stop loss uses taker (IOC)
        assert any(o.time_in_force == TimeInForce.IOC for o in placed_orders)

    @pytest.mark.asyncio
    async def test_exit_on_take_profit(self, mock_settings, mock_x10, mock_store):
        """Should exit on take profit hit."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.take_profit_bps = Decimal("15")

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Long trade at 50000, current price at 50100 (20 bps up - TP hit)
        trade = SurgeTrade(
            trade_id="t1",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.01"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        mock_store.list_open_surge_trades = AsyncMock(return_value=[trade])
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50100"))  # 20 bps up
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50100", "best_ask": "50105"})
        mock_x10.get_orderbook_depth = AsyncMock(return_value={"bids": [], "asks": []})

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id="exit_order",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="exit_order",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.01"),
                price=Decimal("50099"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50099"),
                filled_qty=Decimal("0.01"),
            )
        )
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine.tick()

        # Should have placed exit order (POST_ONLY for take profit)
        assert len(placed_orders) >= 1
        assert any(o.time_in_force == TimeInForce.POST_ONLY for o in placed_orders)

    @pytest.mark.asyncio
    async def test_exit_on_signal_flip(self, mock_settings, mock_x10, mock_store):
        """Should exit on signal flip (imbalance reversal)."""
        from datetime import UTC, datetime

        from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus

        mock_settings.surge_pro.exit_imbalance_threshold = Decimal("0.10")

        # Risk guard passes
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Long trade, but now imbalance is negative (more asks)
        trade = SurgeTrade(
            trade_id="t1",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.01"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        mock_store.list_open_surge_trades = AsyncMock(return_value=[trade])
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50000"))  # No P/L
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={"best_bid": "50000", "best_ask": "50005"})
        # Negative imbalance: more asks than bids = bearish = flip for LONG
        mock_x10.get_orderbook_depth = AsyncMock(
            return_value={
                "bids": [(Decimal("50000"), Decimal("1"))],
                "asks": [(Decimal("50005"), Decimal("5"))],  # 5x more asks
            }
        )

        placed_orders = []

        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id="exit_order",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_x10.get_order = AsyncMock(
            return_value=Order(
                order_id="exit_order",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.01"),
                price=Decimal("50004"),
                time_in_force=TimeInForce.POST_ONLY,
                status=OrderStatus.FILLED,
                avg_fill_price=Decimal("50004"),
                filled_qty=Decimal("0.01"),
            )
        )
        mock_store.update_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        await engine.tick()

        # Should have placed exit order
        assert len(placed_orders) >= 1
