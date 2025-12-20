from decimal import Decimal
from datetime import datetime

import pytest

from src.domain.entities import Trade
from src.domain.services import TradeInvariants
from src.domain.value_objects import Price, Side, TradeStatus


def make_trade(leg1_qty=Decimal("1"), leg2_qty=Decimal("1")) -> Trade:
    return Trade(
        id="t1",
        symbol="BTC-USD",
        status=TradeStatus.OPEN,
        leg1_exchange="lighter",
        leg1_side=Side.BUY,
        leg1_entry_price=Price("50000"),
        leg1_quantity=leg1_qty,
        leg2_exchange="x10",
        leg2_side=Side.SELL,
        leg2_entry_price=Price("50010"),
        leg2_quantity=leg2_qty,
        entry_time=datetime.utcnow(),
        expected_apy=Decimal("0.30"),
    )


def test_invariants_pass_on_delta_neutral():
    inv = TradeInvariants(notional_tolerance=Decimal("0.001"))
    trade = make_trade()
    result = inv.validate(trade)
    assert result.ok


def test_invariants_fail_on_same_side():
    inv = TradeInvariants()
    trade = make_trade()
    bad_trade = Trade(
        **{**trade.__dict__, "leg2_side": Side.BUY}
    )
    result = inv.validate(bad_trade)
    assert not result.ok
    assert "opposite" in (result.reason or "").lower()


def test_invariants_fail_on_notional_mismatch():
    inv = TradeInvariants(notional_tolerance=Decimal("0.0001"))
    trade = make_trade(leg2_qty=Decimal("1.1"))
    result = inv.validate(trade)
    assert not result.ok
    assert "notional" in (result.reason or "").lower()


def test_mark_closed_returns_new_instance():
    trade = make_trade()
    closed = trade.mark_closed(datetime.utcnow())
    assert closed.status == TradeStatus.CLOSED
    assert closed is not trade
    assert trade.status == TradeStatus.OPEN


def test_price_must_be_positive():
    with pytest.raises(ValueError):
        Price("-1")
