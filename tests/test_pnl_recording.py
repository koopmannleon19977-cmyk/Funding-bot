"""
Tests for PnL Recording and Calculation

This module tests:
- Task 1.2: PnL Recording Fix (values persisted to database)
- PnL computation for LONG/SHORT positions with fills
- Sign correctness for various scenarios
"""
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pnl_utils import compute_realized_pnl, compute_hedge_pnl, sum_closed_pnl_from_csv


class TestPnLRecording:
    """Test that PnL values are correctly persisted to the database."""
    
    @pytest.mark.asyncio
    async def test_pnl_recording_basic(self):
        """Test basic PnL recording to database."""
        # Import after path setup
        from src.state_manager import InMemoryStateManager, TradeState, TradeStatus
        
        db_path = "data/trades.db"
        sm = InMemoryStateManager(db_path=db_path)
        
        print("\n1. Starting State Manager...")
        await sm.start()
        
        # Create a unique test symbol
        test_symbol = f"TEST-PNL-{int(time.time())}"
        test_pnl = 3.75
        test_funding = 0.42
        
        print(f"\n2. Creating test trade: {test_symbol}")
        
        # Create a test trade
        test_trade = TradeState(
            symbol=test_symbol,
            side_x10="BUY",
            side_lighter="SELL",
            size_usd=500.0,
            entry_price_x10=1.0,
            entry_price_lighter=1.0,
            status=TradeStatus.OPEN,
            created_at=int(time.time() * 1000) - 10000,
        )
        
        await sm.add_trade(test_trade)
        print(f"   ✅ Trade added to state")
        
        # Now close with PnL
        print(f"\n3. Closing trade with PnL=${test_pnl:.2f}, Funding=${test_funding:.2f}")
        await sm.close_trade(test_symbol, pnl=test_pnl, funding=test_funding)
        print(f"   ✅ Trade closed in state")
        
        # Wait for write-behind to flush
        print(f"\n4. Waiting for database write...")
        await asyncio.sleep(2)
        await sm._flush_writes()
        print(f"   ✅ Writes flushed")
        
        # Stop the state manager
        await sm.stop()
        print(f"\n5. State Manager stopped")
        
        # Verify in database
        print(f"\n6. Verifying in database...")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT symbol, pnl, funding_collected, closed_at FROM trades WHERE symbol = ?",
            (test_symbol,)
        )
        row = cursor.fetchone()
        
        try:
            assert row is not None, "Trade not found in database"
            db_symbol, db_pnl, db_funding, db_closed = row
            
            assert abs(db_pnl - test_pnl) < 0.01, f"PnL mismatch: expected {test_pnl}, got {db_pnl}"
            assert abs(db_funding - test_funding) < 0.01, f"Funding mismatch: expected {test_funding}, got {db_funding}"
            
            print(f"\n✅ TASK 1.2 VERIFIED: PnL Recording is WORKING!")
            print(f"   Expected PnL: ${test_pnl:.2f} → Got: ${db_pnl:.2f}")
            print(f"   Expected Funding: ${test_funding:.2f} → Got: ${db_funding:.2f}")
        finally:
            # Cleanup test trade
            cursor.execute("DELETE FROM trades WHERE symbol = ?", (test_symbol,))
            conn.commit()
            conn.close()
            print(f"\n7. Test trade cleaned up from database")


