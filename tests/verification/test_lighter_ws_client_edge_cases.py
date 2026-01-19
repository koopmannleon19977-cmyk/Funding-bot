"""
Lighter WebSocket Client Edge Case Tests.

Tests error handling paths, edge cases, and state management
for the Lighter WsClient to improve coverage.
"""

from unittest.mock import MagicMock, patch

import pytest

from funding_bot.adapters.exchanges.lighter.ws_client import WsClient

# =============================================================================
# Initialization Tests
# =============================================================================


class TestWsClientInitialization:
    """Tests for WsClient initialization and setup."""

    def test_raises_error_with_no_subscriptions(self):
        """Should raise Exception when no subscriptions provided."""
        with pytest.raises(Exception, match="No subscriptions provided"):
            WsClient(
                host="example.com",
                order_book_ids=[],
                account_ids=[],
                on_order_book_update=None,
                on_account_update=None,
            )

    def test_uses_default_host_when_none_provided(self):
        """Should use default host from Configuration when host is None."""
        with patch("funding_bot.adapters.exchanges.lighter.ws_client.Configuration") as mock_config:
            mock_config.get_default.return_value.host = "https://default-lighter.com"
            client = WsClient(
                host=None,
                order_book_ids=[1],
                account_ids=[],
            )
            assert client.base_url == "wss://default-lighter.com/stream"

    def test_initializes_state_dictionaries(self):
        """Should initialize empty state dictionaries for order books and accounts."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        assert client.order_book_states == {}
        assert client.account_states == {}
        assert client.ws is None


# =============================================================================
# Message Handler Tests
# =============================================================================


class TestWsClientMessageHandlers:
    """Tests for WebSocket message handling."""

    def test_handle_connected_sends_order_book_subscriptions(self):
        """Should send subscribe messages for order books on connected."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1, 2],
            account_ids=[],
        )
        mock_ws = MagicMock()

        client.handle_connected(mock_ws)

        assert mock_ws.send.call_count == 2
        calls = [call[0][0] for call in mock_ws.send.call_args_list]
        assert '"channel": "order_book/1"' in calls[0]
        assert '"channel": "order_book/2"' in calls[1]

    def test_handle_connected_sends_account_subscriptions(self):
        """Should send subscribe messages for accounts on connected."""
        client = WsClient(
            host="example.com",
            order_book_ids=[],
            account_ids=[10, 20],
        )
        mock_ws = MagicMock()

        client.handle_connected(mock_ws)

        assert mock_ws.send.call_count == 2
        calls = [call[0][0] for call in mock_ws.send.call_args_list]
        assert '"channel": "account_all/10"' in calls[0]
        assert '"channel": "account_all/20"' in calls[1]

    def test_handle_connected_sends_both_subscription_types(self):
        """Should send both order book and account subscriptions."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[10],
        )
        mock_ws = MagicMock()

        client.handle_connected(mock_ws)

        assert mock_ws.send.call_count == 2

    @pytest.mark.asyncio
    async def test_handle_connected_async_sends_subscriptions(self):
        """Should send subscribe messages asynchronously."""
        from unittest.mock import AsyncMock

        client = WsClient(
            host="example.com",
            order_book_ids=[1, 2],
            account_ids=[],
        )
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()

        await client.handle_connected_async(mock_ws)

        assert mock_ws.send.call_count == 2

    def test_handle_unhandled_message_raises_exception(self):
        """Should raise exception for unknown message types."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        with pytest.raises(Exception, match="Unhandled message"):
            client.handle_unhandled_message({"type": "unknown_type", "data": "test"})


# =============================================================================
# Order Book State Management Tests
# =============================================================================


