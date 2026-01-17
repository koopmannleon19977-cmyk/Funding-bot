"""
Unit tests for coordinated close maker price calculation.

Tests that POST_ONLY maker orders are placed on the correct side of the spread
to avoid immediate rejection.
"""
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from funding_bot.domain.models import Side
from funding_bot.services.positions.close import _calculate_leg_maker_price


class TestCalculateLegMakerPrice:
    """Tests for _calculate_leg_maker_price helper."""

    def _make_mock_orderbook(
        self,
        lighter_bid: Decimal = Decimal("100"),
        lighter_ask: Decimal = Decimal("101"),
        x10_bid: Decimal = Decimal("100"),
        x10_ask: Decimal = Decimal("101"),
    ) -> MagicMock:
        """Create mock orderbook."""
        ob = MagicMock()
        ob.lighter_bid = lighter_bid
        ob.lighter_ask = lighter_ask
        ob.x10_bid = x10_bid
        ob.x10_ask = x10_ask
        return ob

    def test_buy_side_uses_bid_not_ask_for_lighter(self):
        """
        BUY maker order should use bid price (not ask).

        A POST_ONLY BUY at ask would immediately cross and be rejected.
        Placing at bid ensures the order goes into the orderbook.
        """
        ob = self._make_mock_orderbook(
            lighter_bid=Decimal("49990"),
            lighter_ask=Decimal("50010"),
        )
        tick = Decimal("0.01")

        price = _calculate_leg_maker_price(
            ob=ob,
            price_data=None,
            close_qty=Decimal("1.0"),
            close_side=Side.BUY,
            exchange="lighter",
            tick=tick,
        )

        # Should use bid (49990), NOT ask (50010)
        assert price == Decimal("49990"), f"BUY maker should use bid, got {price}"

    def test_sell_side_uses_ask_not_bid_for_lighter(self):
        """
        SELL maker order should use ask price (not bid).

        A POST_ONLY SELL at bid would immediately cross and be rejected.
        Placing at ask ensures the order goes into the orderbook.
        """
        ob = self._make_mock_orderbook(
            lighter_bid=Decimal("49990"),
            lighter_ask=Decimal("50010"),
        )
        tick = Decimal("0.01")

        price = _calculate_leg_maker_price(
            ob=ob,
            price_data=None,
            close_qty=Decimal("1.0"),
            close_side=Side.SELL,
            exchange="lighter",
            tick=tick,
        )

        # Should use ask (50010), NOT bid (49990)
        assert price == Decimal("50010"), f"SELL maker should use ask, got {price}"

    def test_buy_side_uses_bid_not_ask_for_x10(self):
        """BUY maker order on X10 should use bid price."""
        ob = self._make_mock_orderbook(
            x10_bid=Decimal("49990"),
            x10_ask=Decimal("50010"),
        )
        tick = Decimal("0.01")

        price = _calculate_leg_maker_price(
            ob=ob,
            price_data=None,
            close_qty=Decimal("1.0"),
            close_side=Side.BUY,
            exchange="x10",
            tick=tick,
        )

        assert price == Decimal("49990"), f"BUY maker should use bid, got {price}"

    def test_sell_side_uses_ask_not_bid_for_x10(self):
        """SELL maker order on X10 should use ask price."""
        ob = self._make_mock_orderbook(
            x10_bid=Decimal("49990"),
            x10_ask=Decimal("50010"),
        )
        tick = Decimal("0.01")

        price = _calculate_leg_maker_price(
            ob=ob,
            price_data=None,
            close_qty=Decimal("1.0"),
            close_side=Side.SELL,
            exchange="x10",
            tick=tick,
        )

        assert price == Decimal("50010"), f"SELL maker should use ask, got {price}"

    def test_zero_qty_returns_zero(self):
        """Zero quantity should return zero price."""
        ob = self._make_mock_orderbook()
        tick = Decimal("0.01")

        price = _calculate_leg_maker_price(
            ob=ob,
            price_data=None,
            close_qty=Decimal("0"),
            close_side=Side.BUY,
            exchange="lighter",
            tick=tick,
        )

        assert price == Decimal("0")


