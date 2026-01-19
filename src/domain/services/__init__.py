from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from src.domain.entities import Opportunity, Trade
from src.domain.rules import Constitution
from src.domain.value_objects import (
    Decision,
    DecisionResult,
    InvariantResult,
    MaintenanceDecision,
    Price,
    RiskResult,
    Side,
    SizeResult,
    TradeStatus,
)


class OpportunityScorer:
    def __init__(self, rules: Constitution):
        self.rules = rules

    def score(self, opportunity: Opportunity) -> DecisionResult:
        total = opportunity.expected_apy * Decimal("100")
        if opportunity.spread > self.rules.max_spread_filter_percent:
            return DecisionResult(Decision.REJECT, total, reason="spread_too_wide")
        if opportunity.expected_apy < self.rules.min_apy_filter:
            return DecisionResult(Decision.REJECT, total, reason="apy_below_min")
        return DecisionResult(Decision.TAKE, total)


class PositionSizer:
    def __init__(self, rules: Constitution):
        self.rules = rules

    def size(
        self,
        opportunity: Opportunity,
        max_notional: Decimal,
        balance_lighter: Decimal,
        balance_x10: Decimal,
        lot_size: Decimal | None = None,
    ) -> SizeResult:
        if opportunity.expected_apy < self.rules.min_apy_filter:
            return SizeResult(Decimal("0"), reason="apy_below_min")
        if opportunity.spread > self.rules.max_spread_filter_percent:
            return SizeResult(Decimal("0"), reason="spread_too_wide")

        notional = min(max_notional, balance_lighter, balance_x10)
        if lot_size is not None and lot_size > 0:
            qty = (notional / opportunity.ask.value).quantize(Decimal("0.00000001"))
            qty_steps = (qty // lot_size) * lot_size
            notional = (qty_steps * opportunity.ask.value).quantize(Decimal("0.01"))
            if qty_steps <= 0:
                return SizeResult(Decimal("0"), reason="lot_rounding_zero")
        return SizeResult(notional)


class PnLCalculator:
    @staticmethod
    def unrealized(
        long_entry: Price, short_entry: Price, long_current: Price, short_current: Price, quantity: Decimal
    ) -> Decimal:
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        long_pnl = (long_current.value - long_entry.value) * quantity
        short_pnl = (short_entry.value - short_current.value) * quantity
        return long_pnl + short_pnl

    @staticmethod
    def realized(price_pnl: Decimal, fees: Decimal) -> Decimal:
        return price_pnl - fees


class RiskEvaluator:
    def __init__(self, rules: Constitution):
        self.rules = rules

    def evaluate(
        self, drawdown_pct: Decimal, exposure_usd: Decimal, max_exposure_usd: Decimal, volatility_pct_24h: Decimal
    ) -> RiskResult:
        if drawdown_pct > self.rules.max_drawdown_pct:
            return RiskResult(False, "drawdown_exceeded")
        if exposure_usd > max_exposure_usd:
            return RiskResult(False, "exposure_exceeded")
        if volatility_pct_24h > self.rules.max_volatility_pct:
            return RiskResult(False, "volatility_exceeded")
        return RiskResult(True)


class TradeInvariants:
    def __init__(self, notional_tolerance: Decimal | None = None):
        self.notional_tolerance = notional_tolerance or Decimal("0.001")

    def validate(self, trade: Trade) -> InvariantResult:
        if trade.leg1_side == trade.leg2_side:
            return InvariantResult(False, "legs_must_be_opposite")
        notional1 = trade.leg1_entry_price.value * trade.leg1_quantity
        notional2 = trade.leg2_entry_price.value * trade.leg2_quantity
        mismatch = abs(notional1 - notional2)
        threshold = trade.leg1_entry_price.value * trade.leg1_quantity * self.notional_tolerance
        if mismatch > threshold:
            return InvariantResult(False, "notional_mismatch")
        return InvariantResult(True)


@dataclass
class EntryDecision:
    ok: bool
    reason: str | None = None


class ConstitutionGuard:
    def __init__(self, rules: Constitution):
        self.rules = rules

    def check_entry(
        self, opportunity: Opportunity, desired_notional_usd: Decimal, est_fees_usd: Decimal, hold_hours: Decimal
    ) -> EntryDecision:
        if opportunity.expected_apy < self.rules.min_apy_filter:
            return EntryDecision(False, "apy_below_min")
        if opportunity.spread > self.rules.max_spread_filter_percent:
            return EntryDecision(False, "spread_too_wide")
        if opportunity.liquidity < desired_notional_usd:
            return EntryDecision(False, "insufficient_liquidity")
        # Treat expected_apy as an annualized rate
        hourly_return = (opportunity.expected_apy * desired_notional_usd) / Decimal("8760")
        breakeven_hours = (est_fees_usd / hourly_return) if hourly_return > 0 else Decimal("INF")
        if breakeven_hours > self.rules.max_breakeven_hours:
            return EntryDecision(False, "breakeven_too_slow")
        expected_profit_window = hourly_return * hold_hours
        if expected_profit_window - est_fees_usd < self.rules.min_profit_exit_usd:
            return EntryDecision(False, "min_profit_not_met")
        return EntryDecision(True)

    def check_maintenance(
        self, funding_apy: Decimal, funding_flip_hours: Decimal, volatility_pct_24h: Decimal, age_hours: Decimal
    ) -> EntryDecision:
        if funding_apy < self.rules.min_maintenance_apy:
            return EntryDecision(False, "apy_below_maintenance")
        if funding_flip_hours > self.rules.funding_flip_hours_threshold:
            return EntryDecision(False, "funding_flip")
        if volatility_pct_24h > self.rules.volatility_panic_threshold:
            return EntryDecision(False, "volatility_panic")
        if age_hours > self.rules.max_hold_hours:
            return EntryDecision(False, "max_hold_exceeded")
        return EntryDecision(True)


class OpportunityResult:
    def __init__(self, trade: Trade):
        self.trade = trade


class PositionSizerResult(SizeResult):
    pass


class RiskEvaluatorResult(RiskResult):
    pass


class ManageDecision(MaintenanceDecision):
    pass


class TradeFactory:
    @staticmethod
    def create_trade(opportunity: Opportunity, notional: Decimal, leg1_side: Side, leg2_side: Side) -> Trade:
        quantity = (notional / opportunity.ask.value).quantize(Decimal("0.00000001"))
        return Trade(
            id=str(uuid4()),
            symbol=opportunity.symbol,
            status=TradeStatus.OPEN,
            leg1_exchange="lighter",
            leg1_side=leg1_side,
            leg1_entry_price=opportunity.ask,
            leg1_quantity=quantity,
            leg2_exchange="x10",
            leg2_side=leg2_side,
            leg2_entry_price=opportunity.bid,
            leg2_quantity=quantity,
            entry_time=__import__("datetime").datetime.utcnow(),
            expected_apy=opportunity.expected_apy,
        )
