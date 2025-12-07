
import sys
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

# Fix imports
sys.path.append('c:\\Users\\koopm\\funding-bot')

# Mock Config
sys.modules['config'] = MagicMock()
import config
config.MIN_PROFIT_EXIT_USD = 0.05
config.FARM_HOLD_SECONDS = 3600

from src.trading.smart_exit import should_farm_quick_exit

class TestSmartFarm(unittest.TestCase):
    def test_fast_profit_churn(self):
        """Test if it exits quickly on small profit (Volume Churn)"""
        trade = {'entry_time': datetime.now(timezone.utc).isoformat()}
        gross_pnl = 0.06 # > 0.05
        
        exit_bool, reason = should_farm_quick_exit("BTC-USD", trade, 0, gross_pnl)
        self.assertTrue(exit_bool)
        self.assertIn("FARM_PROFIT", reason)
        print(f"✅ Fast Profit Churn Test Passed: {reason}")

    def test_hold_if_no_profit(self):
        """Test if it holds if profit is small and not aged"""
        trade = {'entry_time': datetime.now(timezone.utc).isoformat()}
        gross_pnl = 0.04 # < 0.05
        
        exit_bool, reason = should_farm_quick_exit("BTC-USD", trade, 0, gross_pnl)
        self.assertFalse(exit_bool)
        print(f"✅ Hold Test Passed")

    def test_aged_exit(self):
        """Test if it exits after hold duration (if Break Even)"""
        # Entry 2 hours ago
        entry = datetime.now(timezone.utc) - timedelta(hours=2)
        trade = {'entry_time': entry.isoformat()}
        gross_pnl = 0.00 # BE
        
        exit_bool, reason = should_farm_quick_exit("BTC-USD", trade, 0, gross_pnl)
        self.assertTrue(exit_bool)
        self.assertIn("FARM_AGED_OUT", reason)
        print(f"✅ Aged Exit Test Passed: {reason}")
        
    def test_aged_hold_if_loss(self):
        """Test if it holds even after age if in loss (Don't realize loss)"""
        # Entry 2 hours ago
        entry = datetime.now(timezone.utc) - timedelta(hours=2)
        trade = {'entry_time': entry.isoformat()}
        gross_pnl = -0.10
        
        exit_bool, reason = should_farm_quick_exit("BTC-USD", trade, 0, gross_pnl)
        self.assertFalse(exit_bool)
        print(f"✅ Aged Loss Hold Test Passed")

if __name__ == '__main__':
    unittest.main()
