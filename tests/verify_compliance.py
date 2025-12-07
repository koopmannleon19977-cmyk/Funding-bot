
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock
import sys
import os

# Adjust path to find src
sys.path.append(os.getcwd())

from src.parallel_execution import ParallelExecutionManager
import config

# Mock Config
config.COMPLIANCE_CHECK_ENABLED = True
config.COMPLIANCE_BLOCK_SELF_MATCH = True

async def test_compliance_check():
    print("🛡️ Testing Compliance Check...")
    
    # Mock Adapters
    mock_x10 = MagicMock()
    mock_lit = MagicMock()
    
    mock_x10.get_open_orders = AsyncMock()
    mock_lit.get_open_orders = AsyncMock()
    
    # Mock DB
    mock_db = MagicMock()
    
    pem = ParallelExecutionManager(mock_x10, mock_lit, mock_db)
    
    # Scenario 1: Clean (No open orders)
    print("\n1. Testing Clean Scenario (No Orders)...")
    mock_x10.get_open_orders.return_value = []
    mock_lit.get_open_orders.return_value = []
    
    res = await pem._run_compliance_check('BTC-USD', 'BUY', 'SELL')
    assert res == True
    print("✅ Passed: Allowed trade when no orders exist.")
    
    # Scenario 2: Self-Match on X10 (Buy into Sell)
    print("\n2. Testing Self-Match on X10 (Buy into Sell)...")
    mock_x10.get_open_orders.return_value = [{'id': '1', 'side': 'SELL', 'price': 50000}]
    mock_lit.get_open_orders.return_value = []
    
    res = await pem._run_compliance_check('BTC-USD', 'BUY', 'SELL')
    assert res == False
    print("✅ Passed: Blocked BUY when SELL order exists on X10.")
    
    # Scenario 3: Self-Match on Lighter (Sell into Buy)
    print("\n3. Testing Self-Match on Lighter (Sell into Buy)...")
    mock_x10.get_open_orders.return_value = []
    mock_lit.get_open_orders.return_value = [{'id': '2', 'side': 'BUY', 'price': 51000}]
    
    res = await pem._run_compliance_check('BTC-USD', 'SELL', 'SELL')
    assert res == False
    print("✅ Passed: Blocked SELL when BUY order exists on Lighter.")
    
    # Scenario 4: Non-Conflicting Orders (Buy while Buy open)
    print("\n4. Testing Non-Conflicting Orders (Buy while Buy open)...")
    mock_x10.get_open_orders.return_value = [{'id': '3', 'side': 'BUY', 'price': 49000}]
    mock_lit.get_open_orders.return_value = []
    
    res = await pem._run_compliance_check('BTC-USD', 'BUY', 'SELL')
    assert res == True
    print("✅ Passed: Allowed BUY when BUY order exists (stacking).")

    print("\n✅ All Compliance Tests Passed!")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_compliance_check())
