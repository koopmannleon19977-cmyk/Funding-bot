from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pytest import raises
from unittest.mock import AsyncMock

from src.application.use_cases import OpenTradeRequest, OpenTradeUseCase
from src.domain.entities import Opportunity
from src.domain.rules import Constitution
from src.domain.services import ConstitutionGuard, PositionSizer, RiskEvaluator, TradeInvariants
from src.domain.services.opportunity_scorer import OpportunityScorer
from src.domain.value_objects import Price


@pytest.mark.asyncio
async def test_executes_successfully_with_positive_size():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    scorer = OpportunityScorer(rules)
    sizer = PositionSizer(rules)
    invariants = TradeInvariants()
    risk = RiskEvaluator(rules)
    guard = ConstitutionGuard(rules)

    lighter = AsyncMock()
    x10 = AsyncMock()

    repo = AsyncMock()
    repo.exists.return_value = False

    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.50"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    req = OpenTradeRequest(
        symbol="BTC-USD",
        opportunity=opp,
        max_notional=Decimal("1000"),
        balance_lighter=Decimal("2000"),
        balance_x10=Decimal("2000"),
        max_exposure_usd=Decimal("2000"),
        volatility_pct_24h=Decimal("10"),
        estimated_fees_usd=Decimal("0.1"),
        hold_hours=Decimal("8"),
    )

    uc = OpenTradeUseCase(lighter, x10, repo, scorer, sizer, invariants, rules, risk, guard=guard)
    trade = await uc.execute(req)

    repo.save.assert_awaited()
    UUID(trade.id)  # validates UUID format
    assert trade.symbol == "BTC-USD"


@pytest.mark.asyncio
async def test_rejects_when_sizer_returns_zero():
    rules = Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002"))
    scorer = OpportunityScorer(rules)
    # Force size zero by setting balances to zero
    sizer = PositionSizer(rules)
    invariants = TradeInvariants()
    risk = RiskEvaluator(rules)
    guard = ConstitutionGuard(rules)

    repo = AsyncMock()
    repo.exists.return_value = False

    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.50"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    req = OpenTradeRequest(
        symbol="BTC-USD",
        opportunity=opp,
        max_notional=Decimal("100"),
        balance_lighter=Decimal("0"),
        balance_x10=Decimal("0"),
        max_exposure_usd=Decimal("500"),
        volatility_pct_24h=Decimal("10"),
        estimated_fees_usd=Decimal("1"),
        hold_hours=Decimal("8"),
    )

    uc = OpenTradeUseCase(SimpleNamespace(), SimpleNamespace(), repo, scorer, sizer, invariants, rules, risk, guard=guard)
    with raises(ValueError):
        await uc.execute(req)


@pytest.mark.asyncio
async def test_rejects_when_constitution_guard_blocks():
    rules = Constitution(min_apy_filter=Decimal("0.35"), max_spread_filter_percent=Decimal("0.002"))
    scorer = OpportunityScorer(rules)
    sizer = PositionSizer(rules)
    invariants = TradeInvariants()
    guard = ConstitutionGuard(rules)

    repo = AsyncMock()
    repo.exists.return_value = False

    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.36"),
        spread=Decimal("0.0015"),
        liquidity=Decimal("10000"),
        bid=Price("50000"),
        ask=Price("50010"),
    )
    req = OpenTradeRequest(
        symbol="BTC-USD",
        opportunity=opp,
        max_notional=Decimal("100"),
        balance_lighter=Decimal("200"),
        balance_x10=Decimal("200"),
        max_exposure_usd=Decimal("500"),
        volatility_pct_24h=Decimal("10"),
        estimated_fees_usd=Decimal("50"),
        hold_hours=Decimal("8"),
    )

    uc = OpenTradeUseCase(SimpleNamespace(), SimpleNamespace(), repo, scorer, sizer, invariants, rules, guard=guard)
    with raises(ValueError):
        await uc.execute(req)
