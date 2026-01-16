"""
INTEGRATION TEST: Live Adapter vs Historical Ingestion Consistency.

This CRITICAL test ensures that live funding rates (from LighterAdapter) and
historical rates (from HistoricalIngestion) produce the SAME hourly rates
when given equivalent input.

IMPORTANT: The SDK and REST API return different rate formats:
- SDK FundingApi.funding_rates(): Returns 8-HOUR rates (divided by 8 in adapter)
- REST /api/v1/fundings?resolution=1h: Returns HOURLY rates (no division with interval=1)

Both APIs return DECIMAL format (e.g., 0.0001 = 0.01%).

REGRESSION PROTECTION: This test catches mismatches that would cause:
- Velocity Exit: Would react incorrectly with wrong historical rates
- Z-Score Exit: Would trigger false alarms with live/history mismatch
- Scoring: APY calculations would be wrong
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.services.historical.ingestion import HistoricalIngestionService


@pytest.mark.asyncio
async def test_live_adapter_and_historical_ingestion_consistency():
    """
    CRITICAL: Live adapter and historical ingestion MUST produce same hourly rate.

    IMPORTANT: SDK returns 8h rate, REST returns 1h rate (both in decimal format).
    - SDK rate: 0.0008 (8h rate) → adapter divides by 8 → 0.0001 hourly
    - REST rate: 0.0001 (1h rate) → ingestion with interval=1 → 0.0001 hourly

    Both must produce the same 0.0001 hourly rate.
    """
    # Setup: Use interval_hours=1 for historical (REST returns hourly already)
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 0
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    # === LIVE ADAPTER: Fetch funding rate from SDK ===
    adapter = LighterAdapter(settings)
    adapter._funding_api = MagicMock()

    # SDK returns 8-HOUR rate in DECIMAL format
    # 0.0008 = 0.08% per 8 hours → 0.0001 = 0.01% per hour
    rate_data = SimpleNamespace(
        exchange="lighter",
        market_id=1,
        symbol="ETH",
        rate="0.0008",  # 8h rate in DECIMAL (adapter divides by 8)
        next_funding_time=None,
    )
    result = SimpleNamespace(funding_rates=[rate_data])
    adapter._sdk_call_with_retry = AsyncMock(return_value=result)

    await adapter.refresh_all_market_data()
    live_rate = adapter._funding_cache["ETH"].rate

    # === HISTORICAL INGESTION: Fetch same rate from REST API ===
    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)
    ingestion = HistoricalIngestionService(store, settings)

    # REST API returns HOURLY rate in DECIMAL format (same effective rate)
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [
                {
                    "timestamp": 1704067200,
                    "rate": "0.0001"  # 1h rate in DECIMAL (already hourly)
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

    hist_rate = candles[0].funding_rate_hourly

    # === ASSERT: Both produce same hourly rate ===
    assert live_rate == hist_rate == Decimal("0.0001"), (
        f"NORMALIZATION MISMATCH DETECTED!\n"
        f"  Live Adapter: {live_rate} (from SDK 8h rate 0.0008 / 8)\n"
        f"  Historical:   {hist_rate} (from REST 1h rate 0.0001)\n"
        f"  Expected:     0.0001 (both)"
    )


@pytest.mark.asyncio
async def test_live_and_history_consistency_with_interval_8():
    """
    BACKWARDS COMPATIBILITY: Both components must handle interval=8 consistently.

    When interval=8 is set, historical ingestion divides REST rate by 8.
    - SDK rate: 0.0008 (8h) → adapter always divides by 8 → 0.0001
    - REST rate: 0.0008 (if 8h) → ingestion with interval=8 → 0.0008 / 8 = 0.0001

    Note: In practice, REST API returns hourly rates, so interval=8 would be wrong.
    This test is for backwards compatibility in case API behavior changes.
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("8")  # Divide REST rate by 8
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 0
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    # Live adapter (always divides SDK rate by 8)
    adapter = LighterAdapter(settings)
    adapter._funding_api = MagicMock()

    rate_data = SimpleNamespace(
        exchange="lighter",
        market_id=1,
        symbol="BTC",
        rate="0.0008",  # 8h rate in DECIMAL
        next_funding_time=None,
    )
    result = SimpleNamespace(funding_rates=[rate_data])
    adapter._sdk_call_with_retry = AsyncMock(return_value=result)

    await adapter.refresh_all_market_data()
    live_rate = adapter._funding_cache["BTC"].rate

    # Historical ingestion with interval=8 (divides by 8)
    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)
    ingestion = HistoricalIngestionService(store, settings)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [{"timestamp": 1704067200, "rate": "0.0008"}]  # 8h rate
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="BTC",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    hist_rate = candles[0].funding_rate_hourly

    # ASSERT: Both divide by 8 and produce same hourly rate
    assert live_rate == hist_rate == Decimal("0.0001"), (
        f"INTERVAL=8 MISMATCH!\n"
        f"  Live Adapter: {live_rate}\n"
        f"  Historical:   {hist_rate}\n"
        f"  Expected:     0.0001 (both should divide 0.0008 by 8)"
    )


@pytest.mark.asyncio
async def test_apy_consistency_between_components():
    """
    APY CONSISTENCY: Live and historical APY calculations must match.

    GIVEN: Both produce hourly rate 0.0001 (0.01% hourly)
    WHEN: Calculating APY
    THEN: Both must produce same APY (0.876 = 87.6% annualized)
    """
    settings = MagicMock()
    settings.lighter.funding_rate_interval_hours = Decimal("1")
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 0
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    # Live adapter APY
    adapter = LighterAdapter(settings)
    adapter._funding_api = MagicMock()

    rate_data = SimpleNamespace(
        exchange="lighter",
        market_id=1,
        symbol="ETH",
        rate="0.0008",  # SDK 8h rate → 0.0001 hourly
        next_funding_time=None,
    )
    result = SimpleNamespace(funding_rates=[rate_data])
    adapter._sdk_call_with_retry = AsyncMock(return_value=result)

    await adapter.refresh_all_market_data()
    live_hourly = adapter._funding_cache["ETH"].rate
    live_apy = adapter._funding_cache["ETH"].rate_annual

    # Historical APY
    store = MagicMock()
    store.insert_funding_candles = AsyncMock(return_value=1)
    ingestion = HistoricalIngestionService(store, settings)

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "fundings": [{"timestamp": 1704067200, "rate": "0.0001"}]  # REST 1h rate
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        candles = await ingestion._fetch_lighter_candles(
            symbol="ETH",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            end_time=datetime(2024, 1, 2, tzinfo=UTC),
            count_back=24
        )

    hist_hourly = candles[0].funding_rate_hourly
    hist_apy = candles[0].funding_apy

    # Expected: 0.0001 hourly * 24 * 365 = 0.876 APY (87.6% annualized)
    expected_apy = Decimal("0.0001") * Decimal("24") * Decimal("365")

    assert live_hourly == hist_hourly == Decimal("0.0001"), \
        f"Hourly rate mismatch: Live={live_hourly}, Hist={hist_hourly}"

    assert abs(live_apy - expected_apy) < Decimal("0.01"), \
        f"Live APY mismatch: {live_apy} vs expected {expected_apy}"
    assert abs(hist_apy - expected_apy) < Decimal("0.01"), \
        f"Historical APY mismatch: {hist_apy} vs expected {expected_apy}"
