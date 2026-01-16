"""
REGRESSION TESTS: Lighter Adapter Funding Rate Normalization.

These tests ensure that the Lighter adapter correctly normalizes funding rates
from the API (which returns 8-hour rates) to hourly rates.

CRITICAL: Lighter FundingApi.funding_rates() returns 8-hour rates in DECIMAL format.
The adapter divides by 8 to get hourly rates. NO division by 100 is needed.

Based on official documentation (docs.lighter.xyz), Lighter funding is applied
hourly, but the API returns 8-hour cumulative rates.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.domain.models import Exchange


@pytest.mark.asyncio
async def test_lighter_refresh_all_market_data_divides_by_8():
    """
    PROOF: Lighter funding rate normalization in refresh_all_market_data.

    REGRESSION TEST: This test ensures that refresh_all_market_data correctly
    converts the 8-hour rate from the API to an hourly rate by dividing by 8.

    The API returns DECIMAL format (e.g., 0.0008 = 0.08% per 8 hours).
    After division by 8: 0.0001 = 0.01% per hour.
    """
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"
    settings.lighter.funding_rate_interval_hours = Decimal("1")

    adapter = LighterAdapter(settings)
    adapter._funding_api = MagicMock()

    # Lighter API returns 8-hour rate in DECIMAL format (0.0008 = 0.08% per 8h)
    # refresh_all_market_data divides by 8 to get hourly rate
    rate_data = SimpleNamespace(
        exchange="lighter",
        market_id=1,
        symbol="LIT",
        rate="0.0008",  # 0.08% per 8 hours (DECIMAL format)
        next_funding_time=None,
    )
    result = SimpleNamespace(funding_rates=[rate_data])
    adapter._sdk_call_with_retry = AsyncMock(return_value=result)  # type: ignore[method-assign]

    await adapter.refresh_all_market_data()

    assert adapter._funding_cache["LIT"].exchange == Exchange.LIGHTER
    # 0.0008 / 8 = 0.0001 (hourly rate)
    assert adapter._funding_cache["LIT"].rate == Decimal("0.0001")


@pytest.mark.asyncio
async def test_lighter_refresh_all_market_data_realistic_values():
    """
    PROOF: Lighter funding rate normalization with realistic values.

    Using realistic funding rate values from actual API responses.
    Example: 0.0005 = 0.05% per 8 hours → 0.0000625 per hour
    """
    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 123
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"
    settings.lighter.funding_rate_interval_hours = Decimal("1")

    adapter = LighterAdapter(settings)
    adapter._funding_api = MagicMock()

    # Realistic rate from API docs: "0.0005" (8h rate)
    rate_data = SimpleNamespace(
        exchange="lighter",
        market_id=1,
        symbol="BTC",
        rate="0.0005",  # 0.05% per 8 hours
        next_funding_time=None,
    )
    result = SimpleNamespace(funding_rates=[rate_data])
    adapter._sdk_call_with_retry = AsyncMock(return_value=result)

    await adapter.refresh_all_market_data()

    # 0.0005 / 8 = 0.0000625 hourly
    assert adapter._funding_cache["BTC"].rate == Decimal("0.0005") / Decimal("8")
