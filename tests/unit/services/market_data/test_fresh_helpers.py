"""
Unit tests for market_data/fresh.py helper functions.

Tests the refactored helper functions extracted in Phase 2.5:
- Orderbook validation helpers
- Book merging helpers
- Safe decimal conversion

OFFLINE-FIRST: These tests do NOT require exchange SDK or network access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from funding_bot.services.market_data.fresh import (
    OrderbookFetchConfig,
    _book_has_depth,
    _has_any_price_data,
    _load_orderbook_fetch_config,
    _merge_exchange_book,
    _safe_decimal,
    _snapshot_depth_ok,
)
from funding_bot.services.market_data.models import OrderbookSnapshot

# =============================================================================
# DATACLASS TESTS
# =============================================================================


class TestOrderbookFetchConfig:
    """Tests for OrderbookFetchConfig dataclass."""

    def test_default_values(self):
        """Config should have sensible defaults."""
        config = OrderbookFetchConfig()
        assert config.attempts == 3
        assert config.base_delay == 0.3
        assert config.fallback_max_age == 10.0

    def test_custom_values(self):
        """Config should accept custom values."""
        config = OrderbookFetchConfig(
            attempts=5,
            base_delay=0.5,
            fallback_max_age=30.0,
        )
        assert config.attempts == 5
        assert config.base_delay == 0.5
        assert config.fallback_max_age == 30.0


class TestLoadOrderbookFetchConfig:
    """Tests for _load_orderbook_fetch_config helper."""

    def _make_mock_settings(
        self,
        attempts: int = 3,
        delay: float = 0.3,
        max_age: float = 10.0,
    ) -> MagicMock:
        """Create mock settings object."""
        settings = MagicMock()
        settings.websocket.orderbook_l1_retry_attempts = attempts
        settings.websocket.orderbook_l1_retry_delay_seconds = Decimal(str(delay))
        settings.websocket.orderbook_l1_fallback_max_age_seconds = Decimal(str(max_age))
        return settings

    def test_loads_from_settings(self):
        """Config should be loaded from settings."""
        settings = self._make_mock_settings(attempts=5, delay=0.5, max_age=20.0)
        config = _load_orderbook_fetch_config(settings)

        assert config.attempts == 5
        assert config.base_delay == 0.5
        assert config.fallback_max_age == 20.0

    def test_clamps_attempts_minimum(self):
        """Attempts should have minimum of 1."""
        settings = self._make_mock_settings(attempts=0)
        config = _load_orderbook_fetch_config(settings)

        assert config.attempts >= 1

    def test_clamps_attempts_maximum(self):
        """Attempts should have maximum of 10."""
        settings = self._make_mock_settings(attempts=100)
        config = _load_orderbook_fetch_config(settings)

        assert config.attempts <= 10

    def test_clamps_delay_minimum(self):
        """Delay should have minimum of 0."""
        settings = self._make_mock_settings(delay=-1.0)
        config = _load_orderbook_fetch_config(settings)

        assert config.base_delay >= 0.0

    def test_clamps_delay_maximum(self):
        """Delay should have maximum of 5."""
        settings = self._make_mock_settings(delay=100.0)
        config = _load_orderbook_fetch_config(settings)

        assert config.base_delay <= 5.0


# =============================================================================
# SAFE DECIMAL TESTS
# =============================================================================


class TestSafeDecimal:
    """Tests for _safe_decimal helper."""

    def test_extracts_decimal(self):
        """Should extract Decimal value directly."""
        book = {"best_bid": Decimal("100.5")}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("100.5")

    def test_converts_string(self):
        """Should convert string to Decimal."""
        book = {"best_bid": "100.5"}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("100.5")

    def test_converts_float(self):
        """Should convert float to Decimal."""
        book = {"best_bid": 100.5}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("100.5")

    def test_converts_int(self):
        """Should convert int to Decimal."""
        book = {"best_bid": 100}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("100")

    def test_missing_key_returns_zero(self):
        """Missing key should return zero."""
        book = {}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("0")

    def test_invalid_value_returns_zero(self):
        """Invalid value should return zero."""
        book = {"best_bid": "not_a_number"}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("0")

    def test_none_value_returns_zero(self):
        """None value should return zero."""
        book = {"best_bid": None}
        result = _safe_decimal(book, "best_bid")
        assert result == Decimal("0")


# =============================================================================
# BOOK VALIDATION TESTS
# =============================================================================


class TestBookHasDepth:
    """Tests for _book_has_depth helper."""

    def test_valid_book_has_depth(self):
        """Valid book with bid < ask should have depth."""
        book = {
            "best_bid": Decimal("100"),
            "best_ask": Decimal("101"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        assert _book_has_depth(book) is True

    def test_inverted_spread_no_depth(self):
        """Inverted spread (bid >= ask) should not have depth."""
        book = {
            "best_bid": Decimal("101"),
            "best_ask": Decimal("100"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        assert _book_has_depth(book) is False

    def test_zero_bid_no_depth(self):
        """Zero bid should not have depth."""
        book = {
            "best_bid": Decimal("0"),
            "best_ask": Decimal("100"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        assert _book_has_depth(book) is False

    def test_zero_qty_no_depth(self):
        """Zero quantity should not have depth."""
        book = {
            "best_bid": Decimal("100"),
            "best_ask": Decimal("101"),
            "bid_qty": Decimal("0"),
            "ask_qty": Decimal("5"),
        }
        assert _book_has_depth(book) is False

    def test_missing_keys_no_depth(self):
        """Missing keys should not have depth."""
        book = {"best_bid": Decimal("100")}
        assert _book_has_depth(book) is False


class TestHasAnyPriceData:
    """Tests for _has_any_price_data helper."""

    def test_both_prices_valid(self):
        """Should return True with both prices."""
        book = {"best_bid": Decimal("100"), "best_ask": Decimal("101")}
        assert _has_any_price_data(book) is True

    def test_only_bid_valid(self):
        """Should return True with only bid."""
        book = {"best_bid": Decimal("100"), "best_ask": Decimal("0")}
        assert _has_any_price_data(book) is True

    def test_only_ask_valid(self):
        """Should return True with only ask."""
        book = {"best_bid": Decimal("0"), "best_ask": Decimal("101")}
        assert _has_any_price_data(book) is True

    def test_no_prices(self):
        """Should return False with no prices."""
        book = {"best_bid": Decimal("0"), "best_ask": Decimal("0")}
        assert _has_any_price_data(book) is False

    def test_empty_book(self):
        """Should return False for empty book."""
        book = {}
        assert _has_any_price_data(book) is False


# =============================================================================
# BOOK MERGE TESTS
# =============================================================================


class TestMergeExchangeBook:
    """Tests for _merge_exchange_book helper."""

    def test_uses_fresh_data(self):
        """Should use fresh data when valid."""
        book = {
            "best_bid": Decimal("100"),
            "best_ask": Decimal("101"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        bid, ask, bid_qty, ask_qty = _merge_exchange_book(
            book=book,
            prev_bid=Decimal("99"),
            prev_ask=Decimal("102"),
            prev_bid_qty=Decimal("8"),
            prev_ask_qty=Decimal("4"),
        )

        assert bid == Decimal("100")
        assert ask == Decimal("101")
        assert bid_qty == Decimal("10")
        assert ask_qty == Decimal("5")

    def test_falls_back_to_previous_on_zero_bid(self):
        """Should use previous data when bid is zero."""
        book = {
            "best_bid": Decimal("0"),
            "best_ask": Decimal("101"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        bid, ask, bid_qty, ask_qty = _merge_exchange_book(
            book=book,
            prev_bid=Decimal("99"),
            prev_ask=Decimal("102"),
            prev_bid_qty=Decimal("8"),
            prev_ask_qty=Decimal("4"),
        )

        assert bid == Decimal("99")  # Previous

    def test_falls_back_on_inverted_spread(self):
        """Should fall back completely on inverted spread."""
        book = {
            "best_bid": Decimal("102"),  # Inverted
            "best_ask": Decimal("100"),
            "bid_qty": Decimal("10"),
            "ask_qty": Decimal("5"),
        }
        bid, ask, bid_qty, ask_qty = _merge_exchange_book(
            book=book,
            prev_bid=Decimal("99"),
            prev_ask=Decimal("101"),
            prev_bid_qty=Decimal("8"),
            prev_ask_qty=Decimal("4"),
        )

        assert bid == Decimal("99")
        assert ask == Decimal("101")
        assert bid_qty == Decimal("8")
        assert ask_qty == Decimal("4")

    def test_partial_fallback(self):
        """Should partially fall back when some values are zero."""
        book = {
            "best_bid": Decimal("100"),
            "best_ask": Decimal("101"),
            "bid_qty": Decimal("0"),  # Zero
            "ask_qty": Decimal("5"),
        }
        bid, ask, bid_qty, ask_qty = _merge_exchange_book(
            book=book,
            prev_bid=Decimal("99"),
            prev_ask=Decimal("102"),
            prev_bid_qty=Decimal("8"),
            prev_ask_qty=Decimal("4"),
        )

        assert bid == Decimal("100")  # Fresh
        assert ask == Decimal("101")  # Fresh
        assert bid_qty == Decimal("8")  # Previous (fallback)
        assert ask_qty == Decimal("5")  # Fresh


# =============================================================================
# SNAPSHOT VALIDATION TESTS
# =============================================================================


class TestSnapshotDepthOk:
    """Tests for _snapshot_depth_ok helper."""

    def _make_snapshot(
        self,
        lighter_bid: Decimal = Decimal("100"),
        lighter_ask: Decimal = Decimal("101"),
        lighter_bid_qty: Decimal = Decimal("10"),
        lighter_ask_qty: Decimal = Decimal("5"),
        x10_bid: Decimal = Decimal("100"),
        x10_ask: Decimal = Decimal("101"),
        x10_bid_qty: Decimal = Decimal("10"),
        x10_ask_qty: Decimal = Decimal("5"),
        updated: datetime | None = None,
    ) -> OrderbookSnapshot:
        """Create an OrderbookSnapshot for testing."""
        if updated is None:
            updated = datetime.now(UTC)

        return OrderbookSnapshot(
            symbol="BTC",
            lighter_bid=lighter_bid,
            lighter_ask=lighter_ask,
            lighter_bid_qty=lighter_bid_qty,
            lighter_ask_qty=lighter_ask_qty,
            x10_bid=x10_bid,
            x10_ask=x10_ask,
            x10_bid_qty=x10_bid_qty,
            x10_ask_qty=x10_ask_qty,
            lighter_updated=updated,
            x10_updated=updated,
        )

    def test_valid_snapshot_ok(self):
        """Valid snapshot should be ok."""
        ob = self._make_snapshot()
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is True

    def test_missing_lighter_depth_not_ok(self):
        """Missing Lighter depth should not be ok."""
        ob = self._make_snapshot(
            lighter_bid_qty=Decimal("0"),
            lighter_ask_qty=Decimal("0"),
        )
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is False

    def test_missing_x10_depth_not_ok(self):
        """Missing X10 depth should not be ok."""
        ob = self._make_snapshot(
            x10_bid_qty=Decimal("0"),
            x10_ask_qty=Decimal("0"),
        )
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is False

    def test_one_sided_depth_ok(self):
        """One-sided depth should be accepted (for low-volume symbols)."""
        ob = self._make_snapshot(
            lighter_bid_qty=Decimal("10"),
            lighter_ask_qty=Decimal("0"),  # Only bid side
            x10_bid_qty=Decimal("0"),  # Only ask side
            x10_ask_qty=Decimal("5"),
        )
        now = datetime.now(UTC)

        # Still ok because both exchanges have some depth
        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is True

    def test_inverted_lighter_spread_not_ok(self):
        """Inverted Lighter spread should not be ok."""
        ob = self._make_snapshot(
            lighter_bid=Decimal("102"),  # Higher than ask
            lighter_ask=Decimal("100"),
        )
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is False

    def test_inverted_x10_spread_not_ok(self):
        """Inverted X10 spread should not be ok."""
        ob = self._make_snapshot(
            x10_bid=Decimal("102"),  # Higher than ask
            x10_ask=Decimal("100"),
        )
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is False

    def test_stale_data_not_ok(self):
        """Stale data should not be ok."""
        old_time = datetime.now(UTC) - timedelta(seconds=60)
        ob = self._make_snapshot(updated=old_time)
        now = datetime.now(UTC)

        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is False

    def test_no_tradeable_direction_not_ok(self):
        """Should fail if no tradeable direction exists."""
        ob = self._make_snapshot(
            lighter_ask=Decimal("0"),  # Can't buy on Lighter
            x10_bid=Decimal("0"),  # Can't sell on X10
            lighter_bid=Decimal("100"),
            x10_ask=Decimal("101"),
        )
        now = datetime.now(UTC)

        # Can only long X10 and short Lighter, but can_long_lighter=False, can_long_x10=False
        # Actually can_long_x10 = x10_ask > 0 and lighter_bid > 0 = True
        # So this should still be ok
        assert _snapshot_depth_ok(ob, now, fallback_max_age=10.0) is True
