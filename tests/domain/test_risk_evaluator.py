from decimal import Decimal

from src.domain.rules import Constitution
from src.domain.services import RiskEvaluator


def test_risk_passes_within_limits():
    rules = Constitution(max_drawdown_pct=Decimal("0.2"), max_volatility_pct=Decimal("50"))
    evaluator = RiskEvaluator(rules)
    res = evaluator.evaluate(drawdown_pct=Decimal("0.1"), exposure_usd=Decimal("1000"), max_exposure_usd=Decimal("2000"), volatility_pct_24h=Decimal("10"))
    assert res.ok


def test_risk_rejects_drawdown():
    rules = Constitution(max_drawdown_pct=Decimal("0.2"), max_volatility_pct=Decimal("50"))
    evaluator = RiskEvaluator(rules)
    res = evaluator.evaluate(drawdown_pct=Decimal("0.25"), exposure_usd=Decimal("1000"), max_exposure_usd=Decimal("2000"), volatility_pct_24h=Decimal("10"))
    assert not res.ok
    assert res.reason == "drawdown_exceeded"


def test_risk_rejects_exposure():
    rules = Constitution(max_drawdown_pct=Decimal("0.2"), max_volatility_pct=Decimal("50"))
    evaluator = RiskEvaluator(rules)
    res = evaluator.evaluate(drawdown_pct=Decimal("0.1"), exposure_usd=Decimal("3000"), max_exposure_usd=Decimal("2000"), volatility_pct_24h=Decimal("10"))
    assert not res.ok
    assert res.reason == "exposure_exceeded"


def test_risk_rejects_volatility():
    rules = Constitution(max_drawdown_pct=Decimal("0.2"), max_volatility_pct=Decimal("50"))
    evaluator = RiskEvaluator(rules)
    res = evaluator.evaluate(drawdown_pct=Decimal("0.1"), exposure_usd=Decimal("1000"), max_exposure_usd=Decimal("2000"), volatility_pct_24h=Decimal("60"))
    assert not res.ok
    assert res.reason == "volatility_exceeded"
