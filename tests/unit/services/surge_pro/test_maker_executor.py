"""Tests for MakerExecutor component."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from funding_bot.services.surge_pro.maker_executor import FillResult, MakerExecutor


class TestMakerPriceCalculation:
    """Test maker price calculation logic."""

    def test_buy_price_is_above_best_bid(self):
        """BUY maker price should be best_bid + 1 tick."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        price = executor.calc_maker_price(
            side=Side.BUY,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("100.10"),
            tick_size=Decimal("0.01"),
        )

        assert price == Decimal("100.01")

    def test_sell_price_is_below_best_ask(self):
        """SELL maker price should be best_ask - 1 tick."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        price = executor.calc_maker_price(
            side=Side.SELL,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("100.10"),
            tick_size=Decimal("0.01"),
        )

        assert price == Decimal("100.09")

    def test_price_respects_tick_size(self):
        """Price should be rounded to tick size."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        # With larger tick size
        price = executor.calc_maker_price(
            side=Side.BUY,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("101.00"),
            tick_size=Decimal("0.50"),
        )

        assert price == Decimal("100.50")

    def test_small_tick_size(self):
        """Price calculation works with small tick sizes (e.g., BTC)."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        price = executor.calc_maker_price(
            side=Side.BUY,
            best_bid=Decimal("50000.00"),
            best_ask=Decimal("50001.00"),
            tick_size=Decimal("0.10"),
        )

        assert price == Decimal("50000.10")

    def test_sell_price_with_tight_spread(self):
        """SELL price with 1-tick spread places at best bid level (crossing)."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        # Spread is exactly 1 tick
        price = executor.calc_maker_price(
            side=Side.SELL,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("100.01"),
            tick_size=Decimal("0.01"),
        )

        # SELL at best_ask - tick = 100.01 - 0.01 = 100.00
        assert price == Decimal("100.00")


class TestPlaceMakerOrder:
    """Test maker order placement."""

    async def test_place_maker_order_creates_correct_request(self):
        """place_maker_order should create POST_ONLY order request."""
        mock_x10 = MagicMock()
        mock_order = Order(
            order_id="test-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            price=Decimal("3000.00"),
            time_in_force=TimeInForce.POST_ONLY,
            status=OrderStatus.OPEN,
        )
        mock_x10.place_order = AsyncMock(return_value=mock_order)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)

        result = await executor.place_maker_order(
            symbol="ETH",
            side=Side.BUY,
            qty=Decimal("1.0"),
            price=Decimal("3000.00"),
        )

        assert result == mock_order
        mock_x10.place_order.assert_called_once()

        # Verify the request parameters
        call_args = mock_x10.place_order.call_args[0][0]
        assert call_args.symbol == "ETH"
        assert call_args.side == Side.BUY
        assert call_args.qty == Decimal("1.0")
        assert call_args.price == Decimal("3000.00")
        assert call_args.time_in_force == TimeInForce.POST_ONLY
        assert call_args.exchange == Exchange.X10

    async def test_place_maker_order_with_reduce_only(self):
        """place_maker_order should support reduce_only flag."""
        mock_x10 = MagicMock()
        mock_x10.place_order = AsyncMock(
            return_value=Order(
                order_id="test-456",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.1"),
                reduce_only=True,
            )
        )

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)

        await executor.place_maker_order(
            symbol="BTC",
            side=Side.SELL,
            qty=Decimal("0.1"),
            price=Decimal("60000.00"),
            reduce_only=True,
        )

        call_args = mock_x10.place_order.call_args[0][0]
        assert call_args.reduce_only is True


