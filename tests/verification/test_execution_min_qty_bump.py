from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.errors import ExecutionError
from funding_bot.domain.models import Exchange, Side, Trade
from funding_bot.services.execution import ExecutionEngine


@pytest.mark.asyncio
async def test_calculate_quantity_rejects_when_min_qty_exceeds_sized_target_notional():
    settings = MagicMock()
    settings.execution.max_min_qty_bump_multiple = Decimal("1.5")

    lighter = AsyncMock()
    x10 = AsyncMock()
    store = AsyncMock()
    event_bus = AsyncMock()

    market_data = MagicMock()
    market_data.get_price.return_value.mid_price = Decimal("544.0")

    def _market_info(_symbol: str, _exchange: Exchange):
        info = MagicMock()
        info.step_size = Decimal("0.0001")
        info.min_qty = Decimal("0.100")  # forces ~$54 min notional at $544
        return info

    market_data.get_market_info.side_effect = _market_info

    engine = ExecutionEngine(settings, lighter, x10, store, event_bus, market_data)

    trade = Trade.create(
        symbol="ZEC",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("0.0001"),
        target_notional_usd=Decimal("20.0"),
    )
    trade.target_qty = Decimal("0.0001")
    trade.target_notional_usd = Decimal("20.0")

    with pytest.raises(ExecutionError):
        await engine._calculate_quantity(trade, MagicMock())
