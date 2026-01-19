import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.application.use_cases import CloseTradeRequest, CloseTradeUseCase
from src.domain.entities import Trade
from src.domain.value_objects import Price, Side, TradeStatus
from src.infrastructure.messaging.event_bus import EventBus, TradeClosed


def make_trade():
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
        entry_time=datetime.utcnow() - timedelta(hours=1),
        expected_apy=Decimal("0.3"),
    )


@pytest.mark.asyncio
async def test_close_trade_marks_closed_and_saves():
    repo = AsyncMock()
    uc = CloseTradeUseCase(repo)
    trade = make_trade()
    req = CloseTradeRequest(trade=trade)
    closed = await uc.execute(req)
    repo.save.assert_awaited()
    assert closed.status == TradeStatus.CLOSED
    assert closed.close_time is not None
    assert trade.status == TradeStatus.OPEN  # original immutable


@pytest.mark.asyncio
async def test_close_trade_publishes_event():
    repo = AsyncMock()
    repo.save = AsyncMock()
    bus = EventBus()
    received: list[str] = []

    async def handler(event: TradeClosed):
        received.append(event.trade_id)

    bus.subscribe(TradeClosed, handler)
    await bus.start()

    trade = make_trade()
    uc = CloseTradeUseCase(repo, event_bus=bus)
    await uc.execute(CloseTradeRequest(trade=trade))

    for _ in range(5):
        await asyncio.sleep(0.02)
        if received:
            break
    await bus.stop()

    assert received == ["t1"]
