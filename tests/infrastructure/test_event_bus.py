import asyncio
from datetime import datetime

import pytest

from src.infrastructure.messaging.event_bus import EventBus, TradeOpened


@pytest.mark.asyncio
async def test_event_bus_dispatches():
    bus = EventBus()
    received = {}

    async def handler(evt: TradeOpened):
        received["id"] = evt.trade_id

    bus.subscribe(TradeOpened, handler)
    await bus.start()
    await bus.publish(TradeOpened(trade_id="t1", symbol="BTC-USD", timestamp=datetime.utcnow()))
    await asyncio.sleep(0.05)
    await bus.stop()

    assert received["id"] == "t1"
