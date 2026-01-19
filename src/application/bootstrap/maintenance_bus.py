from src.application.use_cases.close_trade import CloseTradeRequest
from src.infrastructure.messaging.event_bus import EventBus, MaintenanceViolation


async def setup_maintenance_wiring(repo, close_use_case, event_bus: EventBus | None = None) -> EventBus:
    bus = event_bus or EventBus()
    await bus.start()

    async def _handler(event: MaintenanceViolation):
        trade = await repo.get_by_id(event.trade_id)
        if trade:
            handler = close_use_case.execute
            await handler(CloseTradeRequest(trade=trade))

    bus.subscribe(MaintenanceViolation, _handler)
    return bus


def teardown_event_bus(event_bus: EventBus):
    async def stop():
        await event_bus.stop()

    return stop
