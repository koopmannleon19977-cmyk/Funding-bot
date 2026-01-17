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