class TestPlaceTakerOrder:
    """Test taker fallback order placement."""

    async def test_place_taker_order_uses_ioc(self):
        """place_taker_order should create IOC order request."""
        mock_x10 = MagicMock()
        mock_order = Order(
            order_id="taker-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            price=Decimal("3010.00"),
            time_in_force=TimeInForce.IOC,
            status=OrderStatus.FILLED,
        )
        mock_x10.place_order = AsyncMock(return_value=mock_order)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)

        result = await executor.place_taker_order(
            symbol="ETH",
            side=Side.BUY,
            qty=Decimal("1.0"),
            price=Decimal("3010.00"),
        )

        assert result == mock_order
        call_args = mock_x10.place_order.call_args[0][0]
        assert call_args.time_in_force == TimeInForce.IOC


class TestWaitForFill:
    """Test fill waiting logic."""

    async def test_wait_for_fill_returns_immediately_on_filled(self):
        """wait_for_fill should return immediately when order is filled."""
        mock_x10 = MagicMock()
        filled_order = Order(
            order_id="fill-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.FILLED,
            avg_fill_price=Decimal("3000.50"),
        )
        mock_x10.get_order = AsyncMock(return_value=filled_order)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)
        order = Order(
            order_id="fill-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.OPEN,
        )

        result = await executor.wait_for_fill(order, timeout_s=5.0)

        assert result.filled is True
        assert result.order == filled_order
        assert result.fill_price == Decimal("3000.50")
        assert result.fill_time_ms is not None
        assert result.fill_time_ms >= 0

    async def test_wait_for_fill_returns_false_on_cancelled(self):
        """wait_for_fill should return filled=False when order is cancelled."""
        mock_x10 = MagicMock()
        cancelled_order = Order(
            order_id="cancel-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.CANCELLED,
        )
        mock_x10.get_order = AsyncMock(return_value=cancelled_order)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)
        order = Order(
            order_id="cancel-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.OPEN,
        )

        result = await executor.wait_for_fill(order, timeout_s=5.0)

        assert result.filled is False
        assert result.fill_price is None

    async def test_wait_for_fill_times_out(self):
        """wait_for_fill should return filled=False on timeout."""
        mock_x10 = MagicMock()
        open_order = Order(
            order_id="timeout-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.OPEN,
        )
        mock_x10.get_order = AsyncMock(return_value=open_order)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)

        # Very short timeout to test quickly
        result = await executor.wait_for_fill(open_order, timeout_s=0.15)

        assert result.filled is False
        assert result.fill_price is None


class TestCancelOrder:
    """Test order cancellation."""

    async def test_cancel_order_success(self):
        """cancel_order should return True on success."""
        mock_x10 = MagicMock()
        mock_x10.cancel_order = AsyncMock(return_value=None)

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)
        order = Order(
            order_id="cancel-me-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.OPEN,
        )

        result = await executor.cancel_order(order)

        assert result is True
        mock_x10.cancel_order.assert_called_once_with("ETH", "cancel-me-123")

    async def test_cancel_order_handles_exception(self):
        """cancel_order should return False on exception."""
        mock_x10 = MagicMock()
        mock_x10.cancel_order = AsyncMock(side_effect=Exception("Network error"))

        executor = MakerExecutor(settings=MagicMock(), x10=mock_x10)
        order = Order(
            order_id="fail-cancel-123",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("1.0"),
            status=OrderStatus.OPEN,
        )

        result = await executor.cancel_order(order)

        assert result is False


class TestFillResult:
    """Test FillResult dataclass."""

    def test_fill_result_defaults(self):
        """FillResult should have correct default values."""
        result = FillResult(
            filled=False,
            order=None,
            fill_price=None,
            fill_time_ms=None,
        )

        assert result.filled is False
        assert result.order is None
        assert result.fill_price is None
        assert result.fill_time_ms is None
        assert result.used_taker_fallback is False

    def test_fill_result_with_taker_fallback(self):
        """FillResult should track taker fallback usage."""
        result = FillResult(
            filled=True,
            order=None,
            fill_price=Decimal("100.00"),
            fill_time_ms=50.0,
            used_taker_fallback=True,
        )

        assert result.used_taker_fallback is True
