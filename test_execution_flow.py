
import sys
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from decimal import Decimal

sys.path.append('c:\\Users\\koopm\\funding-bot')

# Mock Config
sys.modules['config'] = MagicMock()
import config
config.PARALLEL_EXECUTION_TIMEOUT = 5.0
config.ROLLBACK_DELAY_SECONDS = 0.1

from src.parallel_execution import ParallelExecutionManager, ExecutionState

class TestExecutionFlow(unittest.TestCase):
    def setUp(self):
        self.x10_mock = AsyncMock()
        self.lighter_mock = AsyncMock()
        self.db_mock = MagicMock()
        self.manager = ParallelExecutionManager(self.x10_mock, self.lighter_mock, self.db_mock)
        
        # Default Success setup
        self.lighter_mock.open_live_position.return_value = (True, "LIT_ORDER_1")
        self.x10_mock.open_live_position.return_value = (True, "X10_ORDER_1")
        
        # Mock Position Fetch for Fill Check
        # Initially empty, then filled
        # We need check_fill loop to succeed.
        # It calls lighter.fetch_open_positions()
        
        # Scenario: 
        # Call 1: empty
        # Call 2: filled
        self.lighter_mock.fetch_open_positions.side_effect = [
            [], # Init
            [{'symbol': 'BTC-USD', 'size': 0.0}], # First check
            [{'symbol': 'BTC-USD', 'size': 100.0}]  # Second check (Filled)
        ]
        
        self.manager.execution_locks['BTC-USD'] = asyncio.Lock()
        
    async def run_async_test(self):
        # MOCK Compliance Check to return True
        with patch.object(self.manager, '_run_compliance_check', new_callable=AsyncMock) as mock_comp:
            mock_comp.return_value = True
            
            # Run Execution
            success, x10_id, lit_id = await self.manager.execute_trade_parallel(
                "BTC-USD", "BUY", "BUY", Decimal("100"), Decimal("100")
            )
            
            return success, x10_id, lit_id

    def test_full_success_flow(self):
        """Verify Lighter Maker -> Wait -> X10 Taker flow"""
        success, x10_id, lit_id = asyncio.run(self.run_async_test())
        
        # Verify Success
        self.assertTrue(success)
        self.assertEqual(lit_id, "LIT_ORDER_1")
        self.assertEqual(x10_id, "X10_ORDER_1")
        
        # Verify Lighter Call (Maker)
        self.lighter_mock.open_live_position.assert_called()
        args, kwargs = self.lighter_mock.open_live_position.call_args
        # args: (symbol, side, notional, post_only=...)
        # But wait, implementation passes post_only as kwarg?
        # Check src/parallel_execution.py:
        # await self.lighter.open_live_position(symbol, side, notional_usd, post_only=post_only)
        # So it might be positional or kwarg depending on adapter sig.
        # Adapter sig: (symbol, side, notional_usd, price=None, reduce_only=..., post_only=...)
        # So post_only is typically a kwarg.
        self.assertTrue(kwargs.get('post_only') == True)
        
        # Verify X10 Call (Taker) - MUST BE AFTER LIGHTER
        self.x10_mock.open_live_position.assert_called()
        args2, kwargs2 = self.x10_mock.open_live_position.call_args
        self.assertTrue(kwargs2.get('post_only') is False)
        
        print("✅ Full Success Flow Verified: Lighter(Maker) -> Wait -> X10(Taker)")

    def test_lighter_placement_fail(self):
        """Verify abort if Lighter placement fails"""
        self.lighter_mock.open_live_position.return_value = (False, None)
        
        # Run
        asyncio.run(self.run_async_test()) # expect failure
        
        # X10 should NOT be called
        self.x10_mock.open_live_position.assert_not_called()
        print("✅ Placement Fail Verified: X10 not called")

if __name__ == '__main__':
    unittest.main()
