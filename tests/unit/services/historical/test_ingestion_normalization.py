"""
REGRESSION TESTS: Historical Ingestion Funding Rate Normalization.

These tests ensure that historical funding rate data from Lighter is normalized
consistently with live adapter rates.

CRITICAL: Lighter API returns rates in DECIMAL format (e.g., 0.0001 = 0.01%).
NO division by 100 is needed - the API already returns decimal format.

Funding rates used: Velocity Exit, Z-Score Exit, and APY-based Scoring.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from funding_bot.domain.historical import FundingCandle
from funding_bot.services.historical.ingestion import HistoricalIngestionService


@pytest.mark.asyncio
async def test_historical_ingestion_with_interval_1_no_division():
    """
    REGRESSION: Historical ingestion must NOT divide when interval_hours=1.

    GIVEN: Lighter API returns hourly rate 0.0001 (DECIMAL format = 0.01%)
    WHEN: config sets funding_rate_interval_hours=1 (default)
    THEN: stored hourly rate must be 0.0001 (NO division)

    IMPORTANT: API returns DECIMAL format, not percent format.
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")

    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)

    ingestion = HistoricalIngestionService(store, settings)

    # Mock REST API response: hourly funding rate in DECIMAL format
    # 0.0001 = 0.01% hourly (API already returns decimal, NOT percent)
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [
                {
                    "timestamp": 1704067200,  # 2024-01-01 00:00:00 UTC
                    "rate": "0.0001"  # DECIMAL format (0.01% hourly)
                }
            ]
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="ETH",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    # ASSERT: Rate stays as-is (API returns decimal, interval=1 means no division)
    assert len(candles) == 1
    assert candles[0].funding_rate_hourly == Decimal("0.0001")
    # APY = 0.0001 * 24 * 365 = 0.876
    assert candles[0].funding_apy == Decimal("0.0001") * 24 * 365


@pytest.mark.asyncio
async def test_historical_ingestion_with_interval_8_divides_by_8():
    """
    BACKWARDS COMPATIBILITY: Historical ingestion must divide by 8 when interval_hours=8.

    GIVEN: Lighter API returns 8h rate 0.0008 (DECIMAL format)
    WHEN: config sets funding_rate_interval_hours=8
    THEN: stored hourly rate must be 0.0001 (0.0008 / 8)

    This is a SAFETY NET in case the Lighter API changes to 8h rates in the future.
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("8")

    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)

    ingestion = HistoricalIngestionService(store, settings)

    # Mock REST API response: 8h funding rate in DECIMAL format
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [
                {
                    "timestamp": 1704067200,
                    "rate": "0.0008"  # DECIMAL format (8h rate)
                }
            ]
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="BTC",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    # ASSERT: Division by 8 (0.0008 / 8 = 0.0001)
    assert len(candles) == 1
    assert candles[0].funding_rate_hourly == Decimal("0.0001")


@pytest.mark.asyncio
async def test_historical_ingestion_fallback_to_interval_1_when_missing():
    """
    REGRESSION: Historical ingestion must fallback to interval=1 when settings missing.

    GIVEN: Settings object missing or None
    WHEN: fetching Lighter candles
    THEN: must use interval_hours=1 (no division)

    This ensures graceful degradation when settings are incomplete.
    """
    settings = MagicMock()
    # Simulate missing settings attribute
    settings.lighter = None

    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)

    ingestion = HistoricalIngestionService(store, settings)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [
                {
                    "timestamp": 1704067200,
                    "rate": "0.0001"  # DECIMAL format
                }
            ]
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="SOL",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    # ASSERT: Fallback to interval=1 (no division), rate stays as-is
    assert candles[0].funding_rate_hourly == Decimal("0.0001")


@pytest.mark.asyncio
async def test_historical_ingestion_apy_calculation():
    """
    REGRESSION: APY calculation must be consistent with hourly rate.

    GIVEN: Hourly rate 0.0001 (0.01% in decimal)
    WHEN: calculating APY
    THEN: APY = 0.0001 * 24 * 365 = 0.876 (87.6%)

    This ensures APY is calculated correctly for scoring and exit decisions.
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")

    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)

    ingestion = HistoricalIngestionService(store, settings)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [
                {
                    "timestamp": 1704067200,
                    "rate": "0.0001"  # DECIMAL format (0.01% hourly)
                }
            ]
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="ETH",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    # ASSERT: APY = hourly_rate * 24 * 365
    # 0.0001 * 24 * 365 = 0.876
    expected_apy = Decimal("0.0001") * Decimal("24") * Decimal("365")
    assert candles[0].funding_apy == expected_apy


@pytest.mark.asyncio
async def test_historical_ingestion_multiple_candles_consistency():
    """
    REGRESSION: Multiple candles must use consistent normalization.

    GIVEN: API returns 24 hourly candles
    WHEN: interval_hours=1
    THEN: ALL candles must have correct hourly rates

    This catches edge cases where loop logic might apply division incorrectly.
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")

    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=24)

    ingestion = HistoricalIngestionService(store, settings)

    # Mock 24 hours of hourly data in DECIMAL format
    fundings = []
    for i in range(24):
        fundings.append({
            "timestamp": 1704067200 + (i * 3600),  # Hourly timestamps
            "rate": "0.00005"  # Constant 0.005% hourly rate in DECIMAL format
        })

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"fundings": fundings})
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="DOGE",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    # ASSERT: All 24 candles have correct hourly rate (no division with interval=1)
    assert len(candles) == 24
    for candle in candles:
        assert candle.funding_rate_hourly == Decimal("0.00005"), \
            f"Candle at {candle.timestamp} has incorrect rate: {candle.funding_rate_hourly}"
