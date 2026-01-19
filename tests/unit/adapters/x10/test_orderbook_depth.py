"""
Unit tests for X10 adapter orderbook depth functionality.

Tests that the X10 adapter correctly passes the limit parameter to the SDK
and returns multiple depth levels (not just L1).
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.config.settings import ExchangeSettings, Settings
from funding_bot.domain.errors import ExchangeError


@pytest.fixture
def mock_settings():
    """Create mock settings for X10 adapter."""
    return Settings(
        x10=ExchangeSettings(
            api_key="test_key",
            public_key="test_public_key",
            private_key="test_private_key",
            vault_id="1",
            account_index=0,
        )
    )


@pytest.fixture
def mock_trading_client():
    """Create a mock X10 trading client."""
    client = AsyncMock()
    return client


@pytest.fixture
def x10_adapter(mock_settings, mock_trading_client):
    """Create an X10 adapter instance with mocked trading client."""
    adapter = X10Adapter(settings=mock_settings)
    adapter._trading_client = mock_trading_client
    adapter._orderbook_snapshot_supports_limit = True
    return adapter


@pytest.mark.asyncio
async def test_get_orderbook_l1_passes_limit_parameter(x10_adapter, mock_trading_client):
    """Verify that get_orderbook_l1 passes limit=1 to the SDK."""
    # Arrange: Mock SDK response with L1 data
    mock_response = MagicMock()
    mock_response.data.bid = [MagicMock(price=Decimal("50000"), qty=Decimal("1.5"))]
    mock_response.data.ask = [MagicMock(price=Decimal("50010"), qty=Decimal("2.0"))]

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act
    result = await x10_adapter.get_orderbook_l1("BTC-USD")

    # Assert: _sdk_call_with_retry was called
    assert x10_adapter._sdk_call_with_retry.called

    # Get the lambda function that was passed
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]

    # Call the lambda to verify it passes the right parameters to the SDK
    lambda_func()  # This will call mock_trading_client.markets_info.get_orderbook_snapshot

    # Assert: SDK was called with limit=1
    mock_trading_client.markets_info.get_orderbook_snapshot.assert_called_once()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs

    assert "market_name" in call_kwargs
    assert call_kwargs["market_name"] == "BTC-USD"
    assert "limit" in call_kwargs
    assert call_kwargs["limit"] == 1

    # Assert: L1 data is returned correctly
    assert result["best_bid"] == Decimal("50000")
    assert result["best_ask"] == Decimal("50010")
    assert result["bid_qty"] == Decimal("1.5")
    assert result["ask_qty"] == Decimal("2.0")


@pytest.mark.asyncio
async def test_get_orderbook_depth_passes_limit_parameter(x10_adapter, mock_trading_client):
    """Verify that get_orderbook_depth passes the depth limit to the SDK."""
    # Arrange: Mock SDK response with 5 levels of depth
    mock_response = MagicMock()

    # Create 5 bid levels
    mock_bids = []
    for i in range(5):
        level = MagicMock()
        level.price = Decimal(str(50000 - i * 10))
        level.qty = Decimal(str(1.0 + i * 0.5))
        mock_bids.append(level)

    # Create 5 ask levels
    mock_asks = []
    for i in range(5):
        level = MagicMock()
        level.price = Decimal(str(50010 + i * 10))
        level.qty = Decimal(str(1.0 + i * 0.5))
        mock_asks.append(level)

    mock_response.data.bid = mock_bids
    mock_response.data.ask = mock_asks

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act: Request depth=5
    result = await x10_adapter.get_orderbook_depth("BTC-USD", depth=5)

    # Assert: _sdk_call_with_retry was called
    assert x10_adapter._sdk_call_with_retry.called

    # Get the lambda function that was passed
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]

    # Call the lambda to verify it passes the right parameters to the SDK
    lambda_func()  # This will call mock_trading_client.markets_info.get_orderbook_snapshot

    # Assert: SDK was called with limit=5
    mock_trading_client.markets_info.get_orderbook_snapshot.assert_called_once()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs

    assert "market_name" in call_kwargs
    assert call_kwargs["market_name"] == "BTC-USD"
    assert "limit" in call_kwargs
    assert call_kwargs["limit"] == 5

    # Assert: All 5 levels are returned
    assert len(result["bids"]) == 5
    assert len(result["asks"]) == 5

    # Verify first bid level
    assert result["bids"][0] == (Decimal("50000"), Decimal("1.0"))

    # Verify last bid level
    assert result["bids"][4] == (Decimal("49960"), Decimal("3.0"))


@pytest.mark.asyncio
async def test_get_orderbook_depth_handles_low_volume_symbols(x10_adapter, mock_trading_client):
    """Test that get_orderbook_depth works correctly for low-volume symbols like EDEN."""
    # Arrange: Mock SDK response with limited depth (only 2 levels available)
    mock_response = MagicMock()

    # EDEN symbol might only have 2 levels
    mock_response.data.bid = [
        MagicMock(price=Decimal("0.5"), qty=Decimal("100")),
        MagicMock(price=Decimal("0.49"), qty=Decimal("50")),
    ]
    mock_response.data.ask = [
        MagicMock(price=Decimal("0.51"), qty=Decimal("200")),
        MagicMock(price=Decimal("0.52"), qty=Decimal("150")),
    ]

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act: Request depth=20 but only 2 available
    result = await x10_adapter.get_orderbook_depth("EDEN-USD", depth=20)

    # Assert: SDK was called with limit=20
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]
    lambda_func()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs
    assert call_kwargs["limit"] == 20

    # Assert: Returns available levels (not empty)
    assert len(result["bids"]) == 2
    assert len(result["asks"]) == 2

    # Verify liquidity is aggregated correctly
    total_bid_qty = sum(qty for _, qty in result["bids"])
    total_ask_qty = sum(qty for _, qty in result["asks"])
    assert total_bid_qty == Decimal("150")  # 100 + 50
    assert total_ask_qty == Decimal("350")  # 200 + 150


@pytest.mark.asyncio
async def test_get_orderbook_depth_handles_empty_orderbook(x10_adapter, mock_trading_client):
    """Test that get_orderbook_depth handles empty orderbooks gracefully."""
    # Arrange: Mock SDK response with empty orderbook
    mock_response = MagicMock()
    mock_response.data.bid = []
    mock_response.data.ask = []

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act
    result = await x10_adapter.get_orderbook_depth("LOWVOL-USD", depth=5)

    # Assert: SDK was called with limit=5
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]
    lambda_func()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs
    assert call_kwargs["limit"] == 5

    # Assert: Returns empty lists (not crash)
    assert len(result["bids"]) == 0
    assert len(result["asks"]) == 0


@pytest.mark.asyncio
async def test_get_orderbook_depth_handles_list_format(x10_adapter, mock_trading_client):
    """Test that get_orderbook_depth handles list format [price, qty]."""
    # Arrange: Mock SDK returns list format instead of objects
    mock_response = MagicMock()
    mock_response.data.bid = [
        [Decimal("50000"), Decimal("1.0")],
        [Decimal("49990"), Decimal("2.0")],
    ]
    mock_response.data.ask = [
        [Decimal("50010"), Decimal("1.5")],
        [Decimal("50020"), Decimal("2.5")],
    ]

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act
    result = await x10_adapter.get_orderbook_depth("BTC-USD", depth=2)

    # Assert: Correctly parses list format
    assert len(result["bids"]) == 2
    assert len(result["asks"]) == 2
    assert result["bids"][0] == (Decimal("50000"), Decimal("1.0"))
    assert result["asks"][0] == (Decimal("50010"), Decimal("1.5"))


@pytest.mark.asyncio
async def test_get_orderbook_depth_respects_max_limit(x10_adapter, mock_trading_client):
    """Test that get_orderbook_depth caps limit at 200 (X10 API maximum)."""
    # Arrange
    mock_response = MagicMock()
    mock_response.data.bid = []
    mock_response.data.ask = []

    # Mock the _sdk_call_with_retry to return our mock response
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act: Request depth=500 (exceeds API max)
    result = await x10_adapter.get_orderbook_depth("BTC-USD", depth=500)

    # Assert: SDK was called with limit=200 (capped)
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]
    lambda_func()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs
    assert call_kwargs["limit"] == 200


@pytest.mark.asyncio
async def test_get_orderbook_depth_falls_back_without_limit(x10_adapter, mock_trading_client):
    """Verify fallback behavior when SDK does not support limit parameter."""
    # Arrange: force compatibility mode
    x10_adapter._orderbook_snapshot_supports_limit = False

    mock_response = MagicMock()
    mock_response.data.bid = [MagicMock(price=Decimal("50000"), qty=Decimal("1.0"))]
    mock_response.data.ask = [MagicMock(price=Decimal("50010"), qty=Decimal("1.5"))]
    x10_adapter._sdk_call_with_retry = AsyncMock(return_value=mock_response)

    # Act
    result = await x10_adapter.get_orderbook_depth("BTC-USD", depth=2)

    # Assert: SDK called without limit
    call_args = x10_adapter._sdk_call_with_retry.call_args
    lambda_func = call_args[0][0]
    lambda_func()
    call_kwargs = mock_trading_client.markets_info.get_orderbook_snapshot.call_args.kwargs
    assert "limit" not in call_kwargs

    assert result["bids"][0] == (Decimal("50000"), Decimal("1.0"))
    assert result["asks"][0] == (Decimal("50010"), Decimal("1.5"))


@pytest.mark.asyncio
async def test_get_orderbook_depth_handles_sdk_error(x10_adapter):
    """Test that get_orderbook_depth raises ExchangeError on SDK failure."""
    # Arrange: Mock _sdk_call_with_retry to raise an exception
    x10_adapter._sdk_call_with_retry = AsyncMock(side_effect=Exception("API Error"))

    # Act & Assert: Should raise ExchangeError
    with pytest.raises(ExchangeError) as exc_info:
        await x10_adapter.get_orderbook_depth("BTC-USD", depth=5)

    assert "Failed to get orderbook depth" in str(exc_info.value)
    assert "BTC-USD" in str(exc_info.value)
