from dataclasses import dataclass
from datetime import datetime

from src.domain.entities import Trade
from src.infrastructure.messaging.event_bus import EventBus, TradeClosed


@dataclass
class CloseTradeRequest:
    trade: Trade


class CloseTradeUseCase:
    def __init__(self, repo, event_bus: EventBus | None = None):
        self.repo = repo
        self.event_bus = event_bus

    async def execute(self, request: CloseTradeRequest) -> Trade:
        closed = request.trade.mark_closed(datetime.utcnow())
        await self.repo.save(closed)
        if self.event_bus:
            await self.event_bus.publish(
                TradeClosed(trade_id=closed.id, symbol=closed.symbol, timestamp=datetime.utcnow())
            )
        return closed
