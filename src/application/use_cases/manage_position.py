from dataclasses import dataclass
from decimal import Decimal
from src.domain.entities import Trade
from src.domain.services import ConstitutionGuard, ManageDecision
from src.infrastructure.messaging.event_bus import MaintenanceViolation, EventBus


@dataclass
class ManagePositionRequest:
    trade: Trade
    funding_apy: Decimal
    funding_flip_hours: Decimal
    volatility_pct_24h: Decimal
    age_hours: Decimal


class ManagePositionUseCase:
    def __init__(self, guard: ConstitutionGuard, event_bus: EventBus | None = None):
        self.guard = guard
        self.event_bus = event_bus

    async def execute(self, request: ManagePositionRequest) -> ManageDecision:
        decision = self.guard.check_maintenance(
            funding_apy=request.funding_apy,
            funding_flip_hours=request.funding_flip_hours,
            volatility_pct_24h=request.volatility_pct_24h,
            age_hours=request.age_hours,
        )
        if decision.ok:
            return ManageDecision(True, None)

        if self.event_bus:
            await self.event_bus.publish(
                MaintenanceViolation(
                    trade_id=request.trade.id,
                    symbol=request.trade.symbol,
                    reason=decision.reason or "maintenance_violation",
                    timestamp=__import__("datetime").datetime.utcnow(),
                )
            )
        return ManageDecision(False, decision.reason)
