"""
Unit tests for Lighter funding history normalization.

Tests that historical ingestion correctly normalizes funding rates
based on the configurable funding_rate_interval_hours setting.

CRITICAL: Lighter REST API returns DECIMAL format (e.g., 0.0001 = 0.01%).
NO division by 100 is needed - the API already returns decimal format.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, Mock
import aiohttp

import pytest

from funding_bot.domain.historical import FundingCandle
from funding_bot.services.historical.ingestion import HistoricalIngestionService


@pytest.mark.asyncio
async def test_lighter_ingestion_with_interval_1_no_division():
    """
    PROOF: Lighter REST API returns hourly rate; interval=1 should NOT divide.

    Based on official documentation (docs.lighter.xyz/perpetual-futures/funding):
    - The REST API /api/v1/fundings returns hourly funding rates
    - Rate is in DECIMAL format (e.g., 0.0001 = 0.01%)
    - With interval=1, no division is needed

    This test ensures that with interval_hours=1, the raw API rate is used directly.
    """
    # Mock settings with interval=1 (hourly rate, no division)
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")
    lighter_adapter = MagicMock()  # Not used for REST API calls

    # Mock store
    store = MagicMock()

    # Create service
    service = HistoricalIngestionService(store, settings, lighter_adapter)

    # Simulate API returning 0.0011 in DECIMAL format (0.11% hourly)
    api_data = {
        "fundings": [
            {
                "timestamp": 1736750400,  # 2025-01-13 00:00:00 UTC
                "rate": "0.0011"  # DECIMAL format (0.11% hourly)
            }
        ]
    }

    # Create proper mock response with async context manager
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=api_data)

    mock_session_get_result = AsyncMock()
    mock_session_get_result.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session_get_result.__aexit__ = AsyncMock(return_value=None)

    # Mock aiohttp.ClientSession.get
    with patch("aiohttp.ClientSession.get", return_value=mock_session_get_result):
        candles = await service._fetch_lighter_candles(
            "BTC",
            datetime(2025, 1, 13, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 1, 13, 1, 0, 0, tzinfo=UTC)
        )

    # Verify: With interval=1, raw rate is used directly (NO division)
    assert len(candles) == 1
    assert candles[0].funding_rate_hourly == Decimal("0.0011")  # API rate as-is
    assert candles[0].symbol == "BTC"
    assert candles[0].exchange == "LIGHTER"


@pytest.mark.asyncio
async def test_lighter_ingestion_with_interval_8_divides_by_8():
    """
    PROOF: Backward compatibility - interval=8 should divide raw rate by 8.

    REGRESSION TEST: This test ensures that if interval_hours=8 is set
    (e.g., for backward compatibility or API changes), the ingestion
    service divides the raw rate by 8.

    This serves as a safety net in case the Lighter API changes behavior.
    """
    # Mock settings with interval=8 (8h rate, divide by 8)
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("8")
    lighter_adapter = MagicMock()

    # Mock store
    store = MagicMock()

    # Create service
    service = HistoricalIngestionService(store, settings, lighter_adapter)

    # Simulate API returning 0.0088 (DECIMAL format - 8h cumulative rate)
    # 0.0088 / 8 = 0.0011 hourly
    api_data = {
        "fundings": [
            {
                "timestamp": 1736750400,  # 2025-01-13 00:00:00 UTC
                "rate": "0.0088"  # DECIMAL format (8h cumulative = 0.88%)
            }
        ]
    }

    # Create proper mock response with async context manager
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=api_data)

    mock_session_get_result = AsyncMock()
    mock_session_get_result.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session_get_result.__aexit__ = AsyncMock(return_value=None)

    # Mock aiohttp.ClientSession.get
    with patch("aiohttp.ClientSession.get", return_value=mock_session_get_result):
        candles = await service._fetch_lighter_candles(
            "BTC",
            datetime(2025, 1, 13, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 1, 13, 1, 0, 0, tzinfo=UTC)
        )

    # Verify: With interval=8, raw rate is divided by 8
    assert len(candles) == 1
    assert candles[0].funding_rate_hourly == Decimal("0.0011")  # 0.0088 / 8 = 0.0011
    assert candles[0].symbol == "BTC"
    assert candles[0].exchange == "LIGHTER"


@pytest.mark.asyncio
async def test_lighter_ingestion_apy_calculation_correct():
    """
    PROOF: APY calculation uses normalized hourly rate correctly.

    Verifies that APY is calculated as: hourly_rate * 24 * 365
    """
    # Mock settings with interval=1 (default, hourly rate)
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")
    lighter_adapter = MagicMock()
    store = MagicMock()

    # Create service
    service = HistoricalIngestionService(store, settings, lighter_adapter)

    # 0.001 in DECIMAL = 0.1% hourly rate → 87.6% APY
    api_data = {
        "fundings": [
            {
                "timestamp": 1736750400,
                "rate": "0.001"  # DECIMAL format (0.1% hourly)
            }
        ]
    }

    # Create proper mock response with async context manager
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=api_data)

    mock_session_get_result = AsyncMock()
    mock_session_get_result.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session_get_result.__aexit__ = AsyncMock(return_value=None)

    # Mock aiohttp.ClientSession.get
    with patch("aiohttp.ClientSession.get", return_value=mock_session_get_result):
        candles = await service._fetch_lighter_candles(
            "BTC",
            datetime(2025, 1, 13, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 1, 13, 1, 0, 0, tzinfo=UTC)
        )

    # Verify APY calculation
    assert len(candles) == 1
    expected_apy = Decimal("0.001") * 24 * 365  # 8.76
    assert candles[0].funding_rate_hourly == Decimal("0.001")
    assert candles[0].funding_apy == expected_apy


@pytest.mark.asyncio
async def test_lighter_ingestion_handles_missing_interval_setting():
    """
    PROOF: Service handles missing interval_hours gracefully (fallback to 1).

    Edge case: If settings.lighter doesn't have funding_rate_interval_hours,
    the service should default to 1 (hourly, no division).
    """
    # Mock settings WITHOUT funding_rate_interval_hours
    settings = MagicMock()
    # Deliberately don't set funding_rate_interval_hours
    lighter_adapter = MagicMock()
    store = MagicMock()

    # Create service
    service = HistoricalIngestionService(store, settings, lighter_adapter)

    # Mock API response in DECIMAL format
    api_data = {
        "fundings": [
            {
                "timestamp": 1736750400,
                "rate": "0.001"  # DECIMAL format (0.1% hourly)
            }
        ]
    }

    # Create proper mock response with async context manager
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=api_data)

    mock_session_get_result = AsyncMock()
    mock_session_get_result.__aenter__ = AsyncMock(return_value=mock_response)
    mock_session_get_result.__aexit__ = AsyncMock(return_value=None)

    # Mock aiohttp.ClientSession.get
    with patch("aiohttp.ClientSession.get", return_value=mock_session_get_result):
        candles = await service._fetch_lighter_candles(
            "BTC",
            datetime(2025, 1, 13, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 1, 13, 1, 0, 0, tzinfo=UTC)
        )

    # Verify: Falls back to interval=1 (no division)
    assert len(candles) == 1
    assert candles[0].funding_rate_hourly == Decimal("0.001")  # API rate as-is
