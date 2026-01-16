"""
Unit tests for pre-flight liquidity checks.

Tests run offline with mocks - no exchange SDK required.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from funding_bot.domain.models import Side
from funding_bot.services.liquidity_gates_preflight import (
    PreflightLiquidityConfig,
    PreflightLiquidityResult,
    _aggregate_liquidity,
    _calculate_spread_bps,
    _extract_best_prices,
    check_preflight_liquidity,
)


@pytest.mark.unit
async def test_aggregate_liquidity_empty_levels():
    """Test aggregation with empty levels."""
    qty, levels = _aggregate_liquidity([], 5, Side.BUY)
    assert qty == Decimal("0")
    assert levels == 0


@pytest.mark.unit
async def test_aggregate_liquidity_single_level():
    """Test aggregation with single level."""
    levels = [(Decimal("50000"), Decimal("1.5"))]
    qty, levels_used = _aggregate_liquidity(levels, 5, Side.BUY)
    assert qty == Decimal("1.5")
    assert levels_used == 1


@pytest.mark.unit
async def test_aggregate_liquidity_multiple_levels():
    """Test aggregation with multiple levels."""
    levels = [
        (Decimal("50000"), Decimal("1.0")),
        (Decimal("49999"), Decimal("2.0")),
        (Decimal("49998"), Decimal("1.5")),
        (Decimal("49997"), Decimal("0.5")),
        (Decimal("49996"), Decimal("1.0")),
    ]
    qty, levels_used = _aggregate_liquidity(levels, 5, Side.BUY)
    assert qty == Decimal("6.0")  # 1.0 + 2.0 + 1.5 + 0.5 + 1.0
    assert levels_used == 5


@pytest.mark.unit
async def test_aggregate_liquidity_limited_by_depth():
    """Test aggregation when depth_levels < available levels."""
    levels = [
        (Decimal("50000"), Decimal("1.0")),
        (Decimal("49999"), Decimal("2.0")),
        (Decimal("49998"), Decimal("1.5")),
    ]
    qty, levels_used = _aggregate_liquidity(levels, 2, Side.BUY)
    assert qty == Decimal("3.0")  # Only first 2 levels: 1.0 + 2.0
    assert levels_used == 2


@pytest.mark.unit
async def test_calculate_spread_bps_normal():
    """Test spread calculation with normal values."""
    bid = Decimal("50000")
    ask = Decimal("50010")
    spread_bps = _calculate_spread_bps(bid, ask)
    # Mid = 50005, spread = 10, spread_bps = (10 / 50005) * 10000 ≈ 2.0
    expected_min = Decimal("1.9")
    expected_max = Decimal("2.1")
    assert expected_min <= spread_bps <= expected_max


@pytest.mark.unit
async def test_calculate_spread_bps_zero_prices():
    """Test spread calculation with zero prices."""
    spread_bps = _calculate_spread_bps(Decimal("0"), Decimal("0"))
    assert spread_bps == Decimal("0")


@pytest.mark.unit
async def test_calculate_spread_bps_wide_spread():
    """Test spread calculation with wide spread (1%)."""
    bid = Decimal("50000")
    ask = Decimal("50500")
    spread_bps = _calculate_spread_bps(bid, ask)
    # Mid = 50250, spread = 500, spread_bps = (500 / 50250) * 10000 ≈ 99.5
    expected_min = Decimal("99.0")
    expected_max = Decimal("100.0")
    assert expected_min <= spread_bps <= expected_max


@pytest.mark.unit
async def test_extract_best_prices_normal():
    """Test extracting best prices from normal orderbook."""
    orderbook = {
        "bids": [(Decimal("50000"), Decimal("1.0")), (Decimal("49999"), Decimal("2.0"))],
        "asks": [(Decimal("50010"), Decimal("1.5")), (Decimal("50011"), Decimal("1.0"))],
    }
    bid, ask = _extract_best_prices(orderbook)
    assert bid == Decimal("50000")
    assert ask == Decimal("50010")


@pytest.mark.unit
async def test_extract_best_prices_empty():
    """Test extracting best prices from empty orderbook."""
    orderbook = {"bids": [], "asks": []}
    bid, ask = _extract_best_prices(orderbook)
    assert bid == Decimal("0")
    assert ask == Decimal("0")


@pytest.mark.unit
async def test_preflight_liquidity_both_exchanges_sufficient():
    """Test pre-flight check when both exchanges have sufficient liquidity."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(
        depth_levels=5,
        safety_factor=Decimal("3.0"),
        max_spread_bps=Decimal("50"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is True
    assert result.lighter_available_qty >= Decimal("3.0")  # 1.0 * 3.0
    assert result.x10_available_qty >= Decimal("3.0")
    assert result.failure_reason == ""
    assert result.latency_ms >= 0


@pytest.mark.unit
async def test_preflight_liquidity_lighter_insufficient():
    """Test pre-flight check when Lighter has insufficient liquidity."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("0.5")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("0.5")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(
        depth_levels=5,
        safety_factor=Decimal("3.0"),
        max_spread_bps=Decimal("50"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is False
    assert "Lighter insufficient liquidity" in result.failure_reason
    assert result.lighter_available_qty < Decimal("3.0")


@pytest.mark.unit
async def test_preflight_liquidity_x10_insufficient():
    """Test pre-flight check when X10 has insufficient liquidity."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("0.5")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("0.5")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(
        depth_levels=5,
        safety_factor=Decimal("3.0"),
        max_spread_bps=Decimal("50"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is False
    assert "X10 insufficient liquidity" in result.failure_reason
    assert result.x10_available_qty < Decimal("3.0")


@pytest.mark.unit
async def test_preflight_liquidity_disabled():
    """Test pre-flight check when disabled."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(enabled=False)

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is True
    assert "disabled" in result.failure_reason


@pytest.mark.unit
async def test_preflight_liquidity_invalid_quantity():
    """Test pre-flight check with invalid quantity."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    required_qty = Decimal("0")
    config = PreflightLiquidityConfig()

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is False
    assert "Invalid required_qty" in result.failure_reason


@pytest.mark.unit
async def test_preflight_liquidity_min_threshold():
    """Test pre-flight check with minimum liquidity threshold."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("0.001")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("0.001")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("0.01")
    config = PreflightLiquidityConfig(
        min_liquidity_threshold=Decimal("0.01"),
        safety_factor=Decimal("1.0"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is False
    assert "below threshold" in result.failure_reason


@pytest.mark.unit
async def test_preflight_liquidity_orderbook_fetch_failure():
    """Test pre-flight check when orderbook fetch fails."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(side_effect=Exception("Network error"))
    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig()

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is False
    assert "Failed to fetch one or both orderbooks" in result.failure_reason


@pytest.mark.unit
async def test_preflight_liquidity_sell_side():
    """Test pre-flight check for SELL side (uses bid side)."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(
        depth_levels=5,
        safety_factor=Decimal("3.0"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.SELL, config
    )

    # Assert
    assert result.passed is True
    assert result.lighter_available_qty >= Decimal("3.0")
    assert result.x10_available_qty >= Decimal("3.0")


@pytest.mark.unit
async def test_preflight_liquidity_metrics():
    """Test that pre-flight check returns detailed metrics."""
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()

    lighter_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    x10_adapter.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0")) for _ in range(10)],
            "asks": [(Decimal("50010"), Decimal("5.0")) for _ in range(10)],
        }
    )

    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(
        depth_levels=5,
        safety_factor=Decimal("3.0"),
    )

    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, Side.BUY, config
    )

    # Assert
    assert result.passed is True
    assert result.metrics is not None
    assert "required_qty" in result.metrics
    assert "required_liquidity" in result.metrics
    assert "safety_factor" in result.metrics
    assert "depth_levels" in result.metrics
    assert result.metrics["required_liquidity"] in ["3.0", "3.00"]  # Accept either format
    assert result.lighter_depth_levels == 5
    assert result.x10_depth_levels == 5
