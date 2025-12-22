"""
Unit tests for src/pnl_utils.py

Tests:
- compute_realized_pnl: LONG/SHORT position PnL with fees
- compute_hedge_pnl: Combined hedge PnL for funding arbitrage
- sum_closed_pnl_from_csv: CSV parsing and PnL summation
- reconcile_csv_vs_db: Comparison and mismatch detection
- normalize_funding_sign: Funding sign normalization

NOTE: compute_realized_pnl and compute_hedge_pnl return Decimal values (H5 migration).
Tests use float() conversion for comparison assertions.
"""
import sys
from pathlib import Path
from decimal import Decimal

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pnl_utils import (
    compute_realized_pnl,
    compute_hedge_pnl,
    sum_closed_pnl_from_csv,
    reconcile_csv_vs_db,
    normalize_funding_sign,
    _side_sign,
    _safe_float,
    _safe_decimal,
)


class TestSideSign:
    """Test _side_sign helper function."""
    
    def test_long_variants(self):
        """Test LONG side detection."""
        assert _side_sign("LONG") == 1
        assert _side_sign("long") == 1
        assert _side_sign("BUY") == 1
        assert _side_sign("buy") == 1
        assert _side_sign("Buy") == 1
    
    def test_short_variants(self):
        """Test SHORT side detection."""
        assert _side_sign("SHORT") == -1
        assert _side_sign("short") == -1
        assert _side_sign("SELL") == -1
        assert _side_sign("sell") == -1
        assert _side_sign("Sell") == -1
    
    def test_unknown(self):
        """Test unknown side returns 0."""
        assert _side_sign("") == 0
        assert _side_sign(None) == 0
        assert _side_sign("UNKNOWN") == 0


class TestSafeFloat:
    """Test _safe_float helper function."""
    
    def test_numeric_values(self):
        assert _safe_float(1.5) == 1.5
        assert _safe_float(100) == 100.0
        assert _safe_float(-5.5) == -5.5
    
    def test_string_values(self):
        assert _safe_float("1.5") == 1.5
        assert _safe_float("  2.5  ") == 2.5
        assert _safe_float("-3.14") == -3.14
    
    def test_invalid_values(self):
        assert _safe_float(None) == 0.0
        assert _safe_float("invalid") == 0.0
        assert _safe_float("") == 0.0
        assert _safe_float(None, 99.0) == 99.0