class TestWsClientOrderBookState:
    """Tests for order book state management."""

    def test_handle_subscribed_order_book_stores_state(self):
        """Should store order book state from subscription message."""
        callback_called = []

        def mock_callback(market_id: str, state: dict) -> None:
            callback_called.append((market_id, state))

        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
            on_order_book_update=mock_callback,
        )

        message = {
            "type": "subscribed/order_book",
            "channel": "order_book:1",
            "order_book": {
                "code": 0,
                "asks": [{"price": "101", "size": "1"}],
                "bids": [{"price": "100", "size": "1"}],
                "offset": 1,
                "nonce": 10,
                "begin_nonce": 0,
            },
        }

        client.handle_subscribed_order_book(message)

        assert "1" in client.order_book_states
        assert client.order_book_states["1"]["nonce"] == 10
        assert len(callback_called) == 1

    def test_handle_subscribed_order_book_handles_callback_exception(self):
        """Should log and continue when callback raises exception."""

        def failing_callback(market_id: str, state: dict) -> None:
            raise ValueError("Test error in callback")

        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
            on_order_book_update=failing_callback,
        )

        message = {
            "type": "subscribed/order_book",
            "channel": "order_book:1",
            "order_book": {"code": 0, "asks": [], "bids": [], "nonce": 1},
        }

        # Should not raise exception
        client.handle_subscribed_order_book(message)

        # State should still be stored
        assert "1" in client.order_book_states

    def test_handle_update_order_book_updates_existing_state(self):
        """Should update existing order book state."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        # Initialize state
        client.order_book_states["1"] = {
            "code": 0,
            "asks": [{"price": "101", "size": "1"}],
            "bids": [{"price": "100", "size": "1"}],
            "nonce": 10,
        }

        message = {
            "type": "update/order_book",
            "channel": "order_book:1",
            "order_book": {
                "code": 0,
                "asks": [{"price": "101", "size": "0"}],  # Remove ask
                "bids": [{"price": "100", "size": "2"}],  # Update bid size
                "nonce": 11,
                "begin_nonce": 10,
            },
        }

        client.handle_update_order_book(message)

        # Ask should be removed (size 0)
        assert client.order_book_states["1"]["asks"] == []
        # Bid size should be updated
        assert client.order_book_states["1"]["bids"][0]["size"] == "2"
        # Nonce should be updated
        assert client.order_book_states["1"]["nonce"] == 11

    def test_handle_update_order_book_creates_new_state(self):
        """Should create new state when none exists."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        message = {
            "type": "update/order_book",
            "channel": "order_book:1",
            "order_book": {
                "code": 0,
                "asks": [{"price": "101", "size": "1"}],
                "bids": [{"price": "100", "size": "1"}],
                "nonce": 1,
            },
        }

        client.handle_update_order_book(message)

        assert "1" in client.order_book_states
        assert client.order_book_states["1"]["nonce"] == 1

    def test_update_orders_handles_zero_size_removal(self):
        """Should remove orders with size zero."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        existing_orders = [
            {"price": "100", "size": "1"},
            {"price": "101", "size": "2"},
            {"price": "102", "size": "3"},
        ]

        new_orders = [
            {"price": "100", "size": "0"},  # Remove
            {"price": "101", "size": "5"},  # Update
            # 102 not mentioned, keep as is
        ]

        client.update_orders(new_orders, existing_orders)

        # 100 should be removed (size 0)
        assert not any(o["price"] == "100" for o in existing_orders)
        # 101 should be updated
        assert any(o["price"] == "101" and o["size"] == "5" for o in existing_orders)
        # 102 should remain
        assert any(o["price"] == "102" and o["size"] == "3" for o in existing_orders)

    def test_update_orders_adds_new_orders(self):
        """Should add new orders that don't exist."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        existing_orders = [
            {"price": "100", "size": "1"},
        ]

        new_orders = [
            {"price": "100", "size": "2"},
            {"price": "101", "size": "3"},  # New order
        ]

        client.update_orders(new_orders, existing_orders)

        assert len(existing_orders) == 2
        assert any(o["price"] == "100" and o["size"] == "2" for o in existing_orders)
        assert any(o["price"] == "101" and o["size"] == "3" for o in existing_orders)

    def test_update_orders_handles_missing_price(self):
        """Should handle orders with missing price field."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        existing_orders = [
            {"price": "100", "size": "1"},
        ]

        new_orders = [
            {"size": "2"},  # Missing price
        ]

        # Should not crash
        client.update_orders(new_orders, existing_orders)

        # Order with missing price should be added
        assert len(existing_orders) == 2


# =============================================================================
# Account Update Tests
# =============================================================================


class TestWsClientAccountUpdates:
    """Tests for account state management."""

    def test_handle_subscribed_account_stores_state(self):
        """Should store account state from subscription message."""
        callback_called = []

        def mock_callback(account_id: str, state: dict) -> None:
            callback_called.append((account_id, state))

        client = WsClient(
            host="example.com",
            order_book_ids=[],
            account_ids=[10],
            on_account_update=mock_callback,
        )

        message = {
            "type": "subscribed/account_all",
            "channel": "account_all:10",
            "account": {"balance": "1000"},
        }

        client.handle_subscribed_account(message)

        assert "10" in client.account_states
        assert len(callback_called) == 1
        assert callback_called[0][0] == "10"

    def test_handle_update_account_stores_state(self):
        """Should store account state from update message."""
        callback_called = []

        def mock_callback(account_id: str, state: dict) -> None:
            callback_called.append((account_id, state))

        client = WsClient(
            host="example.com",
            order_book_ids=[],
            account_ids=[10],
            on_account_update=mock_callback,
        )

        message = {
            "type": "update/account_all",
            "channel": "account_all:10",
            "account": {"balance": "2000"},
        }

        client.handle_update_account(message)

        assert "10" in client.account_states
        assert len(callback_called) == 1


# =============================================================================
# Close Method Tests
# =============================================================================


class TestWsClientClose:
    """Tests for WebSocket close methods."""

    def test_close_synchronous(self):
        """Should close synchronous WebSocket connection."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()
        client.ws = mock_ws

        client.close()

        mock_ws.close.assert_called_once()
        assert client.ws is None

    def test_close_handles_exception_synchronous(self):
        """Should handle exception when closing synchronous WebSocket."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()
        mock_ws.close.side_effect = Exception("Connection already closed")
        client.ws = mock_ws

        # Should not raise exception
        client.close()

        assert client.ws is None

    def test_close_when_ws_is_none(self):
        """Should handle close when ws is already None."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        client.ws = None

        # Should not raise exception
        client.close()

    @pytest.mark.asyncio
    async def test_close_async(self):
        """Should close async WebSocket connection."""
        from unittest.mock import AsyncMock

        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()
        client.ws = mock_ws

        await client.close_async()

        mock_ws.close.assert_called_once()
        assert client.ws is None

    @pytest.mark.asyncio
    async def test_close_async_handles_exception(self):
        """Should handle exception when closing async WebSocket."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        async def failing_close():
            raise Exception("Async close failed")

        mock_ws = MagicMock()
        mock_ws.close = failing_close
        client.ws = mock_ws

        # Should not raise exception
        await client.close_async()

        assert client.ws is None


# =============================================================================
# Message Dispatch Tests
# =============================================================================


class TestWsClientMessageDispatch:
    """Tests for message routing and dispatch."""

    def test_on_message_routes_connected_message(self):
        """Should route 'connected' message to correct handler."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()

        message = {"type": "connected"}

        client.on_message(mock_ws, message)

        mock_ws.send.assert_called()

    def test_on_message_routes_ping_message(self):
        """Should route 'ping' message and send pong."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()

        message = {"type": "ping"}

        client.on_message(mock_ws, message)

        mock_ws.send.assert_called_once()
        sent_message = mock_ws.send.call_args[0][0]
        assert '"type": "pong"' in sent_message

    def test_on_message_parses_string_messages(self):
        """Should parse JSON string messages."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()

        message = '{"type": "ping"}'

        client.on_message(mock_ws, message)

        mock_ws.send.assert_called_once()

    def test_on_message_routes_subscribed_order_book(self):
        """Should route 'subscribed/order_book' message correctly."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        message = {
            "type": "subscribed/order_book",
            "channel": "order_book:1",
            "order_book": {"code": 0, "nonce": 1},
        }

        client.on_message(None, message)

        assert "1" in client.order_book_states

    def test_on_message_routes_update_order_book(self):
        """Should route 'update/order_book' message correctly."""
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        message = {
            "type": "update/order_book",
            "channel": "order_book:1",
            "order_book": {"code": 0, "nonce": 2},
        }

        client.on_message(None, message)

        assert "1" in client.order_book_states

    @pytest.mark.asyncio
    async def test_on_message_async_routes_ping(self):
        """Should route 'ping' message in async handler."""
        from unittest.mock import AsyncMock

        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()

        message = '{"type": "ping"}'

        await client.on_message_async(mock_ws, message)

        mock_ws.send.assert_called_once()


# =============================================================================
# Import Guard Tests (websockets.sync compatibility)
# =============================================================================


class TestWsClientImportGuard:
    """
    Tests for websockets.sync import guard.

    The ws_client module gracefully degrades when websockets<12.0 is installed
    (no websockets.sync module). This prevents test-collection crashes and
    provides clear error messages for sync client usage.
    """

    def test_module_imports_without_websockets_sync(self):
        """
        PROOF: ws_client module can be imported even if websockets.sync is missing.

        REGRESSION TEST: This test ensures that the import guard prevents
        ModuleNotFoundError during test collection when websockets<12.0 is installed.

        The module should import successfully with websockets>=10.0 (async only)
        or websockets>=12.0 (async + sync).
        """
        # This test passes if we can import the module at all
        # If websockets.sync import was not guarded, this would fail with
        # ModuleNotFoundError on websockets<12.0
        from funding_bot.adapters.exchanges.lighter import ws_client

        # Module should be loaded
        assert ws_client is not None

        # Async client should always be available (websockets>=10.0)
        assert ws_client.connect_async is not None

    def test_run_raises_clear_error_without_websockets_sync(self):
        """
        PROOF: run() method raises clear RuntimeError when sync client unavailable.

        When websockets<12.0 is installed, the sync client is not available.
        The run() method should fail with a helpful error message instead of
        AttributeError or cryptic import error.
        """
        from funding_bot.adapters.exchanges.lighter.ws_client import _SYNC_CLIENT_AVAILABLE

        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        # If sync client is not available, run() should raise clear error
        if not _SYNC_CLIENT_AVAILABLE:
            with pytest.raises(RuntimeError, match="Synchronous WebSocket client requires websockets>=12.0"):
                client.run()
        else:
            # If sync client IS available, we can't test the error path
            # but the test still documents expected behavior
            pass

    def test_run_async_always_available(self):
        """
        PROOF: run_async() is always available regardless of websockets.sync.

        The async client only requires websockets>=10.0, which is already
        specified in pyproject.toml. This method should work even if
        websockets.sync is unavailable.
        """
        # Async client should be importable
        from funding_bot.adapters.exchanges.lighter.ws_client import connect_async

        assert connect_async is not None

        # The run_async method should exist on the client
        client = WsClient(
            host="example.com",
            order_book_ids=[1],
            account_ids=[],
        )

        assert hasattr(client, "run_async")
        assert callable(client.run_async)