class TestPnLComputation:
    """Test PnL computation with specific scenarios from real trading."""
    
    def test_long_position_loss_with_taker_fee(self):
        """
        Test LONG position with loss and taker fee.
        
        Scenario:
        - LONG entry at 0.4054
        - Close at 0.4052
        - Taker fee: 0.000225 USD per side
        - Quantity: 197
        
        Expected:
        - Price PnL: (0.4052 - 0.4054) * 197 = -0.0394
        - Total fees: 0.000225 * 2 = 0.00045
        - Total PnL: -0.0394 - 0.00045 = -0.03985
        """
        entry_fills = [{"price": 0.4054, "qty": 197, "fee": 0.000225, "is_taker": True}]
        close_fills = [{"price": 0.4052, "qty": 197, "fee": 0.000225, "is_taker": True}]
        
        result = compute_realized_pnl("TAO-USD", "LONG", entry_fills, close_fills)
        
        # Price PnL check
        expected_price_pnl = (0.4052 - 0.4054) * 197
        assert abs(result["price_pnl"] - expected_price_pnl) < 0.0001, \
            f"Price PnL: expected {expected_price_pnl}, got {result['price_pnl']}"
        
        # Fee check
        expected_fees = 0.000225 + 0.000225
        assert abs(result["fee_total"] - expected_fees) < 0.0001, \
            f"Fees: expected {expected_fees}, got {result['fee_total']}"
        
        # Total PnL check (should be negative)
        expected_total = expected_price_pnl - expected_fees
        assert abs(result["total_pnl"] - expected_total) < 0.0001, \
            f"Total PnL: expected {expected_total}, got {result['total_pnl']}"
        
        # Verify it's negative (loss)
        assert result["total_pnl"] < 0, "Expected negative PnL (loss)"
        
        print(f"\n✅ LONG loss test passed:")
        print(f"   Price PnL: ${result['price_pnl']:.6f}")
        print(f"   Fees: ${result['fee_total']:.6f}")
        print(f"   Total: ${result['total_pnl']:.6f}")
    
    def test_short_position_profit_no_fee(self):
        """
        Test SHORT position with profit and no fee.
        
        Scenario:
        - SHORT entry at 0.4053
        - Close at 0.40518
        - No fees (maker)
        - Quantity: 197
        
        Expected:
        - Price PnL: (0.4053 - 0.40518) * 197 = +0.02364
        - Total fees: 0
        - Total PnL: +0.02364
        """
        entry_fills = [{"price": 0.4053, "qty": 197, "fee": 0, "is_taker": False}]
        close_fills = [{"price": 0.40518, "qty": 197, "fee": 0, "is_taker": False}]
        
        result = compute_realized_pnl("TAO-USD", "SHORT", entry_fills, close_fills)
        
        # Price PnL check (SHORT: entry - close)
        expected_price_pnl = (0.4053 - 0.40518) * 197
        assert abs(result["price_pnl"] - expected_price_pnl) < 0.0001, \
            f"Price PnL: expected {expected_price_pnl}, got {result['price_pnl']}"
        
        # Fee check
        assert result["fee_total"] == 0, f"Fees should be 0, got {result['fee_total']}"
        
        # Total PnL check (should be positive)
        expected_total = expected_price_pnl
        assert abs(result["total_pnl"] - expected_total) < 0.0001, \
            f"Total PnL: expected {expected_total}, got {result['total_pnl']}"
        
        # Verify it's positive (profit)
        assert result["total_pnl"] > 0, "Expected positive PnL (profit)"
        
        print(f"\n✅ SHORT profit test passed:")
        print(f"   Price PnL: ${result['price_pnl']:.6f}")
        print(f"   Fees: ${result['fee_total']:.6f}")
        print(f"   Total: ${result['total_pnl']:.6f}")
    
    def test_combined_hedge_pnl(self):
        """
        Test combined hedge PnL computation.
        
        Combining the two previous scenarios:
        - LONG on X10: entry 0.4054, close 0.4052, qty 197, fee 0.000225
        - SHORT on Lighter: entry 0.4053, close 0.40518, qty 197, fee 0
        
        The hedge should have a combined PnL.
        """
        # Using compute_hedge_pnl
        result = compute_hedge_pnl(
            symbol="TAO-USD",
            lighter_side="SHORT",
            x10_side="LONG",
            lighter_entry_price=0.4053,
            lighter_close_price=0.40518,
            lighter_qty=197,
            x10_entry_price=0.4054,
            x10_close_price=0.4052,
            x10_qty=197,
            lighter_fees=0,
            x10_fees=0.00045,  # Entry + exit
            funding_collected=0.0,
        )
        
        # X10 LONG: (0.4052 - 0.4054) * 197 = -0.0394
        expected_x10_pnl = (0.4052 - 0.4054) * 197
        assert abs(result["x10_pnl"] - expected_x10_pnl) < 0.0001
        
        # Lighter SHORT: (0.4053 - 0.40518) * 197 = +0.02364
        expected_lighter_pnl = (0.4053 - 0.40518) * 197
        assert abs(result["lighter_pnl"] - expected_lighter_pnl) < 0.0001
        
        # Combined price PnL
        expected_price_total = expected_x10_pnl + expected_lighter_pnl
        assert abs(result["price_pnl_total"] - expected_price_total) < 0.0001
        
        # Total with fees
        expected_total = expected_price_total - 0.00045
        assert abs(result["total_pnl"] - expected_total) < 0.0001
        
        print(f"\n✅ Hedge PnL test passed:")
        print(f"   X10 LONG PnL: ${result['x10_pnl']:.6f}")
        print(f"   Lighter SHORT PnL: ${result['lighter_pnl']:.6f}")
        print(f"   Price Total: ${result['price_pnl_total']:.6f}")
        print(f"   Fees: ${result['fee_total']:.6f}")
        print(f"   Total: ${result['total_pnl']:.6f}")
    
    def test_long_position_profit(self):
        """Test LONG position with profit."""
        entry_fills = [{"price": 100.0, "qty": 10, "fee": 0.5, "is_taker": True}]
        close_fills = [{"price": 105.0, "qty": 10, "fee": 0.5, "is_taker": True}]
        
        result = compute_realized_pnl("BTC-USD", "LONG", entry_fills, close_fills)
        
        # LONG: (105 - 100) * 10 = +50
        expected_price = 50.0
        expected_fees = 1.0
        expected_total = 49.0
        
        assert abs(result["price_pnl"] - expected_price) < 0.01
        assert abs(result["fee_total"] - expected_fees) < 0.01
        assert abs(result["total_pnl"] - expected_total) < 0.01
        assert result["total_pnl"] > 0  # Profit
    
    def test_short_position_loss(self):
        """Test SHORT position with loss."""
        entry_fills = [{"price": 100.0, "qty": 10, "fee": 0.5, "is_taker": True}]
        close_fills = [{"price": 105.0, "qty": 10, "fee": 0.5, "is_taker": True}]
        
        result = compute_realized_pnl("BTC-USD", "SHORT", entry_fills, close_fills)
        
        # SHORT: (100 - 105) * 10 = -50
        expected_price = -50.0
        expected_fees = 1.0
        expected_total = -51.0
        
        assert abs(result["price_pnl"] - expected_price) < 0.01
        assert abs(result["fee_total"] - expected_fees) < 0.01
        assert abs(result["total_pnl"] - expected_total) < 0.01
        assert result["total_pnl"] < 0  # Loss
    
    def test_multiple_fills(self):
        """Test PnL with multiple fills (averaging)."""
        entry_fills = [
            {"price": 100.0, "qty": 5, "fee": 0.25, "is_taker": True},
            {"price": 102.0, "qty": 5, "fee": 0.25, "is_taker": True},
        ]
        close_fills = [
            {"price": 105.0, "qty": 10, "fee": 0.5, "is_taker": True},
        ]
        
        result = compute_realized_pnl("ETH-USD", "LONG", entry_fills, close_fills)
        
        # Entry VWAP: (100*5 + 102*5) / 10 = 101
        # LONG: (105 - 101) * 10 = +40
        expected_price = 40.0
        expected_fees = 1.0  # 0.25 + 0.25 + 0.5
        expected_total = 39.0
        
        assert abs(result["entry_vwap"] - 101.0) < 0.01
        assert abs(result["price_pnl"] - expected_price) < 0.01
        assert abs(result["total_pnl"] - expected_total) < 0.01