class TestComputeRealizedPnL:
    """Test compute_realized_pnl function."""
    
    def test_long_profit(self):
        """LONG position with profit."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 110.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "LONG", entry, close)
        
        # (110 - 100) * 10 = 100, fees = 1.0
        # Results are Decimal, convert to float for comparison
        assert abs(float(result["price_pnl"]) - 100.0) < 0.01
        assert abs(float(result["fee_total"]) - 1.0) < 0.01
        assert abs(float(result["total_pnl"]) - 99.0) < 0.01
    
    def test_long_loss(self):
        """LONG position with loss."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 90.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "LONG", entry, close)
        
        # (90 - 100) * 10 = -100, fees = 1.0
        assert abs(float(result["price_pnl"]) - (-100.0)) < 0.01
        assert abs(float(result["total_pnl"]) - (-101.0)) < 0.01
        assert float(result["total_pnl"]) < 0
    
    def test_short_profit(self):
        """SHORT position with profit."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 90.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "SHORT", entry, close)
        
        # (100 - 90) * 10 = 100, fees = 1.0
        assert abs(float(result["price_pnl"]) - 100.0) < 0.01
        assert abs(float(result["total_pnl"]) - 99.0) < 0.01
        assert float(result["total_pnl"]) > 0
    
    def test_short_loss(self):
        """SHORT position with loss."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 110.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "SHORT", entry, close)
        
        # (100 - 110) * 10 = -100, fees = 1.0
        assert abs(float(result["price_pnl"]) - (-100.0)) < 0.01
        assert abs(float(result["total_pnl"]) - (-101.0)) < 0.01
        assert float(result["total_pnl"]) < 0
    
    def test_multiple_entry_fills_vwap(self):
        """Test VWAP calculation with multiple entry fills."""
        entry = [
            {"price": 100.0, "qty": 5, "fee": 0.25},
            {"price": 110.0, "qty": 5, "fee": 0.25},
        ]
        close = [{"price": 120.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "LONG", entry, close)
        
        # Entry VWAP: (100*5 + 110*5) / 10 = 105
        assert abs(float(result["entry_vwap"]) - 105.0) < 0.01
        # (120 - 105) * 10 = 150, fees = 1.0
        assert abs(float(result["price_pnl"]) - 150.0) < 0.01
        assert abs(float(result["total_pnl"]) - 149.0) < 0.01
    
    def test_partial_close(self):
        """Test partial close (close qty < entry qty)."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 110.0, "qty": 5, "fee": 0.25}]
        
        result = compute_realized_pnl("TEST-USD", "LONG", entry, close)
        
        # Uses min(10, 5) = 5 for PnL
        # (110 - 100) * 5 = 50, fees = 0.75
        assert abs(float(result["price_pnl"]) - 50.0) < 0.01
        assert abs(float(result["qty"]) - 5.0) < 0.01
    
    def test_zero_fee(self):
        """Test with zero fees (maker)."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0}]
        close = [{"price": 110.0, "qty": 10, "fee": 0}]
        
        result = compute_realized_pnl("TEST-USD", "LONG", entry, close)
        
        assert float(result["fee_total"]) == 0
        assert float(result["price_pnl"]) == float(result["total_pnl"])
    
    def test_unknown_side(self):
        """Test with unknown side returns zeros."""
        entry = [{"price": 100.0, "qty": 10, "fee": 0.5}]
        close = [{"price": 110.0, "qty": 10, "fee": 0.5}]
        
        result = compute_realized_pnl("TEST-USD", "UNKNOWN", entry, close)
        
        assert float(result["price_pnl"]) == 0.0
        assert float(result["total_pnl"]) == 0.0
    
    def test_empty_fills(self):
        """Test with empty fills."""
        result = compute_realized_pnl("TEST-USD", "LONG", [], [])
        
        assert float(result["price_pnl"]) == 0.0
        assert float(result["total_pnl"]) == 0.0


class TestComputeHedgePnL:
    """Test compute_hedge_pnl function."""
    
    def test_classic_funding_arb(self):
        """Test classic funding arb: LONG X10 / SHORT Lighter."""
        result = compute_hedge_pnl(
            symbol="TEST-USD",
            lighter_side="SHORT",
            x10_side="LONG",
            lighter_entry_price=100.0,
            lighter_close_price=102.0,
            lighter_qty=10,
            x10_entry_price=100.0,
            x10_close_price=102.0,
            x10_qty=10,
            lighter_fees=0.5,
            x10_fees=0.5,
            funding_collected=5.0,
        )
        
        # Results are Decimal, convert to float for comparison
        # X10 LONG: (102 - 100) * 10 = +20
        assert abs(float(result["x10_pnl"]) - 20.0) < 0.01
        # Lighter SHORT: (100 - 102) * 10 = -20
        assert abs(float(result["lighter_pnl"]) - (-20.0)) < 0.01
        # Price total: 20 + (-20) = 0
        assert abs(float(result["price_pnl_total"])) < 0.01
        # Total: 0 - 1.0 + 5.0 = 4.0
        assert abs(float(result["total_pnl"]) - 4.0) < 0.01
    
    def test_reverse_funding_arb(self):
        """Test reverse funding arb: SHORT X10 / LONG Lighter."""
        result = compute_hedge_pnl(
            symbol="TEST-USD",
            lighter_side="LONG",
            x10_side="SHORT",
            lighter_entry_price=100.0,
            lighter_close_price=105.0,
            lighter_qty=10,
            x10_entry_price=100.0,
            x10_close_price=105.0,
            x10_qty=10,
            lighter_fees=0.5,
            x10_fees=0.5,
            funding_collected=3.0,
        )
        
        # Lighter LONG: (105 - 100) * 10 = +50
        assert abs(float(result["lighter_pnl"]) - 50.0) < 0.01
        # X10 SHORT: (100 - 105) * 10 = -50
        assert abs(float(result["x10_pnl"]) - (-50.0)) < 0.01
        # Total: 0 - 1.0 + 3.0 = 2.0
        assert abs(float(result["total_pnl"]) - 2.0) < 0.01
    
    def test_basis_trade_profit(self):
        """Test basis trade where basis converges."""
        # Entry: Lighter at 100, X10 at 99 (basis = 1)
        # Close: Lighter at 100, X10 at 100 (basis = 0)
        result = compute_hedge_pnl(
            symbol="TEST-USD",
            lighter_side="SHORT",
            x10_side="LONG",
            lighter_entry_price=100.0,
            lighter_close_price=100.0,
            lighter_qty=10,
            x10_entry_price=99.0,
            x10_close_price=100.0,
            x10_qty=10,
            lighter_fees=0,
            x10_fees=0,
            funding_collected=0,
        )
        
        # X10 LONG: (100 - 99) * 10 = +10
        assert abs(float(result["x10_pnl"]) - 10.0) < 0.01
        # Lighter SHORT: (100 - 100) * 10 = 0
        assert abs(float(result["lighter_pnl"])) < 0.01
        # Total: 10
        assert abs(float(result["total_pnl"]) - 10.0) < 0.01


class TestSumClosedPnLFromCSV:
    """Test sum_closed_pnl_from_csv function."""
    
    def test_basic_parsing(self):
        """Test basic CSV parsing."""
        rows = [
            {"Market": "TAO", "Side": "Close Short", "Closed PnL": "1.50"},
            {"Market": "TAO", "Side": "Close Short", "Closed PnL": "-0.50"},
        ]
        
        result = sum_closed_pnl_from_csv(rows)
        
        assert "TAO-USD" in result
        assert abs(result["TAO-USD"]["total_pnl"] - 1.0) < 0.01
        assert result["TAO-USD"]["trade_count"] == 2
    
    def test_symbol_normalization(self):
        """Test that symbols are normalized to -USD suffix."""
        rows = [
            {"Market": "BTC", "Side": "Close Long", "Closed PnL": "100.0"},
            {"Market": "ETH-USD", "Side": "Close Short", "Closed PnL": "50.0"},
        ]
        
        result = sum_closed_pnl_from_csv(rows)
        
        assert "BTC-USD" in result
        assert "ETH-USD" in result
    
    def test_non_close_trades_ignored(self):
        """Test that non-close trades are ignored."""
        rows = [
            {"Market": "BTC", "Side": "Buy", "Closed PnL": "100.0"},
            {"Market": "ETH", "Side": "Sell", "Closed PnL": "50.0"},
            {"Market": "SOL", "Side": "Close Long", "Closed PnL": "25.0"},
        ]
        
        result = sum_closed_pnl_from_csv(rows)
        
        assert "BTC-USD" not in result
        assert "ETH-USD" not in result
        assert "SOL-USD" in result
    
    def test_empty_and_dash_pnl(self):
        """Test that empty and dash PnL values are ignored."""
        rows = [
            {"Market": "BTC", "Side": "Close Long", "Closed PnL": ""},
            {"Market": "ETH", "Side": "Close Short", "Closed PnL": "-"},
            {"Market": "SOL", "Side": "Close Long", "Closed PnL": "10.0"},
        ]
        
        result = sum_closed_pnl_from_csv(rows)
        
        assert "BTC-USD" not in result
        assert "ETH-USD" not in result
        assert "SOL-USD" in result
    
    def test_empty_input(self):
        """Test with empty input."""
        assert sum_closed_pnl_from_csv([]) == {}


class TestReconcileCSVvsDB:
    """Test reconcile_csv_vs_db function."""
    
    def test_perfect_match(self):
        """Test when CSV and DB match perfectly."""
        csv_pnl = {"BTC-USD": {"total_pnl": 100.0}}
        db_pnl = {"BTC-USD": 100.0}
        
        result = reconcile_csv_vs_db(csv_pnl, db_pnl)
        
        assert len(result["matches"]) == 1
        assert len(result["mismatches"]) == 0
    
    def test_mismatch(self):
        """Test when CSV and DB have different values."""
        csv_pnl = {"BTC-USD": {"total_pnl": 100.0}}
        db_pnl = {"BTC-USD": 50.0}
        
        result = reconcile_csv_vs_db(csv_pnl, db_pnl)
        
        assert len(result["matches"]) == 0
        assert len(result["mismatches"]) == 1
        assert result["mismatches"][0]["difference"] == 50.0
    
    def test_missing_in_db(self):
        """Test symbol in CSV but not in DB."""
        csv_pnl = {"BTC-USD": {"total_pnl": 100.0}}
        db_pnl = {}
        
        result = reconcile_csv_vs_db(csv_pnl, db_pnl)
        
        assert len(result["missing_in_db"]) == 1
        assert result["missing_in_db"][0]["symbol"] == "BTC-USD"
    
    def test_missing_in_csv(self):
        """Test symbol in DB but not in CSV."""
        csv_pnl = {}
        db_pnl = {"BTC-USD": 100.0}
        
        result = reconcile_csv_vs_db(csv_pnl, db_pnl)
        
        assert len(result["missing_in_csv"]) == 1
        assert result["missing_in_csv"][0]["symbol"] == "BTC-USD"
    
    def test_tolerance(self):
        """Test that small differences within tolerance are matches."""
        csv_pnl = {"BTC-USD": {"total_pnl": 100.0}}
        db_pnl = {"BTC-USD": 100.005}
        
        result = reconcile_csv_vs_db(csv_pnl, db_pnl, tolerance=0.01)
        
        assert len(result["matches"]) == 1
        assert len(result["mismatches"]) == 0


class TestNormalizeFundingSign:
    """Test normalize_funding_sign function."""
    
    def test_cost_positive_to_profit_positive(self):
        """Test converting cost-positive to profit-positive."""
        # paid_out = 5 means cost, should become -5
        assert normalize_funding_sign(5.0, is_profit_positive=False) == -5.0
        # paid_out = -5 means received, should become 5
        assert normalize_funding_sign(-5.0, is_profit_positive=False) == 5.0
    
    def test_already_profit_positive(self):
        """Test pass-through for profit-positive values."""
        assert normalize_funding_sign(5.0, is_profit_positive=True) == 5.0
        assert normalize_funding_sign(-5.0, is_profit_positive=True) == -5.0
    
    def test_zero(self):
        """Test zero remains zero."""
        assert normalize_funding_sign(0.0, is_profit_positive=False) == 0.0
        assert normalize_funding_sign(0.0, is_profit_positive=True) == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])



