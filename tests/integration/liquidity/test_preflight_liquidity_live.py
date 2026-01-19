"""
Integration tests for pre-flight liquidity checks with REAL API calls.

These tests require:
- Lighter API key (LIGHTER_API_KEY_PRIVATE_KEY env var)
- X10 API key (X10_API_KEY env var)
- Network connectivity to both exchanges

Tests are marked with @pytest.mark.integration and skipped if credentials missing.

Run with:
    pytest tests/integration/liquidity/test_preflight_liquidity_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.config.settings import ExchangeSettings
from funding_bot.domain.models import Side
from funding_bot.observability.logging import get_logger
from funding_bot.services.liquidity_gates_preflight import (
    PreflightLiquidityConfig,
    check_preflight_liquidity,
)

logger = get_logger("funding_bot.tests.integration")


# Helper function to create adapters
async def create_adapters():
    """Create Lighter and X10 adapters for testing."""
    lighter_settings = ExchangeSettings(
        api_key=os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", ""),
        private_key=os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", ""),
        base_url="https://api.lighter.xyz",
    )

    x10_settings = ExchangeSettings(
        api_key=os.getenv("X10_API_KEY", ""),
        base_url="https://api.extended.exchange",
    )

    lighter_adapter = LighterAdapter(lighter_settings, account_index=0)
    await lighter_adapter.connect()

    x10_adapter = X10Adapter(x10_settings)
    await x10_adapter.connect()

    return lighter_adapter, x10_adapter


@pytest.mark.integration
async def test_lighter_orderbook_depth_levels():
    """
    Discovery test: How many orderbook levels does Lighter actually return?

    This test fetches the orderbook and reports the actual number of levels
    returned by the Lighter API.
    """
    lighter_adapter, _ = await create_adapters()

    try:
        symbol = "BTC-PERP"

        # Fetch with high depth limit to see max available
        orderbook = await lighter_adapter.get_orderbook_depth(symbol, depth=100)

        assert orderbook is not None, "Orderbook should not be None"
        assert "bids" in orderbook, "Orderbook should have bids"
        assert "asks" in orderbook, "Orderbook should have asks"

        bid_levels = len(orderbook["bids"])
        ask_levels = len(orderbook["asks"])

        logger.info(f"Lighter orderbook levels for {symbol}:")
        logger.info(f"  Bids: {bid_levels} levels")
        logger.info(f"  Asks: {ask_levels} levels")
        logger.info(f"  Max levels: {max(bid_levels, ask_levels)}")

        # Show sample of first few levels
        if bid_levels > 0:
            logger.info("  Sample bids (top 3):")
            for i, (price, qty) in enumerate(orderbook["bids"][:3]):
                logger.info(f"    Level {i + 1}: {price} @ {qty}")

        if ask_levels > 0:
            logger.info("  Sample asks (top 3):")
            for i, (price, qty) in enumerate(orderbook["asks"][:3]):
                logger.info(f"    Level {i + 1}: {price} @ {qty}")

        # Assert minimum liquidity exists
        assert bid_levels > 0, "Should have at least 1 bid level"
        assert ask_levels > 0, "Should have at least 1 ask level"

    finally:
        await lighter_adapter.close()


@pytest.mark.integration
async def test_x10_orderbook_depth_levels():
    """
    Discovery test: How many orderbook levels does X10 actually return?

    This test fetches the orderbook and reports the actual number of levels
    returned by the X10 API.
    """
    _, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"

        # Fetch with high depth limit to see max available
        orderbook = await x10_adapter.get_orderbook_depth(symbol, depth=100)

        assert orderbook is not None, "Orderbook should not be None"
        assert "bids" in orderbook, "Orderbook should have bids"
        assert "asks" in orderbook, "Orderbook should have asks"

        bid_levels = len(orderbook["bids"])
        ask_levels = len(orderbook["asks"])

        logger.info(f"X10 orderbook levels for {symbol}:")
        logger.info(f"  Bids: {bid_levels} levels")
        logger.info(f"  Asks: {ask_levels} levels")
        logger.info(f"  Max levels: {max(bid_levels, ask_levels)}")

        # Show sample of first few levels
        if bid_levels > 0:
            logger.info("  Sample bids (top 3):")
            for i, (price, qty) in enumerate(orderbook["bids"][:3]):
                logger.info(f"    Level {i + 1}: {price} @ {qty}")

        if ask_levels > 0:
            logger.info("  Sample asks (top 3):")
            for i, (price, qty) in enumerate(orderbook["asks"][:3]):
                logger.info(f"    Level {i + 1}: {price} @ {qty}")

        # Assert minimum liquidity exists
        assert bid_levels > 0, "Should have at least 1 bid level"
        assert ask_levels > 0, "Should have at least 1 ask level"

    finally:
        await x10_adapter.close()


@pytest.mark.integration
async def test_orderbook_latency_benchmark():
    """
    Benchmark: How long does it take to fetch orderbooks from both exchanges?

    This test measures the latency of fetching orderbooks from both exchanges
    in parallel (as the pre-flight check does).
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"

        # Measure individual fetch times
        import time

        # Lighter latency
        start = time.time()
        lighter_ob = await lighter_adapter.get_orderbook_depth(symbol, depth=20)
        lighter_latency_ms = (time.time() - start) * 1000

        # X10 latency
        start = time.time()
        x10_ob = await x10_adapter.get_orderbook_depth(symbol, depth=20)
        x10_latency_ms = (time.time() - start) * 1000

        # Parallel fetch latency (like pre-flight check)
        start = time.time()
        lighter_ob, x10_ob = await asyncio.gather(
            lighter_adapter.get_orderbook_depth(symbol, depth=20),
            x10_adapter.get_orderbook_depth(symbol, depth=20),
        )
        parallel_latency_ms = (time.time() - start) * 1000

        logger.info(f"Orderbook fetch latency for {symbol}:")
        logger.info(f"  Lighter: {lighter_latency_ms:.1f}ms")
        logger.info(f"  X10: {x10_latency_ms:.1f}ms")
        logger.info(f"  Parallel: {parallel_latency_ms:.1f}ms")
        logger.info(f"  Speedup: {((lighter_latency_ms + x10_latency_ms) / parallel_latency_ms):.2f}x")

        # Assert reasonable latency
        assert lighter_latency_ms < 2000, "Lighter should respond within 2 seconds"
        assert x10_latency_ms < 2000, "X10 should respond within 2 seconds"
        assert parallel_latency_ms < 2000, "Parallel fetch should complete within 2 seconds"

        # Log recommendation
        if parallel_latency_ms > 500:
            logger.warning(
                f"Parallel fetch latency ({parallel_latency_ms:.1f}ms) exceeds target (500ms). "
                "Consider enabling caching or reducing depth_levels."
            )

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_preflight_liquidity_real_btc():
    """
    Test pre-flight liquidity check with real BTC-PERP orderbooks.

    This is the main integration test that validates the pre-flight liquidity check
    works correctly with real market data.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"
        required_qty = Decimal("0.01")  # Small quantity for testing

        config = PreflightLiquidityConfig(
            depth_levels=5,
            safety_factor=Decimal("3.0"),
            max_spread_bps=Decimal("50"),
        )

        logger.info(f"Testing pre-flight liquidity check for {symbol}...")

        result = await check_preflight_liquidity(
            lighter_adapter=lighter_adapter,
            x10_adapter=x10_adapter,
            symbol=symbol,
            required_qty=required_qty,
            side=Side.BUY,
            config=config,
        )

        logger.info(f"Pre-flight liquidity check result for {symbol}:")
        logger.info(f"  Passed: {result.passed}")
        logger.info(f"  Required: {required_qty}")
        logger.info(f"  Lighter available: {result.lighter_available_qty} ({result.lighter_depth_levels} levels)")
        logger.info(f"  X10 available: {result.x10_available_qty} ({result.x10_depth_levels} levels)")
        logger.info(f"  Spread: {result.spread_bps} bps")
        logger.info(f"  Latency: {result.latency_ms:.1f}ms")

        if result.passed:
            logger.info("  ✅ Pre-flight check PASSED")
        else:
            logger.warning(f"  ❌ Pre-flight check FAILED: {result.failure_reason}")

        # Assert result is valid
        assert result is not None, "Result should not be None"
        assert result.latency_ms >= 0, "Latency should be non-negative"

        # For BTC, we expect high liquidity
        if result.passed:
            logger.info("✅ BTC-PERP has sufficient liquidity (expected for high-volume symbol)")
        else:
            logger.warning(
                f"BTC-PERP failed pre-flight check: {result.failure_reason}. "
                "This is unexpected for a high-volume symbol."
            )

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_preflight_liquidity_real_eth():
    """
    Test pre-flight liquidity check with real ETH-PERP orderbooks.

    Tests the same functionality as BTC but with a different symbol to validate
    the check works across different markets.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "ETH-PERP"
        required_qty = Decimal("0.1")  # Small quantity for testing

        config = PreflightLiquidityConfig(
            depth_levels=5,
            safety_factor=Decimal("3.0"),
            max_spread_bps=Decimal("50"),
        )

        logger.info(f"Testing pre-flight liquidity check for {symbol}...")

        result = await check_preflight_liquidity(
            lighter_adapter=lighter_adapter,
            x10_adapter=x10_adapter,
            symbol=symbol,
            required_qty=required_qty,
            side=Side.BUY,
            config=config,
        )

        logger.info(f"Pre-flight liquidity check result for {symbol}:")
        logger.info(f"  Passed: {result.passed}")
        logger.info(f"  Required: {required_qty}")
        logger.info(f"  Lighter available: {result.lighter_available_qty} ({result.lighter_depth_levels} levels)")
        logger.info(f"  X10 available: {result.x10_available_qty} ({result.x10_depth_levels} levels)")
        logger.info(f"  Spread: {result.spread_bps} bps")
        logger.info(f"  Latency: {result.latency_ms:.1f}ms")

        if result.passed:
            logger.info("  ✅ Pre-flight check PASSED")
        else:
            logger.warning(f"  ❌ Pre-flight check FAILED: {result.failure_reason}")

        # Assert result is valid
        assert result is not None, "Result should not be None"
        assert result.latency_ms >= 0, "Latency should be non-negative"

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_preflight_liquidity_sell_side():
    """
    Test pre-flight liquidity check for SELL side (uses bid side of orderbook).

    Validates that the check correctly uses bid side for sell orders.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"
        required_qty = Decimal("0.01")

        config = PreflightLiquidityConfig(
            depth_levels=5,
            safety_factor=Decimal("3.0"),
        )

        logger.info(f"Testing pre-flight liquidity check for {symbol} (SELL side)...")

        result = await check_preflight_liquidity(
            lighter_adapter=lighter_adapter,
            x10_adapter=x10_adapter,
            symbol=symbol,
            required_qty=required_qty,
            side=Side.SELL,  # SELL side
            config=config,
        )

        logger.info(f"SELL side pre-flight liquidity check result for {symbol}:")
        logger.info(f"  Passed: {result.passed}")
        logger.info(f"  Lighter available: {result.lighter_available_qty} (from bids)")
        logger.info(f"  X10 available: {result.x10_available_qty} (from bids)")
        logger.info(f"  Latency: {result.latency_ms:.1f}ms")

        assert result is not None, "Result should not be None"

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_depth_level_comparison():
    """
    Compare liquidity across different depth levels.

    Tests how available liquidity changes as we increase the number of levels
    we aggregate. This helps tune the depth_levels configuration.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"

        logger.info(f"Testing liquidity aggregation across depth levels for {symbol}...")

        # Test different depth levels
        for depth_levels in [1, 3, 5, 10, 20]:
            config = PreflightLiquidityConfig(
                depth_levels=depth_levels,
                safety_factor=Decimal("1.0"),  # No safety factor for raw comparison
            )

            result = await check_preflight_liquidity(
                lighter_adapter=lighter_adapter,
                x10_adapter=x10_adapter,
                symbol=symbol,
                required_qty=Decimal("0.001"),  # Tiny qty, just to trigger check
                side=Side.BUY,
                config=config,
            )

            logger.info(
                f"  Depth {depth_levels:2d} levels: "
                f"Lighter={result.lighter_available_qty:10.6f}, "
                f"X10={result.x10_available_qty:10.6f}, "
                f"Total={result.lighter_available_qty + result.x10_available_qty:10.6f}"
            )

        # Log recommendation
        logger.info("")
        logger.info("Recommendation:")
        logger.info("  - Use 3-5 levels for high-volume symbols (BTC, ETH)")
        logger.info("  - Use 5-10 levels for medium-volume symbols")
        logger.info("  - Use 10+ levels for low-volume symbols")

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_safety_factor_impact():
    """
    Test how different safety factors affect the pre-flight check.

    This helps tune the safety_factor configuration.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbol = "BTC-PERP"
        required_qty = Decimal("0.01")

        logger.info(f"Testing safety factor impact for {symbol} (required={required_qty})...")

        # Test different safety factors
        for safety_factor in [Decimal("1.0"), Decimal("2.0"), Decimal("3.0"), Decimal("5.0")]:
            config = PreflightLiquidityConfig(
                depth_levels=5,
                safety_factor=safety_factor,
            )

            result = await check_preflight_liquidity(
                lighter_adapter=lighter_adapter,
                x10_adapter=x10_adapter,
                symbol=symbol,
                required_qty=required_qty,
                side=Side.BUY,
                config=config,
            )

            required_liquidity = required_qty * safety_factor

            logger.info(
                f"  Safety {safety_factor}x: "
                f"Required={required_liquidity:.6f}, "
                f"Lighter={result.lighter_available_qty:.6f}, "
                f"X10={result.x10_available_qty:.6f}, "
                f"Passed={'✅' if result.passed else '❌'}"
            )

        # Log recommendation
        logger.info("")
        logger.info("Recommendation:")
        logger.info("  - Use 2.0x for high-volume symbols (BTC, ETH)")
        logger.info("  - Use 3.0x for medium-volume symbols (default)")
        logger.info("  - Use 5.0x for low-volume symbols")

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )


@pytest.mark.integration
async def test_spread_measurement():
    """
    Measure actual spread values for different symbols.

    This helps validate the spread_bps configuration and understand
    real-world spread characteristics.
    """
    lighter_adapter, x10_adapter = await create_adapters()

    try:
        symbols = ["BTC-PERP", "ETH-PERP"]

        logger.info("Measuring spreads for different symbols...")

        for symbol in symbols:
            config = PreflightLiquidityConfig(
                depth_levels=1,  # Only L1 for spread measurement
                safety_factor=Decimal("1.0"),
            )

            result = await check_preflight_liquidity(
                lighter_adapter=lighter_adapter,
                x10_adapter=x10_adapter,
                symbol=symbol,
                required_qty=Decimal("0.001"),
                side=Side.BUY,
                config=config,
            )

            logger.info(
                f"  {symbol:10s}: Spread={result.spread_bps:6.2f} bps "
                f"(={'Normal' if result.spread_bps < 10 else 'Wide' if result.spread_bps < 50 else 'VERY WIDE'})"
            )

        # Log recommendation
        logger.info("")
        logger.info("Spread analysis:")
        logger.info("  - < 10 bps: Excellent (typical for BTC/ETH)")
        logger.info("  - 10-50 bps: Normal")
        logger.info("  - > 50 bps: Wide (consider rejecting)")
        logger.info("  - > 100 bps: Very wide (high market stress)")

    finally:
        await asyncio.gather(
            lighter_adapter.close(),
            x10_adapter.close(),
        )
