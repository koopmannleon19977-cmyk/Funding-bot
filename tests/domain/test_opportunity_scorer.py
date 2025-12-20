from decimal import Decimal

from src.domain.entities import Opportunity
from src.domain.rules import Constitution
from src.domain.services import OpportunityScorer
from src.domain.value_objects import Decision, Price


def test_high_apy_scores_take():
    scorer = OpportunityScorer(rules=Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002")))
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.80"),
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price(Decimal("50000")),
        ask=Price(Decimal("50010")),
    )
    result = scorer.score(opp)
    assert result.decision == Decision.TAKE
    assert result.total >= Decimal("80")


def test_low_apy_rejects():
    scorer = OpportunityScorer(rules=Constitution(min_apy_filter=Decimal("0.20"), max_spread_filter_percent=Decimal("0.002")))
    opp = Opportunity(
        symbol="BTC-USD",
        expected_apy=Decimal("0.10"),  # below threshold
        spread=Decimal("0.001"),
        liquidity=Decimal("10000"),
        bid=Price(Decimal("50000")),
        ask=Price(Decimal("50010")),
    )
    result = scorer.score(opp)
    assert result.decision == Decision.REJECT
    assert result.total < Decimal("60")
