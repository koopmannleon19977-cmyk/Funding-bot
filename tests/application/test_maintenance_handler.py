import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.application.handlers.maintenance_handler import wire_maintenance_auto_close
from src.application.use_cases import CloseTradeRequest
from src.domain.entities import Trade
from src.domain.value_objects import Price, Side, TradeStatus
from src.infrastructure.messaging.event_bus import EventBus, MaintenanceViolation


def make_trade() -> Trade:
    return Trade(
        id="t1",
        symbol="BTC-USD",
        status=TradeStatus.OPEN,
        leg1_exchange="lighter",
        leg1_side=Side.BUY,
        leg1_entry_price=Price("50000"),
        leg1_quantity=Decimal("1"),
        leg2_exchange="x10",
        leg2_side=Side.SELL,
        leg2_entry_price=Price("50010"),
        leg2_quantity=Decimal("1"),
        entry_time=__import__("datetime").datetime.utcnow(),
        expected_apy=Decimal("0.5"),
    )


@pytest.mark.asyncio
async def test_handler_closes_on_violation():
    bus = EventBus()
    trade = make_trade()

    async def fetch_trade(event: MaintenanceViolation):
        return trade if event.trade_id == "t1" else None

    closer = AsyncMock()

    wire_maintenance_auto_close(bus, fetch_trade, closer)
    await bus.start()

    await bus.publish(
        MaintenanceViolation(
            trade_id="t1",
            symbol="BTC-USD",
            reason="volatility_panic",
            timestamp=__import__("datetime").datetime.utcnow(),
        )
    )

    for _ in range(5):
        await asyncio.sleep(0.02)
        if closer.execute.await_count:
            break

    await bus.stop()

    closer.execute.assert_awaited_once()
    args, _ = closer.execute.call_args
    assert isinstance(args[0], CloseTradeRequest)
    assert args[0].trade.id == "t1"


@pytest.mark.asyncio
async def test_handler_no_trade_noop():
    bus = EventBus()

    async def fetch_trade(event: MaintenanceViolation):
        return None

    closer = AsyncMock()

    wire_maintenance_auto_close(bus, fetch_trade, closer)
    await bus.start()

    await bus.publish(
        MaintenanceViolation(
            trade_id="missing",
            symbol="BTC-USD",
            reason="funding_flip",
            timestamp=__import__("datetime").datetime.utcnow(),
        )
    )
    await asyncio.sleep(0.05)
    await bus.stop()

    closer.execute.assert_not_awaited()
