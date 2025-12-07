
import sys
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock

sys.path.append('c:\\Users\\koopm\\funding-bot')

# Mock Config (simulate loaded value)
sys.modules['config'] = MagicMock()
import config
config.MAX_TOTAL_EXPOSURE_PCT = 18.0 # 18x

from src.main import check_total_exposure

class TestExposureCheck(unittest.TestCase):
    def test_exposure_allowance(self):
        """Test if 2x leverage trade is allowed with 18x limit"""
        
        # Setup Mocks
        x10_mock = AsyncMock()
        x10_mock.get_real_available_balance.return_value = 250.0 # $250 Balance
        
        lighter_mock = AsyncMock()
        lighter_mock.get_real_available_balance.return_value = 250.0
        
        # Mock get_open_trades to return empty
        with unittest.mock.patch('src.main.get_open_trades', new_callable=AsyncMock) as mock_trades:
            mock_trades.return_value = []
            
            # Action: Check exposure for $500 trade (2x Leverage)
            can_trade, pct, limit = asyncio.run(check_total_exposure(x10_mock, lighter_mock, 500.0))
            
            print(f"Exposure Check: Can Trade={can_trade}, Pct={pct:.2f}, Limit={limit:.2f}")
            
            self.assertTrue(can_trade)
            self.assertLess(pct, limit)
            print("✅ Exposure Check Passed!")

if __name__ == '__main__':
    unittest.main()
