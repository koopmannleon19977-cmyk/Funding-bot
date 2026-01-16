"""
Tests for partial fill handling during Lighter close.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)
from funding_bot.services.positions.close import (
    _calculate_close_price,
    _close_leg,
    _close_lighter_smart,
    _ensure_lighter_websocket,
    _execute_lighter_close_attempt,
    _execute_lighter_maker_close,
    _execute_lighter_taker_fallback,
    _get_lighter_close_config,
    _should_skip_lighter_close,
)


class DummyManager:
    def __init__(self) -> None:
        self.settings = MagicMock()
        self.settings.trading.early_tp_fast_close_enabled = True
        self.settings.trading.early_tp_fast_close_use_taker_directly = False
        self.settings.trading.early_tp_fast_close_max_attempts = 2
        self.settings.trading.early_tp_fast_close_attempt_timeout_seconds = 0.6
        self.settings.trading.early_tp_fast_close_total_timeout_seconds = 30.0
        self.settings.execution.close_get_order_timeout_seconds = Decimal("0.1")

        self.lighter = AsyncMock()
        self.market_data = MagicMock()
        self._close_leg = _close_leg.__get__(self, DummyManager)

        # Add helper methods for the refactored close logic
        self._get_lighter_close_config = _get_lighter_close_config.__get__(self, DummyManager)
        self._should_skip_lighter_close = _should_skip_lighter_close.__get__(self, DummyManager)
        self._ensure_lighter_websocket = _ensure_lighter_websocket.__get__(self, DummyManager)
        self._calculate_close_price = _calculate_close_price.__get__(self, DummyManager)
        self._execute_lighter_maker_close = _execute_lighter_maker_close.__get__(self, DummyManager)
        self._execute_lighter_close_attempt = _execute_lighter_close_attempt.__get__(self, DummyManager)
        self._execute_lighter_taker_fallback = _execute_lighter_taker_fallback.__get__(self, DummyManager)


@pytest.mark.asyncio
async def test_close_lighter_partial_fill_aggregates_notional_and_fees():
    mgr = DummyManager()

    trade = Trade(
        trade_id="close-partial-001",
        symbol="ETH",
        status=TradeStatus.OPEN,
        target_qty=Decimal("1.0"),
        target_notional_usd=Decimal("100"),
        entry_apy=Decimal("0.10"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            filled_qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            order_id="lighter-order-001",
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.SELL,
            filled_qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            order_id="x10-order-001",
        ),
        created_at=datetime.now(UTC),
    )
    trade.close_reason = "EARLY_TAKE_PROFIT_TEST"

    mgr.lighter.get_position = AsyncMock(
        return_value=Position(
            symbol="ETH",
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
        )
    )

    mgr.market_data.get_price = MagicMock(return_value=MagicMock(lighter_price=Decimal("100")))
    mgr.market_data.get_orderbook = MagicMock(
        return_value=MagicMock(lighter_bid=Decimal("100"), lighter_ask=Decimal("101"))
    )
    mgr.market_data.get_market_info = MagicMock(return_value=MagicMock(tick_size=Decimal("0.01")))

    mgr.lighter.place_order = AsyncMock(
        side_effect=[
            Order(
                order_id="order-1",
                symbol="ETH",
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("1.0"),
                price=Decimal("100"),
                status=OrderStatus.OPEN,
            ),
            Order(
                order_id="order-2",
                symbol="ETH",
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("0.4"),
                price=Decimal("110"),
                status=OrderStatus.OPEN,
            ),
        ]
    )

    async def get_order(_symbol: str, order_id: str) -> Order:
        if order_id == "order-1":
            return Order(
                order_id="order-1",
                symbol="ETH",
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                qty=Decimal("1.0"),
                price=Decimal("100"),
                status=OrderStatus.CANCELLED,
                filled_qty=Decimal("0.6"),
                avg_fill_price=Decimal("100"),
                fee=Decimal("0.10"),
            )
        return Order(
            order_id="order-2",
            symbol="ETH",
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.4"),
            price=Decimal("110"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("0.4"),
            avg_fill_price=Decimal("110"),
            fee=Decimal("0.20"),
        )

    mgr.lighter.get_order = AsyncMock(side_effect=get_order)
    mgr.lighter.cancel_order = AsyncMock(return_value=True)

    await _close_lighter_smart(mgr, trade)

    assert trade.leg1.exit_price == Decimal("104")
    assert trade.leg1.fees == Decimal("0.30")
    assert trade.close_attempts == 2
