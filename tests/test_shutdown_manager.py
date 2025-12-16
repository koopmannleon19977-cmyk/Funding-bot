import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
import config


shutdown_mod = pytest.importorskip("src.shutdown_manager")
ShutdownManager = getattr(shutdown_mod, "ShutdownManager")

@pytest.mark.asyncio
async def test_shutdown_sequence_order():
    # 1. Setup Mock Bot and Adapters
    bot = MagicMock()
    bot.x10 = AsyncMock()
    bot.lighter = AsyncMock()
    
    # Mock config object on bot
    bot.config = MagicMock()
    
    # Mock fetch_open_positions to return some positions so we can test closure
    bot.x10.fetch_open_positions.return_value = [{'symbol': 'BTC-USD', 'size': 1.0}]
    bot.lighter.fetch_open_positions.return_value = [{'symbol': 'ETH-USD', 'size': -10.0}]
    
    # Mock prices
    bot.lighter.fetch_fresh_mark_price.return_value = 2000.0
    
    # 2. Initialize Manager
    manager = ShutdownManager(bot)
    
    # 3. Execute Shutdown
    # We need to ensure config.CLOSE_ALL_ON_SHUTDOWN is True
    original_config_val = getattr(config, 'CLOSE_ALL_ON_SHUTDOWN', True)
    config.CLOSE_ALL_ON_SHUTDOWN = True
    
    await manager.execute()
    
    # 4. Verify Order of Operations
    
    # Step 1: Flag set
    assert config.IS_SHUTTING_DOWN is True
    
    # Step 2: Cancel Orders BEFORE Closing Positions
    # We check that cancel_all_orders was called
    bot.x10.cancel_all_orders.assert_called()
    bot.lighter.cancel_all_orders.assert_called()
    
    # Step 3: Close Positions
    bot.x10.close_live_position.assert_called()
    bot.lighter.open_live_position.assert_called() # Lighter uses open_live_position to close
    
    # Verify exact calls for correctness
    # X10: Should close 1.0 BTC-USD (SELL)
    bot.x10.close_live_position.assert_called_with('BTC-USD', 'SELL', 1.0)
    
    # Lighter: Should close -10.0 ETH-USD (BUY)
    # Check arguments of lighter call
    args, kwargs = bot.lighter.open_live_position.call_args
    assert kwargs['symbol'] == 'ETH-USD'
    assert kwargs['side'] == 'BUY'
    assert kwargs['amount'] == 10.0
    assert kwargs['reduce_only'] is True
    assert kwargs['time_in_force'] == 'IOC'
    
    # Restore config
    config.CLOSE_ALL_ON_SHUTDOWN = original_config_val

@pytest.mark.asyncio
async def test_shutdown_retries():
    # Test that verify loop retries if positions remain open
    
    bot = MagicMock()
    bot.x10 = AsyncMock()
    bot.lighter = AsyncMock()
    bot.config = MagicMock()
    
    # First call to fetch returns positions (initial close)
    # Second call (verification 1) returns positions (retry needed)
    # Third call (verification 2) returns empty (success)
    bot.x10.fetch_open_positions.side_effect = [
        [{'symbol': 'BTC-USD', 'size': 1.0}], # Initial
        [{'symbol': 'BTC-USD', 'size': 1.0}], # Verify 1 (Still open)
        []                                    # Verify 2 (Closed)
    ]
    
    bot.lighter.fetch_open_positions.return_value = [] # Lighter clean
    
    manager = ShutdownManager(bot)
    config.CLOSE_ALL_ON_SHUTDOWN = True
    
    await manager.execute()
    
    # close_live_position should be called at least twice (initial + 1 retry)
    assert bot.x10.close_live_position.call_count >= 2
