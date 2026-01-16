from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from websockets.client import connect as connect_async

# Optional sync client (websockets>=12.0)
# Older versions (<12.0) don't have websockets.sync, so we gracefully degrade
try:
    from websockets.sync.client import connect as connect_sync
    _SYNC_CLIENT_AVAILABLE = True
except ImportError:
    # websockets<12.0 doesn't have sync client
    # The bot primarily uses async; sync is only for special cases
    connect_sync = None  # type: ignore
    _SYNC_CLIENT_AVAILABLE = False

# Optional Lighter SDK import for default host resolution
# The adapter always passes host explicitly, so this is only needed for the default case.
# This allows offline unit tests to run without the Lighter SDK installed.
try:
    from lighter.configuration import Configuration
    _LIGHTER_SDK_AVAILABLE = True
except ImportError:
    # Lighter SDK not installed - acceptable if host is always provided by adapter
    Configuration = None  # type: ignore
    _LIGHTER_SDK_AVAILABLE = False

from funding_bot.utils import json_dumps, json_loads

logger = logging.getLogger(__name__)


class WsClient:
    """
    Minimal Lighter WS client used by funding-bot.

    Why this exists:
    - The upstream `lighter.ws_client.WsClient` does not keep order book metadata (nonce/begin_nonce/offset)
      hot in the local state, which prevents safe continuity validation.
    - We need to verify `.tmp/lighter_ws.html` guidance: `begin_nonce` on the current update should match the
      previous update's `nonce` (per-market). If this fails, the local order book must be treated as stale.
    """

    def __init__(
        self,
        host: str | None = None,
        path: str = "/stream",
        order_book_ids: list[int] | None = None,
        account_ids: list[int] | None = None,
        on_order_book_update: Callable[[str, dict[str, Any]], Any] | None = print,
        on_account_update: Callable[[str, dict[str, Any]], Any] | None = print,
    ) -> None:
        if host is None:
            if not _LIGHTER_SDK_AVAILABLE:
                raise RuntimeError(
                    "WsClient with host=None requires the Lighter SDK to be installed. "
                    "Either install the Lighter SDK (pip install lighter) or provide an explicit host parameter. "
                    "Note: The adapter always passes host explicitly, so this error should not occur in normal operation."
                )
            host = Configuration.get_default().host.replace("https://", "")  # type: ignore

        self.base_url = f"wss://{host}{path}"

        self.subscriptions = {
            "order_books": list(order_book_ids or []),
            "accounts": list(account_ids or []),
        }

        if len(self.subscriptions["order_books"]) == 0 and len(self.subscriptions["accounts"]) == 0:
            raise Exception("No subscriptions provided.")

        self.order_book_states: dict[str, dict[str, Any]] = {}
        self.account_states: dict[str, dict[str, Any]] = {}

        self.on_order_book_update = on_order_book_update
        self.on_account_update = on_account_update

        self.ws: Any = None

    def on_message(self, ws: Any, message: Any) -> None:
        if isinstance(message, str):
            message = json_loads(message)

        message_type = message.get("type")

        if message_type == "connected":
            self.handle_connected(ws)
        elif message_type == "subscribed/order_book":
            self.handle_subscribed_order_book(message)
        elif message_type == "update/order_book":
            self.handle_update_order_book(message)
        elif message_type == "subscribed/account_all":
            self.handle_subscribed_account(message)
        elif message_type == "update/account_all":
            self.handle_update_account(message)
        elif message_type == "ping":
            ws.send(json_dumps({"type": "pong"}))
        else:
            self.handle_unhandled_message(message)

    async def on_message_async(self, ws: Any, message: str) -> None:
        msg = json_loads(message)
        message_type = msg.get("type")

        if message_type == "connected":
            await self.handle_connected_async(ws)
        elif message_type == "ping":
            await ws.send(json_dumps({"type": "pong"}))
        else:
            # Safe because all remaining handlers are "pure" (no await needed).
            self.on_message(ws, msg)

    def handle_connected(self, ws: Any) -> None:
        for market_id in self.subscriptions["order_books"]:
            ws.send(json_dumps({"type": "subscribe", "channel": f"order_book/{market_id}"}))
        for account_id in self.subscriptions["accounts"]:
            ws.send(json_dumps({"type": "subscribe", "channel": f"account_all/{account_id}"}))

    async def handle_connected_async(self, ws: Any) -> None:
        for market_id in self.subscriptions["order_books"]:
            await ws.send(json_dumps({"type": "subscribe", "channel": f"order_book/{market_id}"}))
        for account_id in self.subscriptions["accounts"]:
            await ws.send(json_dumps({"type": "subscribe", "channel": f"account_all/{account_id}"}))

    def handle_subscribed_order_book(self, message: dict[str, Any]) -> None:
        market_id = message["channel"].split(":")[1]
        self.order_book_states[market_id] = message["order_book"]
        if self.on_order_book_update:
            try:
                self.on_order_book_update(market_id, self.order_book_states[market_id])
            except Exception as e:
                logger.warning(f"on_order_book_update failed (ignored): {e}")

    def handle_update_order_book(self, message: dict[str, Any]) -> None:
        market_id = message["channel"].split(":")[1]
        self.update_order_book_state(market_id, message["order_book"])
        if self.on_order_book_update:
            try:
                self.on_order_book_update(market_id, self.order_book_states[market_id])
            except Exception as e:
                logger.warning(f"on_order_book_update failed (ignored): {e}")

    def update_order_book_state(self, market_id: str, order_book: dict[str, Any]) -> None:
        state = self.order_book_states.get(market_id)
        if not state:
            self.order_book_states[market_id] = order_book
            return

        self.update_orders(order_book.get("asks", []), state.setdefault("asks", []))
        self.update_orders(order_book.get("bids", []), state.setdefault("bids", []))

        # Keep metadata hot so downstream code can validate continuity / gaps.
        for key in ("code", "offset", "nonce", "begin_nonce"):
            if key in order_book:
                state[key] = order_book[key]

    def update_orders(self, new_orders: list[dict[str, Any]], existing_orders: list[dict[str, Any]]) -> None:
        """
        Update existing orders with new data.

        PERFORMANCE: O(n) using dictionary lookups instead of O(n²) nested loops.
        For 100 orders: 10,000 comparisons → 100 comparisons (100× faster).
        """
        # Build price-to-order mapping for O(1) lookups
        # This transforms the list into a dictionary indexed by price
        price_to_order = {order.get("price"): order for order in existing_orders}

        # Process new orders - O(n) instead of O(n²)
        for new_order in new_orders:
            price = new_order.get("price")
            new_size = new_order.get("size", 0)

            if price in price_to_order:
                # Update existing order
                price_to_order[price]["size"] = new_size
            else:
                # Add new order
                price_to_order[price] = new_order

        # Rebuild list with non-zero orders only - O(n)
        # This also removes orders with size=0 efficiently
        existing_orders[:] = [
            order for order in price_to_order.values()
            if float(order.get("size", 0)) > 0
        ]

    def handle_subscribed_account(self, message: dict[str, Any]) -> None:
        account_id = message["channel"].split(":")[1]
        self.account_states[account_id] = message
        if self.on_account_update:
            self.on_account_update(account_id, self.account_states[account_id])

    def handle_update_account(self, message: dict[str, Any]) -> None:
        account_id = message["channel"].split(":")[1]
        self.account_states[account_id] = message
        if self.on_account_update:
            self.on_account_update(account_id, self.account_states[account_id])

    def handle_unhandled_message(self, message: dict[str, Any]) -> None:
        raise Exception(f"Unhandled message: {message}")

    def run(self) -> None:
        """Run synchronous WebSocket client (requires websockets>=12.0)."""
        if not _SYNC_CLIENT_AVAILABLE:
            raise RuntimeError(
                "Synchronous WebSocket client requires websockets>=12.0. "
                "Please upgrade: pip install 'websockets>=12.0'. "
                "Alternatively, use run_async() for async WebSocket support."
            )
        if connect_sync is None:
            raise RuntimeError("Sync client not available (should not happen)")

        ws = connect_sync(self.base_url)
        self.ws = ws
        for message in ws:
            self.on_message(ws, message)

    async def run_async(self) -> None:
        ws = await connect_async(self.base_url)
        self.ws = ws
        async for message in ws:
            await self.on_message_async(ws, message)