@pytest.mark.asyncio
async def test_cancelled_order_triggers_ioc_escalation():
    """
    Test that CANCELLED/REJECTED orders are NOT treated as filled.

    Bug: When order status is CANCELLED/REJECTED, the code removes
    the leg from legs_to_close but doesn't add to filled_legs.
    This causes the check `if not legs_to_close` to falsely conclude
    all legs were filled.
    """
    from unittest.mock import AsyncMock
    import types

    from funding_bot.domain.models import Exchange, OrderStatus, TimeInForce
    from funding_bot.services.positions import close
    from funding_bot.services.positions.close import _close_both_legs_coordinated

    # Create mock service
    service = MagicMock()
    service.settings.trading.coordinated_close_enabled = True
    service.settings.trading.coordinated_close_x10_use_maker = True
    service.settings.trading.coordinated_close_maker_timeout_seconds = 0.5
    service.settings.trading.coordinated_close_ioc_timeout_seconds = 2.0
    service.settings.trading.coordinated_close_escalate_to_ioc = True

    service.market_data.get_price.return_value = MagicMock(
        lighter_price=Decimal("50000"),
        x10_price=Decimal("50000"),
    )
    service.market_data.get_orderbook.return_value = MagicMock(
        lighter_bid=Decimal("49990"),
        lighter_ask=Decimal("50010"),
        x10_bid=Decimal("49990"),
        x10_ask=Decimal("50010"),
    )
    service.market_data.get_market_info.side_effect = lambda symbol, exchange: MagicMock(
        tick_size=Decimal("0.01")
    )

    # Create test trade with proper Side objects that have inverse() method
    trade = MagicMock()
    trade.symbol = "BTC"

    # Leg1: Lighter leg - SELL position means we BUY to close
    leg1 = MagicMock()
    leg1.side = Side.SELL
    leg1.filled_qty = Decimal("0.1")  # Position size to close
    leg1.remaining_qty = Decimal("0.1")
    leg1.fees = Decimal("0")
    trade.leg1 = leg1

    # Leg2: X10 leg - BUY position means we SELL to close
    leg2 = MagicMock()
    leg2.side = Side.BUY
    leg2.filled_qty = Decimal("0.1")  # Position size to close
    leg2.remaining_qty = Decimal("0.1")
    leg2.fees = Decimal("0")
    trade.leg2 = leg2

    # Track IOC escalation
    ioc_escalation_called = False

    def make_cancelled_order(order_id):
        """Create a mock CANCELLED order."""
        order = MagicMock()
        order.order_id = order_id
        order.status = OrderStatus.CANCELLED
        order.is_filled = False
        order.is_active = False  # CANCELLED is not active
        order.filled_qty = Decimal("0")
        order.avg_fill_price = Decimal("0")
        order.fee = Decimal("0")
        return order

    def make_rejected_order(order_id):
        """Create a mock REJECTED order."""
        order = MagicMock()
        order.order_id = order_id
        order.status = OrderStatus.REJECTED
        order.is_filled = False
        order.is_active = False  # REJECTED is not active
        order.filled_qty = Decimal("0")
        order.avg_fill_price = Decimal("0")
        order.fee = Decimal("0")
        return order

    async def mock_place_order(request):
        """Return pending order on initial placement."""
        order = MagicMock()
        order.order_id = f"order_{request.exchange.value}"
        order.status = OrderStatus.PENDING
        order.is_filled = False
        order.is_active = True
        order.filled_qty = Decimal("0")
        order.avg_fill_price = Decimal("0")
        order.fee = Decimal("0")
        return order

    # Lighter order gets CANCELLED, X10 gets REJECTED
    service.lighter.place_order = AsyncMock(side_effect=mock_place_order)
    service.x10.place_order = AsyncMock(side_effect=mock_place_order)
    service.lighter.get_order = AsyncMock(return_value=make_cancelled_order("order_LIGHTER"))
    service.x10.get_order = AsyncMock(return_value=make_rejected_order("order_X10"))

    # Track if IOC escalation or fallback is called
    service._close_lighter_smart = AsyncMock()
    service._close_x10_smart = AsyncMock()
    service._update_trade = AsyncMock()

    # Bind methods
    service._submit_maker_order = types.MethodType(close._submit_maker_order, service)
    service._execute_ioc_close = AsyncMock()  # Track if this is called

    await close._close_both_legs_coordinated(service, trade)

    # The key assertion: IOC escalation OR fallback should be called
    # because CANCELLED/REJECTED orders are NOT filled
    ioc_or_fallback_called = (
        service._execute_ioc_close.called or
        service._close_lighter_smart.called or
        service._close_x10_smart.called
    )

    assert ioc_or_fallback_called, (
        "CANCELLED/REJECTED orders should trigger IOC escalation or fallback, "
        "not be treated as filled"
    )
