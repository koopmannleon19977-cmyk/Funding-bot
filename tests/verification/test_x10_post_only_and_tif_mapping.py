from __future__ import annotations

import sys
import types
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.domain.models import Exchange, OrderRequest, OrderType, Side, TimeInForce


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
    settings.execution = MagicMock()
    # Let adapter choose its internal default for slippage
    settings.execution.taker_order_slippage = None
    return settings


def _install_fake_x10_orders_module() -> None:
    import enum

    orders_mod = types.ModuleType("x10.perpetual.orders")

    class OrderSide(enum.Enum):
        BUY = "BUY"
        SELL = "SELL"

    class TimeInForceEnum(enum.Enum):
        GTT = "GTT"
        IOC = "IOC"
        FOK = "FOK"

    orders_mod.OrderSide = OrderSide
    orders_mod.TimeInForce = TimeInForceEnum

    perpetual_mod = types.ModuleType("x10.perpetual")
    perpetual_mod.orders = orders_mod

    x10_mod = types.ModuleType("x10")
    x10_mod.perpetual = perpetual_mod

    sys.modules.setdefault("x10", x10_mod)
    sys.modules.setdefault("x10.perpetual", perpetual_mod)
    sys.modules.setdefault("x10.perpetual.orders", orders_mod)


def test_x10_parse_order_model_maps_post_only_and_reduce_only():
    adapter = X10Adapter(_make_settings())
    order = adapter._parse_order_model(
        {
            "id": 123,
            "status": "NEW",
            "side": "BUY",
            "type": "LIMIT",
            "qty": "1",
            "price": "100",
            "reduceOnly": True,
            "postOnly": True,
            "timeInForce": "GTT",
            "externalId": "ext_1",
        },
        symbol="ETH",
        order_id_hint="123",
        client_order_id_hint="ext_1",
    )
    assert order.reduce_only is True
    assert order.time_in_force == TimeInForce.POST_ONLY


def test_x10_parse_order_model_maps_ioc_time_in_force():
    adapter = X10Adapter(_make_settings())
    order = adapter._parse_order_model(
        {"id": 1, "status": "NEW", "side": "SELL", "type": "LIMIT", "qty": "1", "price": "100", "timeInForce": "IOC"},
        symbol="ETH",
        order_id_hint="1",
    )
    assert order.time_in_force == TimeInForce.IOC


@pytest.mark.asyncio
async def test_x10_place_order_sets_post_only_flag_for_post_only_requests():
    _install_fake_x10_orders_module()
    adapter = X10Adapter(_make_settings())

    adapter._markets = {
        "ETH-USD": MagicMock(step_size=Decimal("0.0001"), tick_size=Decimal("0.01"), min_qty=Decimal("0"))
    }
    adapter._symbol_map = {"ETH": "ETH-USD", "ETH-USD": "ETH-USD"}

    adapter._trading_client = MagicMock()
    adapter._price_cache = {"ETH-USD": Decimal("100")}
    adapter._trading_client.place_order = AsyncMock(return_value=MagicMock(data={"order_id": "1", "status": "pending"}))

    async def _direct(call, **_kwargs):
        return await call()

    adapter._sdk_call_with_retry = _direct  # type: ignore[assignment]

    req = OrderRequest(
        symbol="ETH",
        exchange=Exchange.X10,
        side=Side.BUY,
        qty=Decimal("1"),
        order_type=OrderType.LIMIT,
        price=Decimal("100"),
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=False,
        client_order_id="cid1",
    )

    await adapter.place_order(req)

    _args, kwargs = adapter._trading_client.place_order.call_args
    assert kwargs["post_only"] is True
