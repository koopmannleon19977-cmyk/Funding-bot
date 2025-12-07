
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from src.parallel_execution import ParallelExecutionManager, ExecutionState

@pytest.mark.asyncio
async def test_successful_execution(mock_x10, mock_lighter, mock_db):
    """Verify standard successful execution of both legs"""
    manager = ParallelExecutionManager(mock_x10, mock_lighter, mock_db)
    
    success, x10_id, lighter_id = await manager.execute_trade_parallel(
        symbol="BTC-USD",
        side_x10="BUY", side_lighter="SELL",
        size_x10=0.1, size_lighter=0.1
    )
    
    assert success is True
    assert x10_id == "x10_order_id"
    assert lighter_id == "lighter_order_id"
    assert manager._stats["successful"] == 1
    
    # Verify adapters called
    mock_lighter.open_live_position.assert_called_once()
    mock_x10.open_live_position.assert_called_once()

@pytest.mark.asyncio
async def test_x10_fail_rollback_lighter(mock_x10, mock_lighter, mock_db):
    """Simulate X10 failing, Lighter succeeding -> Expect Lighter Rollback"""
    # Setup mocks
    mock_x10.open_live_position = AsyncMock(return_value=(False, "error"))
    mock_lighter.open_live_position = AsyncMock(return_value=(True, "lighter_order_id"))
    
    # Mock Rollback Verification (Lighter position check)
    mock_lighter.fetch_open_positions = AsyncMock(return_value=[
        {'symbol': 'BTC-USD', 'size': '-0.1', 'side': 'SELL'}
    ])
    
    manager = ParallelExecutionManager(mock_x10, mock_lighter, mock_db)
    # Start background rollback loop
    await manager.start()
    
    success, x10_id, lighter_id = await manager.execute_trade_parallel(
        symbol="BTC-USD",
        side_x10="BUY", side_lighter="SELL",
        size_x10=0.1, size_lighter=0.1
    )
    
    assert success is False
    assert lighter_id == "lighter_order_id"
    assert manager._stats["failed"] == 1
    assert manager._stats["rollbacks_triggered"] == 1
    
    # Give background task time to process
    await asyncio.sleep(3.5)  # Wait for 3s delay in manager + processing
    
    # Verify Lighter closed
    mock_lighter.close_live_position.assert_called()
    assert manager._stats["rollbacks_successful"] == 1
    
    await manager.stop()

@pytest.mark.asyncio
async def test_lighter_fail_rollback_x10(mock_x10, mock_lighter, mock_db):
    """Simulate Lighter failing, X10 succeeding -> Expect X10 Rollback"""
    mock_lighter.open_live_position = AsyncMock(return_value=(False, "error"))
    mock_x10.open_live_position = AsyncMock(return_value=(True, "x10_order_id"))
    
    mock_x10.fetch_open_positions = AsyncMock(return_value=[
        {'symbol': 'BTC-USD', 'size': '0.1', 'side': 'BUY'}
    ])
    
    manager = ParallelExecutionManager(mock_x10, mock_lighter, mock_db)
    await manager.start()
    
    success, x10_id, lighter_id = await manager.execute_trade_parallel(
        symbol="BTC-USD",
        side_x10="BUY", side_lighter="SELL",
        size_x10=0.1, size_lighter=0.1
    )
    
    assert success is False
    assert x10_id == "x10_order_id"
    
    await asyncio.sleep(3.5)
    
    mock_x10.close_live_position.assert_called()
    assert manager._stats["rollbacks_successful"] == 1
    
    await manager.stop()
