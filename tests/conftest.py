
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

@pytest.fixture
def mock_x10():
    """Mock X10Adapter"""
    adapter = MagicMock()
    # Async methods must be AsyncMock
    adapter.open_live_position = AsyncMock(return_value=(True, "x10_order_id"))
    adapter.close_live_position = AsyncMock(return_value=(True, "x10_close_id"))
    adapter.fetch_open_positions = AsyncMock(return_value=[])
    adapter.fetch_mark_price = MagicMock(return_value=100.0)
    return adapter

@pytest.fixture
def mock_lighter():
    """Mock LighterAdapter"""
    adapter = MagicMock()
    adapter.open_live_position = AsyncMock(return_value=(True, "lighter_order_id"))
    adapter.close_live_position = AsyncMock(return_value=(True, "lighter_close_id"))
    adapter.fetch_open_positions = AsyncMock(return_value=[])
    adapter.fetch_mark_price = MagicMock(return_value=100.0)
    return adapter

@pytest.fixture
def mock_db():
    """Mock Database"""
    return MagicMock()

@pytest.fixture
def circuit_breaker_config():
    from src.circuit_breaker import CircuitBreakerConfig
    return CircuitBreakerConfig(
        max_consecutive_failures=2,
        max_drawdown_pct=0.1,
        drawdown_window_seconds=60,
        enable_kill_switch=False
    )
