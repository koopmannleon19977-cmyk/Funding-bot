import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.application.bootstrap.maintenance_bus import setup_maintenance_wiring, teardown_event_bus
from src.application.use_cases.close_trade import CloseTradeRequest
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


class RepoStub:
    def __init__(self, trade: Trade | None):
        self.trade = trade

    async def get_by_id(self, trade_id: str):
        if self.trade and self.trade.id == trade_id:
            return self.trade
        return None


@pytest.mark.asyncio
async def test_setup_wires_and_closes():
    bus = EventBus()
    trade = make_trade()
    repo = RepoStub(trade)
    closer = AsyncMock()

    wired_bus = await setup_maintenance_wiring(repo, closer, event_bus=bus)
    await bus.publish(MaintenanceViolation(trade_id="t1", symbol="BTC-USD", reason="volatility_panic", timestamp=__import__("datetime").datetime.utcnow()))

    for _ in range(5):
        await asyncio.sleep(0.02)
        if closer.execute.await_count:
            break

    stopper = teardown_event_bus(wired_bus)
    await stopper()

    closer.execute.assert_awaited_once()
    args, _ = closer.execute.call_args
    assert isinstance(args[0], CloseTradeRequest)


@pytest.mark.asyncio
async def test_setup_no_trade_noop():
    bus = EventBus()
    repo = RepoStub(None)
    closer = AsyncMock()

    wired_bus = await setup_maintenance_wiring(repo, closer, event_bus=bus)
    await bus.publish(MaintenanceViolation(trade_id="missing", symbol="BTC-USD", reason="funding_flip", timestamp=__import__("datetime").datetime.utcnow()))
    await asyncio.sleep(0.05)
    stopper = teardown_event_bus(wired_bus)
    await stopper()

    closer.execute.assert_not_awaited()
