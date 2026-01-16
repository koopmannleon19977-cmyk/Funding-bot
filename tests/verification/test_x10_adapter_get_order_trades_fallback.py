from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.domain.models import Exchange, Order, OrderStatus, OrderType, Side


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.x10.api_key = ""
    settings.x10.private_key = ""
    settings.x10.public_key = ""
    settings.x10.vault_id = ""
    settings.x10.base_url = "https://api.starknet.extended.exchange"

    settings.trading.maker_fee_x10 = Decimal("0")
    settings.trading.taker_fee_x10 = Decimal("0.001")
    settings.websocket = MagicMock()
    return settings


@pytest.mark.asyncio
async def test_x10_get_order_reconstructs_filled_order_from_trades():
    adapter = X10Adapter(_make_settings())

    account = MagicMock()
    account.get_order_by_id = AsyncMock(side_effect=ValueError("Error response from /user/orders/123: code 404 - not found"))
    account.get_order_by_external_id = AsyncMock(return_value=MagicMock(data=[]))
    account.get_open_orders = AsyncMock(return_value=MagicMock(data=[]))
    account.get_orders_history = AsyncMock(return_value=MagicMock(data=[]))
    account.get_trades = AsyncMock(
        return_value=MagicMock(
            data=[
                {"order_id": 123, "qty": Decimal("2"), "price": Decimal("100"), "fee": Decimal("0.20"), "side": "BUY"},
                {"order_id": 123, "qty": Decimal("3"), "price": Decimal("110"), "fee": Decimal("0.30"), "side": "BUY"},
            ]
        )
    )

    adapter._trading_client = MagicMock()
    adapter._trading_client.account = account
    adapter._submitted_order_by_id["123"] = Order(
        order_id="123",
        symbol="ETH",
        exchange=Exchange.X10,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("5"),
        price=Decimal("105"),
        status=OrderStatus.PENDING,
    )

    order = await adapter.get_order("ETH", "123")
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal("5")
    assert order.avg_fill_price == Decimal("106")
    assert order.fee == Decimal("0.50")


@pytest.mark.asyncio
async def test_x10_get_order_marks_partial_trades_as_terminal_when_not_open_or_in_history():
    adapter = X10Adapter(_make_settings())

    account = MagicMock()
    account.get_order_by_id = AsyncMock(side_effect=ValueError("Error response from /user/orders/123: code 404 - not found"))
    account.get_order_by_external_id = AsyncMock(return_value=MagicMock(data=[]))
    account.get_open_orders = AsyncMock(return_value=MagicMock(data=[]))
    account.get_orders_history = AsyncMock(return_value=MagicMock(data=[]))
    account.get_trades = AsyncMock(return_value=MagicMock(data=[{"order_id": 123, "qty": Decimal("2"), "price": Decimal("100"), "fee": Decimal("0"), "side": "SELL"}]))

    adapter._trading_client = MagicMock()
    adapter._trading_client.account = account
    adapter._submitted_order_by_id["123"] = Order(
        order_id="123",
        symbol="ETH",
        exchange=Exchange.X10,
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        qty=Decimal("5"),
        price=Decimal("100"),
        status=OrderStatus.PENDING,
    )

    order = await adapter.get_order("ETH", "123")
    assert order is not None
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_qty == Decimal("2")

