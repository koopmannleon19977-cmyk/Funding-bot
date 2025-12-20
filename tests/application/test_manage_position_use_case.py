from decimal import Decimal

import asyncio
import pytest

from src.application.use_cases.manage_position import ManagePositionRequest, ManagePositionUseCase
from src.domain.entities import Trade
from src.domain.services import ConstitutionGuard
from src.domain.rules import Constitution
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
async def test_keep_open_when_within_guard():
    guard = ConstitutionGuard(Constitution())
    uc = ManagePositionUseCase(guard)
    req = ManagePositionRequest(
        trade=make_trade(),
        funding_apy=Decimal("0.4"),
        funding_flip_hours=Decimal("1"),
        volatility_pct_24h=Decimal("5"),
        age_hours=Decimal("10"),
    )
    decision = await uc.execute(req)
    assert decision.keep_open
    assert decision.reason is None


@pytest.mark.asyncio
async def test_exit_on_maintenance_violation():
    guard = ConstitutionGuard(Constitution(volatility_panic_threshold=Decimal("8")))
    bus = EventBus()
    received: list[str] = []

    async def handler(evt: MaintenanceViolation):
        received.append(evt.reason)

    bus.subscribe(MaintenanceViolation, handler)
    await bus.start()

    uc = ManagePositionUseCase(guard, event_bus=bus)
    req = ManagePositionRequest(
        trade=make_trade(),
        funding_apy=Decimal("0.10"),
        funding_flip_hours=Decimal("5"),
        volatility_pct_24h=Decimal("12"),
        age_hours=Decimal("80"),
    )
    decision = await uc.execute(req)
    assert not decision.keep_open
    assert decision.reason in {"apy_below_maintenance", "funding_flip", "volatility_panic", "max_hold_exceeded"}

    for _ in range(5):
        await asyncio.sleep(0.02)
        if received:
            break
    await bus.stop()
    assert received, "maintenance event not published"
