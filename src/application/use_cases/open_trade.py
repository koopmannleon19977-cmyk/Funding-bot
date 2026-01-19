from dataclasses import dataclass
from decimal import Decimal

from src.domain.entities import Opportunity, Trade
from src.domain.rules import Constitution
from src.domain.services import (
    ConstitutionGuard,
    OpportunityScorer,
    PositionSizer,
    RiskEvaluator,
    TradeFactory,
    TradeInvariants,
)
from src.domain.value_objects import Side


@dataclass
class OpenTradeRequest:
    symbol: str
    opportunity: Opportunity
    max_notional: Decimal
    balance_lighter: Decimal
    balance_x10: Decimal
    max_exposure_usd: Decimal
    volatility_pct_24h: Decimal
    estimated_fees_usd: Decimal
    hold_hours: Decimal


class OpenTradeUseCase:
    def __init__(
        self,
        lighter,
        x10,
        repo,
        scorer: OpportunityScorer,
        sizer: PositionSizer,
        invariants: TradeInvariants,
        rules: Constitution,
        risk: RiskEvaluator | None = None,
        guard: ConstitutionGuard | None = None,
    ):
        self.lighter = lighter
        self.x10 = x10
        self.repo = repo
        self.scorer = scorer
        self.sizer = sizer
        self.invariants = invariants
        self.rules = rules
        self.risk = risk or RiskEvaluator(rules)
        self.guard = guard or ConstitutionGuard(rules)

    async def execute(self, request: OpenTradeRequest) -> Trade:
        # Guard rails
        if await self.repo.exists(request.symbol):
            raise ValueError("trade_exists")

        size_result = self.sizer.size(
            request.opportunity,
            max_notional=request.max_notional,
            balance_lighter=request.balance_lighter,
            balance_x10=request.balance_x10,
        )
        if size_result.notional <= 0:
            raise ValueError(size_result.reason or "size_zero")

        entry_decision = self.guard.check_entry(
            request.opportunity,
            desired_notional_usd=size_result.notional,
            est_fees_usd=request.estimated_fees_usd,
            hold_hours=request.hold_hours,
        )
        if not entry_decision.ok:
            raise ValueError(entry_decision.reason or "entry_blocked")

        trade = TradeFactory.create_trade(
            request.opportunity,
            size_result.notional,
            leg1_side=Side.BUY,
            leg2_side=Side.SELL,
        )

        inv = self.invariants.validate(trade)
        if not inv.ok:
            raise ValueError(inv.reason or "invariant_failed")

        risk_res = self.risk.evaluate(
            drawdown_pct=Decimal("0"),
            exposure_usd=size_result.notional,
            max_exposure_usd=request.max_exposure_usd,
            volatility_pct_24h=request.volatility_pct_24h,
        )
        if not risk_res.ok:
            raise ValueError(risk_res.reason or "risk_failed")

        await self.repo.save(trade)
        return trade
