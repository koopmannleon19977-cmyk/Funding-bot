"""
Test that check_trades() does NOT count rebalanced trades as closed.

This is a critical test to ensure that rebalance operations (which keep trades
in OPEN status) are not incorrectly counted as closed trades.

The bug: check_trades() was appending trades to closed_trades based only on
result.success, without checking if trade.status == TradeStatus.CLOSED.

The fix: check_trades() now requires BOTH result.success AND trade.status == CLOSED.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


from funding_bot.domain.models import Side, TradeStatus, TradeLeg, Exchange
from funding_bot.services.positions.manager import PositionManager
from funding_bot.services.positions.types import CloseResult
from funding_bot.domain.rules import ExitDecision


def _make_test_trade(
    leg1_qty: Decimal = Decimal("0.1"),
    leg2_qty: Decimal = Decimal("0.1"),
    leg1_side: Side = Side.SELL,
    leg2_side: Side = Side.BUY,
) -> "Trade":
    """Create a test trade for manager testing."""
    from funding_bot.domain.models import Trade

    leg1 = TradeLeg(
        exchange=Exchange.LIGHTER,
        side=leg1_side,
        qty=leg1_qty,
        filled_qty=leg1_qty,
        entry_price=Decimal("50000"),
    )
    leg2 = TradeLeg(
        exchange=Exchange.X10,
        side=leg2_side,
        qty=leg2_qty,
        filled_qty=leg2_qty,
        entry_price=Decimal("50000"),
    )
    return Trade(
        trade_id="test_manager_001",
        symbol="BTC-PERP",
        leg1=leg1,
        leg2=leg2,
        target_qty=leg1_qty,
        target_notional_usd=Decimal("5000"),
        entry_apy=Decimal("0.40"),
        status=TradeStatus.OPEN,
    )


@pytest.mark.asyncio
async def test_check_trades_does_not_count_rebalance_as_closed():
    """
    Test that check_trades() does NOT count rebalanced trades as closed.

    Scenario:
    1. Trade is OPEN
    2. close_trade returns success=True (rebalance completed)
    3. Trade status remains OPEN (rebalance doesn't close)
    4. Expected: closed_trades should be empty

    This verifies the fix for the bug where rebalanced trades were incorrectly
    counted as closed just because result.success was True.
    """
    # Create mock service with all required dependencies
    service = MagicMock(spec=PositionManager)
    service.settings = MagicMock()
    service.store = MagicMock()
    service.event_bus = MagicMock()
    service.lighter = MagicMock()
    service.x10 = MagicMock()
    service.market_data = MagicMock()
    service.opportunity_engine = None
    service._orderbook_subs_lock = asyncio.Lock()
    service._active_orderbook_subs = set()

    # Create test trade
    trade = _make_test_trade()
    trade.status = TradeStatus.OPEN  # Rebalance keeps trade OPEN

    # Mock close_trade to return REBALANCED result with OPEN status
    async def mock_close_trade(trade, reason):
        # Simulate rebalance: returns success but trade stays OPEN
        return CloseResult(success=True, realized_pnl=Decimal("0"), reason="REBALANCED")

    service.close_trade = mock_close_trade

    # Mock list_open_trades to return our test trade
    async def mock_list_open_trades():
        return [trade]

    service.store.list_open_trades = mock_list_open_trades

    # Mock other dependencies
    async def mock_handle_broken_hedge(trade):
        return False

    async def mock_check_imbalance(trade):
        pass

    async def mock_evaluate_exit(trade, best_opp_apy):
        # Don't trigger exit for rebalance test
        return ExitDecision(should_exit=False, reason="")

    service._handle_broken_hedge_if_needed = mock_handle_broken_hedge
    service._check_position_imbalance = mock_check_imbalance
    service._evaluate_exit = mock_evaluate_exit

    # Bind the method to the mock service
    import types
    from funding_bot.services.positions import manager
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    # Call check_trades
    closed_trades = await service.check_trades()

    # Verify rebalanced trade is NOT counted as closed
    assert len(closed_trades) == 0, (
        f"Rebalanced trade (status=OPEN) should not be counted as closed, "
        f"got {len(closed_trades)}: {[t.symbol for t in closed_trades]}"
    )
    assert trade.status == TradeStatus.OPEN, "Trade should remain OPEN after rebalance"


@pytest.mark.asyncio
async def test_check_trades_counts_full_close_as_closed():
    """
    Test that check_trades() DOES count fully closed trades.

    Scenario:
    1. Trade is OPEN
    2. Exit condition triggers
    3. close_trade marks trade as CLOSED
    4. Expected: closed_trades should contain the trade

    This verifies that the fix doesn't break the normal case where
    trades are actually closed and should be counted.

    NOTE: This test uses direct async functions instead of MagicMock + MethodType
    binding to work properly with the parallel exit evaluation implementation.
    """
    # Create test trade - start as OPEN
    trade = _make_test_trade()
    trade.status = TradeStatus.OPEN

    # Mock close_trade to mark trade as CLOSED
    async def mock_close_trade(t, reason):
        t.status = TradeStatus.CLOSED
        return CloseResult(success=True, realized_pnl=Decimal("5"), reason="NetEV")

    # Mock _evaluate_exits_parallel to return exit decision
    async def mock_evaluate_exits_parallel(trades, best_opp_apy, max_concurrent=10):
        # Return: (trade, decision, broken_hedge, imbalance_checked)
        return [
            (
                trade,
                ExitDecision(should_exit=True, reason="NetEV: Funding no longer covers exit cost"),
                False,  # broken_hedge
                True,   # imbalance_checked
            )
        ]

    # Create a minimal mock service with direct async functions
    service = MagicMock()
    service.settings = MagicMock()
    service.store = MagicMock()
    service.store.list_open_trades = AsyncMock(return_value=[trade])
    service.close_trade = mock_close_trade
    service._evaluate_exits_parallel = mock_evaluate_exits_parallel
    service._orderbook_subs_lock = asyncio.Lock()
    service._active_orderbook_subs = set()

    # Bind the check_trades method using the actual implementation
    import types
    from funding_bot.services.positions import manager
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    # Call check_trades
    closed_trades = await service.check_trades()

    # Verify CLOSED trade IS counted
    assert len(closed_trades) == 1, (
        f"Trade with status=CLOSED should be counted as closed, got {len(closed_trades)}"
    )
    assert closed_trades[0] == trade, "Closed trade should be the same object"
    assert trade.status == TradeStatus.CLOSED, "Trade should be marked as CLOSED"


@pytest.mark.asyncio
async def test_check_trades_handles_rebalance_then_full_close():
    """
    Test that check_trades() correctly handles rebalance followed by full close.

    Scenario:
    1. First cycle: rebalance (trade remains OPEN, not counted as closed)
    2. Second cycle: full close (trade becomes CLOSED, counted as closed)

    This verifies the fix works correctly across multiple evaluation cycles.

    NOTE: This test uses direct async functions instead of MagicMock + MethodType
    binding to work properly with the parallel exit evaluation implementation.
    """
    # Create test trade
    trade = _make_test_trade()
    trade.status = TradeStatus.OPEN

    # Track call count to simulate different behaviors
    call_count = {"count": 0}

    # Mock close_trade to handle rebalance vs full close
    async def mock_close_trade(t, reason):
        call_count["count"] += 1
        if "REBALANCE" in reason:
            # Rebalance: keep OPEN
            return CloseResult(success=True, realized_pnl=Decimal("0"), reason="REBALANCED")
        else:
            # Full close: mark CLOSED
            t.status = TradeStatus.CLOSED
            return CloseResult(success=True, realized_pnl=Decimal("5"), reason="NetEV")

    # Mock _evaluate_exits_parallel to alternate between rebalance and full close
    async def mock_evaluate_exits_parallel(trades, best_opp_apy, max_concurrent=10):
        if call_count["count"] == 0:
            # First call: rebalance trigger
            decision = ExitDecision(should_exit=True, reason="REBALANCE: Delta drift 2%")
        else:
            # Second call: full close trigger
            decision = ExitDecision(should_exit=True, reason="NetEV: Funding flipped")
        return [(trade, decision, False, True)]

    # Create a minimal mock service with direct async functions
    service = MagicMock()
    service.settings = MagicMock()
    service.store = MagicMock()
    service.store.list_open_trades = AsyncMock(return_value=[trade])
    service.close_trade = mock_close_trade
    service._evaluate_exits_parallel = mock_evaluate_exits_parallel
    service._orderbook_subs_lock = asyncio.Lock()
    service._active_orderbook_subs = set()

    # Bind the check_trades method using the actual implementation
    import types
    from funding_bot.services.positions import manager
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    # First cycle: rebalance (call_count starts at 0, becomes 1 during evaluation)
    call_count["count"] = 0
    closed_trades = await service.check_trades()
    assert len(closed_trades) == 0, "Rebalance should not count as closed"
    assert trade.status == TradeStatus.OPEN, "Trade should remain OPEN after rebalance"

    # Second cycle: full close (call_count is now 1, becomes 2 during evaluation)
    closed_trades = await service.check_trades()
    assert len(closed_trades) == 1, "Full close should be counted as closed"
    assert trade.status == TradeStatus.CLOSED, "Trade should be marked as CLOSED"


@pytest.mark.asyncio
async def test_check_trades_processes_closing_and_open_trades():
    """
    Test that check_trades() correctly processes both OPEN and CLOSING trades.

    - OPEN trades go through exit evaluation path
    - CLOSING trades go through retry path
    - CLOSED trades are filtered out

    Note: OPEN trades only call close_trade if should_exit=True.
    """
    # Create mock service
    service = MagicMock(spec=PositionManager)
    service.settings = MagicMock()
    service.store = MagicMock()
    service.event_bus = MagicMock()
    service.lighter = MagicMock()
    service.x10 = MagicMock()
    service.market_data = MagicMock()
    service.opportunity_engine = None
    service._orderbook_subs_lock = asyncio.Lock()
    service._active_orderbook_subs = set()

    # Create trades in different states
    closing_trade = _make_test_trade()
    closing_trade.trade_id = "closing_001"
    closing_trade.status = TradeStatus.CLOSING
    closing_trade.close_reason = "NetEV"

    closed_trade = _make_test_trade()
    closed_trade.trade_id = "closed_001"
    closed_trade.status = TradeStatus.CLOSED

    all_trades = [closing_trade, closed_trade]

    # Track close_trade calls
    close_trade_calls = []

    async def mock_close_trade(t, reason):
        close_trade_calls.append((t.trade_id, t.status, reason))
        # Only CLOSING trades should reach close_trade in this test
        if t.status == TradeStatus.CLOSING:
            # Simulate retry completing the close
            t.status = TradeStatus.CLOSED
            return CloseResult(success=True, realized_pnl=Decimal("0"), reason="retry_close")
        return CloseResult(success=False, realized_pnl=Decimal("0"), reason="")

    service.close_trade = mock_close_trade

    async def mock_list_open_trades():
        # Return CLOSING and CLOSED trades
        return all_trades

    service.store.list_open_trades = mock_list_open_trades

    async def mock_handle_broken_hedge(trade):
        return False

    async def mock_check_imbalance(trade):
        pass

    async def mock_evaluate_exit(trade, best_opp_apy):
        # No exit triggers for this test
        return ExitDecision(should_exit=False, reason="")

    service._handle_broken_hedge_if_needed = mock_handle_broken_hedge
    service._check_position_imbalance = mock_check_imbalance
    service._evaluate_exit = mock_evaluate_exit

    import types
    from funding_bot.services.positions import manager
    service.check_trades = types.MethodType(manager.PositionManager.check_trades, service)

    # Call check_trades
    closed_trades = await service.check_trades()

    # Verify close_trade was called for CLOSING (retry path)
    close_call_ids = [call[0] for call in close_trade_calls]
    assert closing_trade.trade_id in close_call_ids, "CLOSING trade should be processed (retry path)"
    # CLOSED trades are filtered out by "if trade.status != TradeStatus.OPEN: continue"

    # Verify only successfully CLOSED trades are in the result
    assert len(closed_trades) == 1
    assert closing_trade in closed_trades, "CLOSING trade that became CLOSED should be counted"
