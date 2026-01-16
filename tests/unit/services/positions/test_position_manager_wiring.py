"""
Test that PositionManager properly wires all close helper methods.

This test verifies that all imported functions from close.py are properly
bound to the PositionManager class for use as instance methods.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def test_position_manager_binds_rebalance_and_coordinated_methods():
    """
    Test that PositionManager properly binds rebalance and coordinated close methods.

    Verifies:
    - _rebalance_trade is bound
    - _close_both_legs_coordinated is bound
    - _submit_maker_order is bound
    - _execute_ioc_close is bound

    All methods should be callable as instance methods.
    """
    from funding_bot.services.positions.manager import PositionManager

    # Verify all critical methods are bound
    assert hasattr(PositionManager, "_rebalance_trade"), "PositionManager should have _rebalance_trade method"
    assert hasattr(PositionManager, "_close_both_legs_coordinated"), "PositionManager should have _close_both_legs_coordinated method"
    assert hasattr(PositionManager, "_submit_maker_order"), "PositionManager should have _submit_maker_order method"
    assert hasattr(PositionManager, "_execute_ioc_close"), "PositionManager should have _execute_ioc_close method"

    # Verify methods are callable
    assert callable(getattr(PositionManager, "_rebalance_trade")), "_rebalance_trade should be callable"
    assert callable(getattr(PositionManager, "_close_both_legs_coordinated")), "_close_both_legs_coordinated should be callable"
    assert callable(getattr(PositionManager, "_submit_maker_order")), "_submit_maker_order should be callable"
    assert callable(getattr(PositionManager, "_execute_ioc_close")), "_execute_ioc_close should be callable"


def test_position_manager_binds_all_close_helpers():
    """
    Test that ALL close helper methods are properly bound.

    Comprehensive check for all methods imported from close.py.
    """
    from funding_bot.services.positions.manager import PositionManager

    # Core close methods
    assert hasattr(PositionManager, "_close_impl"), "Should have _close_impl"
    assert hasattr(PositionManager, "_close_verify_and_finalize"), "Should have _close_verify_and_finalize"
    assert hasattr(PositionManager, "_post_close_readback"), "Should have _post_close_readback"

    # Leg-specific close methods
    assert hasattr(PositionManager, "_close_lighter_smart"), "Should have _close_lighter_smart"
    assert hasattr(PositionManager, "_close_x10_smart"), "Should have _close_x10_smart"
    assert hasattr(PositionManager, "_close_leg"), "Should have _close_leg"
    assert hasattr(PositionManager, "_verify_closed"), "Should have _verify_closed"

    # Rebalance and coordinated close methods (T4-T8)
    assert hasattr(PositionManager, "_rebalance_trade"), "Should have _rebalance_trade"
    assert hasattr(PositionManager, "_close_both_legs_coordinated"), "Should have _close_both_legs_coordinated"
    assert hasattr(PositionManager, "_submit_maker_order"), "Should have _submit_maker_order"
    assert hasattr(PositionManager, "_execute_ioc_close"), "Should have _execute_ioc_close"

    # Helper methods
    assert hasattr(PositionManager, "_get_lighter_close_config"), "Should have _get_lighter_close_config"
    assert hasattr(PositionManager, "_should_skip_lighter_close"), "Should have _should_skip_lighter_close"
    assert hasattr(PositionManager, "_ensure_lighter_websocket"), "Should have _ensure_lighter_websocket"
    assert hasattr(PositionManager, "_calculate_close_price"), "Should have _calculate_close_price"

    # Lighter close methods
    assert hasattr(PositionManager, "_execute_lighter_maker_close"), "Should have _execute_lighter_maker_close"
    assert hasattr(PositionManager, "_execute_lighter_close_attempt"), "Should have _execute_lighter_close_attempt"
    assert hasattr(PositionManager, "_execute_lighter_taker_fallback"), "Should have _execute_lighter_taker_fallback"

    # All should be callable
    for method_name in [
        "_close_impl",
        "_close_verify_and_finalize",
        "_post_close_readback",
        "_close_lighter_smart",
        "_close_x10_smart",
        "_close_leg",
        "_verify_closed",
        "_rebalance_trade",
        "_close_both_legs_coordinated",
        "_submit_maker_order",
        "_execute_ioc_close",
        "_get_lighter_close_config",
        "_should_skip_lighter_close",
        "_ensure_lighter_websocket",
        "_calculate_close_price",
        "_execute_lighter_maker_close",
        "_execute_lighter_close_attempt",
        "_execute_lighter_taker_fallback",
    ]:
        method = getattr(PositionManager, method_name)
        assert callable(method), f"{method_name} should be callable"
