
import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock


@pytest.fixture(autouse=True)
def _test_config_overrides(monkeypatch):
    """Keep unit tests isolated from live safety gates that require real adapters."""
    import config

    monkeypatch.setattr(config, "COMPLIANCE_CHECK_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "IS_SHUTTING_DOWN", False, raising=False)
    # Keep enabled so tests exercise the PHASE 0 path, but we provide a mock orderbook.
    monkeypatch.setattr(config, "OB_VALIDATION_ENABLED", True, raising=False)

    return True

@pytest.fixture
def mock_x10():
    """Mock X10Adapter"""
    adapter = MagicMock()
    # Async methods must be AsyncMock
    adapter.open_live_position = AsyncMock(return_value=(True, "x10_order_id"))
    adapter.close_live_position = AsyncMock(return_value=(True, "x10_close_id"))
    adapter.fetch_open_positions = AsyncMock(return_value=[])
    adapter.cancel_all_orders = AsyncMock(return_value=True)
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.fetch_mark_price = MagicMock(return_value=100.0)
    # Some execution paths look at a websocket-updated positions cache.
    adapter._positions_cache = [{'symbol': 'BTC-USD', 'size': 0.1}]
    return adapter

@pytest.fixture
def mock_lighter():
    """Mock LighterAdapter"""
    adapter = MagicMock()
    adapter.open_live_position = AsyncMock(return_value=(True, "lighter_order_id"))
    adapter.close_live_position = AsyncMock(return_value=(True, "lighter_close_id"))
    adapter.fetch_open_positions = AsyncMock(return_value=[])
    adapter.cancel_all_orders = AsyncMock(return_value=True)
    adapter.cancel_limit_order = AsyncMock(return_value=True)
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.fetch_mark_price = MagicMock(return_value=100.0)

    # Provide a minimal orderbook snapshot so PHASE 0 orderbook validation can pass.
    adapter._orderbook_cache = {
        'BTC-USD': {
            'bids': [(99.9, 1000)],
            'asks': [(100.1, 1000)],
        }
    }
    adapter._orderbook_cache_time = {'BTC-USD': time.time()}
    adapter.fetch_orderbook = AsyncMock(return_value=adapter._orderbook_cache['BTC-USD'])
    adapter.get_market_info = MagicMock(return_value={'lot_size': 0.01})
    return adapter

@pytest.fixture
def mock_db():
    """Mock Database"""
    return MagicMock()

@pytest.fixture
def circuit_breaker_config():
    cb_mod = pytest.importorskip("src.circuit_breaker")
    CircuitBreakerConfig = getattr(cb_mod, "CircuitBreakerConfig", None)
    if CircuitBreakerConfig is None:
        pytest.skip("CircuitBreakerConfig not available in this codebase")
    return CircuitBreakerConfig(
        max_consecutive_failures=2,
        max_drawdown_pct=0.1,
        drawdown_window_seconds=60,
        enable_kill_switch=False,
    )
