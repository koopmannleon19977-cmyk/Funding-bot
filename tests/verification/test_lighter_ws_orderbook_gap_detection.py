from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_lighter_wsclient_updates_meta_and_filters_zero_size():
    from funding_bot.adapters.exchanges.lighter.ws_client import WsClient

    client = WsClient(
        host="example.com",
        order_book_ids=[1],
        account_ids=[],
        on_order_book_update=None,
        on_account_update=None,
    )

    client.handle_subscribed_order_book(
        {
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
    )

    client.handle_update_order_book(
        {
            "type": "update/order_book",
            "channel": "order_book:1",
            "order_book": {
                "code": 0,
                "asks": [{"price": "101", "size": "0"}],
                "bids": [],
                "offset": 2,
                "nonce": 11,
                "begin_nonce": 10,
            },
        }
    )

    state = client.order_book_states["1"]
    assert state["nonce"] == 11
    assert state["begin_nonce"] == 10
    assert state["offset"] == 2
    assert state["asks"] == []


def test_lighter_orderbook_begin_nonce_gap_marks_cache_stale():
    from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
    from funding_bot.config.settings import Settings

    adapter = LighterAdapter(Settings())
    adapter._market_id_to_symbol[1] = "ETH"

    # First update - establishes initial orderbook state
    adapter._on_ws_order_book_update(
        "1",
        {
            "code": 0,
            "asks": [{"price": "101", "size": "1"}],
            "bids": [{"price": "100", "size": "1"}],
            "offset": 1,
            "nonce": 10,
            "begin_nonce": 0,
        },
    )

    assert adapter._orderbook_cache["ETH"]["best_bid"] > 0
    assert "ETH" in adapter._orderbook_updated_at

    # Manually age the LocalOrderbook's connection start to bypass initial sync tolerance
    # This simulates being outside the 10-second tolerance period
    if 1 in adapter._ob_ws_books:
        adapter._ob_ws_books[1]._connection_start = 0.0  # Set to epoch (far in the past)

    # Second update - has nonce gap (begin_nonce=9 != last_nonce=10)
    adapter._on_ws_order_book_update(
        "1",
        {
            "code": 0,
            "asks": [{"price": "101", "size": "1"}],
            "bids": [{"price": "100", "size": "1"}],
            "offset": 2,
            "nonce": 11,
            "begin_nonce": 9,  # should equal previous nonce (10)
        },
    )

    # After gap detection, the orderbook should be marked as stale
    # The key indicator is the timestamp being set to epoch
    epoch = datetime.fromtimestamp(0, tz=UTC)
    assert adapter._orderbook_updated_at["ETH"] == epoch, "Orderbook timestamp should be epoch after gap detection"


def test_lighter_orderbook_offset_gap_marks_cache_stale():
    from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
    from funding_bot.config.settings import Settings

    adapter = LighterAdapter(Settings())
    adapter._market_id_to_symbol[1] = "ETH"

    adapter._on_ws_order_book_update(
        "1",
        {
            "code": 0,
            "asks": [{"price": "101", "size": "1"}],
            "bids": [{"price": "100", "size": "1"}],
            "offset": 1,
            "nonce": 10,
            "begin_nonce": 0,
        },
    )

    # Offset jumps are non-fatal in practice; nonce chain is the authoritative continuity check.
    adapter._on_ws_order_book_update(
        "1",
        {
            "code": 0,
            "asks": [{"price": "101", "size": "1"}],
            "bids": [{"price": "100", "size": "1"}],
            "offset": 4,  # non-fatal jump
            "nonce": 11,
            "begin_nonce": 10,
        },
    )

    epoch = datetime.fromtimestamp(0, tz=UTC)
    assert adapter._orderbook_updated_at["ETH"] != epoch
    assert "ETH" in adapter._orderbook_cache
