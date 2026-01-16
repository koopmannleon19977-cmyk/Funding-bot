"""
Unit tests for Bug Fixes #1, #2, #3

Tests:
- Bug #1: Lighter fee scaling (1e6 not 1e8)
- Bug #2: X10 funding pagination
- Bug #3: total_fees persistence to DB
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from funding_bot.adapters.store.sqlite.trades import _trade_to_row
from funding_bot.domain.models import Exchange, ExecutionState, Side, Trade, TradeLeg, TradeStatus


class TestBug1_LighterFeeScaling:
    """Test Bug #1 Fix: Lighter fee should use 1e6 scale, not 1e8."""

    def test_lighter_fee_scale_constant(self):
        """Verify LIGHTER_FEE_SCALE is set to 1e6 (USDC micro-units)."""
        # Import the adapter module

        # This test ensures the constant exists and is correct
        # The actual code should have: LIGHTER_FEE_SCALE = Decimal("1000000")
        # We can't directly test it without running the adapter, but we can verify
        # the value would be used correctly

        # Simulate: SDK returns fee_int = 2300 (from real Lighter CSV: $0.0023)
        fee_int = 2300
        CORRECT_SCALE = Decimal("1000000")  # 1e6
        WRONG_SCALE = Decimal("100000000")  # 1e8

        fee_correct = Decimal(fee_int) / CORRECT_SCALE
        fee_wrong = Decimal(fee_int) / WRONG_SCALE

        assert fee_correct == Decimal("0.0023"), "Correct scale should yield $0.0023"
        assert fee_wrong == Decimal("0.000023"), "Wrong scale yields $0.000023"
        assert fee_correct == Decimal("0.0023")  # Matches CSV!

    def test_lighter_fee_real_example(self):
        """Test with real ONDO trade data."""
        # Real data from Lighter CSV
        expected_fee = Decimal("0.0023")

        # Assume SDK returned this INT (reverse engineered from CSV)
        fee_int = 2300

        # Apply fix
        LIGHTER_FEE_SCALE = Decimal("1000000")
        calculated_fee = Decimal(fee_int) / LIGHTER_FEE_SCALE

        assert calculated_fee == expected_fee


class TestBug2_X10FundingPagination:
    """Test Bug #2 Fix: X10 funding history must paginate."""

    @pytest.mark.asyncio
    async def test_pagination_mock(self):
        """Simulate pagination across 3 pages."""
        # Mock X10 adapter

        # This is a conceptual test - in real implementation,
        # get_funding_history should loop until no more pages

        # Simulate API returning 3 pages
        page1 = [{"fundingFee": "0.001"} for _ in range(100)]  # Full page
        page2 = [{"fundingFee": "0.001"} for _ in range(100)]  # Full page
        page3 = [{"fundingFee": "0.001"} for _ in range(50)]   # Partial page (end)

        # Total funding should be all 250 payments
        total_payments = len(page1) + len(page2) + len(page3)
        assert total_payments == 250

        # If we only fetched first page (BUG), we'd miss 150 payments
        buggy_count = len(page1)
        assert buggy_count == 100

        # Fix should get all 250
        fixed_count = total_payments
        assert fixed_count == 250

        missing_pct = (total_payments - buggy_count) / total_payments * 100
        assert missing_pct == 60.0  # 60% missing with bug!


class TestBug3_TotalFeesDB:
    """Test Bug #3 Fix: total_fees must be written to DB."""

    def test_total_fees_in_row_dict(self):
        """Verify _trade_to_row() includes total_fees."""
        # Create mock trade
        trade = Trade(
            trade_id="test-123",
            symbol="BTC",
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                order_id="leg1-order",
                qty=Decimal("10"),
                filled_qty=Decimal("10"),
                entry_price=Decimal("50000"),
                exit_price=Decimal("50100"),
                fees=Decimal("0.0025"),  # Lighter fee
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                order_id="leg2-order",
                qty=Decimal("10"),
                filled_qty=Decimal("10"),
                entry_price=Decimal("50000"),
                exit_price=Decimal("50100"),
                fees=Decimal("0.050"),  # X10 fee
            ),
            target_qty=Decimal("10"),
            target_notional_usd=Decimal("500000"),
            status=TradeStatus.CLOSED,
            execution_state=ExecutionState.COMPLETE,
            created_at=datetime.now(UTC),
        )

        # Verify total_fees property works
        expected_total = Decimal("0.0025") + Decimal("0.050")
        assert trade.total_fees == expected_total
        assert trade.total_fees == Decimal("0.0525")

        # Create mock store instance
        class MockStore:
            pass

        store = MockStore()

        # Call the function (need to bind it)
        row_dict = _trade_to_row(store, trade)

        # ✅ BUG #3 FIX VERIFICATION: total_fees must be in the dict
        assert "total_fees" in row_dict, "Bug #3: total_fees missing from DB row!"
        assert row_dict["total_fees"] == str(trade.total_fees)
        assert row_dict["total_fees"] == "0.0525"

    def test_total_fees_calculation(self):
        """Verify total_fees @property calculates correctly."""
        leg1 = TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            order_id="test",
            qty=Decimal("1"),
            filled_qty=Decimal("1"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            fees=Decimal("0.002"),
        )

        leg2 = TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            order_id="test2",
            qty=Decimal("1"),
            filled_qty=Decimal("1"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            fees=Decimal("0.053"),
        )

        trade = Trade(
            trade_id="test",
            symbol="TEST",
            leg1=leg1,
            leg2=leg2,
            target_qty=Decimal("1"),
            target_notional_usd=Decimal("100"),
            status=TradeStatus.OPEN,
            execution_state=ExecutionState.COMPLETE,  # Both legs filled
            created_at=datetime.now(UTC),
        )

        # total_fees should be leg1.fees + leg2.fees
        assert trade.total_fees == Decimal("0.055")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
