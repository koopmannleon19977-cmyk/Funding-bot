from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.domain.models import Exchange, MarketInfo, OrderRequest, OrderType, Side, TimeInForce


@pytest.mark.asyncio
async def test_x10_adapter_rounds_qty_to_step_multiple():
    settings = MagicMock()
    settings.x10.api_key = ""
    settings.x10.private_key = ""
    settings.x10.public_key = ""
    settings.x10.vault_id = ""

    adapter = X10Adapter(settings)
    adapter._trading_client = MagicMock()
    adapter._trading_client.place_order = AsyncMock(
        return_value={"order_id": "1", "status": "new", "avg_fill_price": "0", "fee": "0"}
    )

    adapter._markets["1000SHIB-USD"] = MarketInfo(
        symbol="1000SHIB-USD",
        exchange=Exchange.X10,
        base_asset="1000SHIB",
        quote_asset="USD",
        tick_size=Decimal("0.000001"),
        step_size=Decimal("100"),
        min_qty=Decimal("1000"),
        qty_precision=0,
    )

    req = OrderRequest(
        symbol="1000SHIB",
        exchange=Exchange.X10,
        side=Side.SELL,
        qty=Decimal("17780"),
        order_type=OrderType.LIMIT,
        price=Decimal("0.007069"),
        time_in_force=TimeInForce.IOC,
    )

    order = await adapter.place_order(req)

    # 17780 is not a multiple of step=100 => should round DOWN to 17700 for X10.
    called_qty = adapter._trading_client.place_order.call_args.kwargs["amount_of_synthetic"]
    assert called_qty == Decimal("17700")
    assert order.qty == Decimal("17700")