class TestCSVParsing:
    """Test CSV parsing and PnL summation."""
    
    def test_sum_closed_pnl_from_csv(self):
        """Test sum_closed_pnl_from_csv with sample CSV rows."""
        csv_rows = [
            {"Market": "TAO", "Side": "Buy", "Closed PnL": "-", "Size": "100", "Price": "0.40"},
            {"Market": "TAO", "Side": "Close Short", "Closed PnL": "-0.0123", "Size": "100", "Price": "0.40", "Date": "2024-01-01"},
            {"Market": "TAO", "Side": "Close Short", "Closed PnL": "0.0456", "Size": "100", "Price": "0.41", "Date": "2024-01-02"},
            {"Market": "ETH", "Side": "Close Long", "Closed PnL": "5.50", "Size": "1", "Price": "2000", "Date": "2024-01-01"},
            {"Market": "ETH", "Side": "Close Long", "Closed PnL": "", "Size": "1", "Price": "2000", "Date": "2024-01-02"},  # Empty PnL
            {"Market": "BTC", "Side": "Sell", "Closed PnL": "10.00", "Size": "0.1", "Price": "50000"},  # Not a close
        ]
        
        result = sum_closed_pnl_from_csv(csv_rows)
        
        # TAO-USD: -0.0123 + 0.0456 = 0.0333
        assert "TAO-USD" in result
        assert abs(result["TAO-USD"]["total_pnl"] - 0.0333) < 0.0001
        assert result["TAO-USD"]["trade_count"] == 2
        
        # ETH-USD: 5.50 only (empty PnL ignored)
        assert "ETH-USD" in result
        assert abs(result["ETH-USD"]["total_pnl"] - 5.50) < 0.01
        assert result["ETH-USD"]["trade_count"] == 1
        
        # BTC-USD: Should not be present (not a close trade)
        assert "BTC-USD" not in result
        
        print(f"\n✅ CSV parsing test passed:")
        for symbol, data in result.items():
            print(f"   {symbol}: total=${data['total_pnl']:.4f}, trades={data['trade_count']}")
    
    def test_empty_csv(self):
        """Test with empty CSV rows."""
        result = sum_closed_pnl_from_csv([])
        assert result == {}
    
    def test_no_close_trades(self):
        """Test CSV with no close trades."""
        csv_rows = [
            {"Market": "BTC", "Side": "Buy", "Closed PnL": "-"},
            {"Market": "ETH", "Side": "Sell", "Closed PnL": "10.00"},
        ]
        result = sum_closed_pnl_from_csv(csv_rows)
        assert result == {}


if __name__ == "__main__":
    # Run basic tests without pytest
    print("Running PnL computation tests...\n")
    
    test_class = TestPnLComputation()
    test_class.test_long_position_loss_with_taker_fee()
    test_class.test_short_position_profit_no_fee()
    test_class.test_combined_hedge_pnl()
    test_class.test_long_position_profit()
    test_class.test_short_position_loss()
    test_class.test_multiple_fills()
    
    print("\nRunning CSV parsing tests...\n")
    csv_test = TestCSVParsing()
    csv_test.test_sum_closed_pnl_from_csv()
    csv_test.test_empty_csv()
    csv_test.test_no_close_trades()
    
    print("\n✅ All tests passed!")
