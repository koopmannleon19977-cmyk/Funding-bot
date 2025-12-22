from src.application.use_cases.close_trade import CloseTradeRequest
from src.infrastructure.messaging.event_bus import EventBus, MaintenanceViolation


def wire_maintenance_auto_close(event_bus: EventBus, fetch_trade, close_use_case):
    async def _handler(event: MaintenanceViolation):
        trade = await fetch_trade(event)
        if trade:
            await close_use_case.execute(CloseTradeRequest(trade=trade))

    event_bus.subscribe(MaintenanceViolation, _handler)
    return event_bus
