"""Tests for RiskGuard component."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.services.surge_pro.risk_guard import RiskCheckResult, RiskGuard


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.daily_loss_cap_usd = Decimal("5")
    settings.hourly_loss_pause_usd = Decimal("2")
    settings.loss_streak_pause_count = 5
    settings.min_fill_rate_percent = 20
    settings.pause_duration_minutes = 30
    return settings


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestDailyLossCap:
    """Test daily loss cap detection."""

    @pytest.mark.asyncio
    async def test_blocks_when_daily_cap_reached(self, mock_settings, mock_store):
        """Should block trading when daily loss cap reached."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-5.50"))

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "daily_loss_cap"

    @pytest.mark.asyncio
    async def test_allows_when_under_cap(self, mock_settings, mock_store):
        """Should allow trading when under daily loss cap."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-2.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is False


class TestLossStreak:
    """Test loss streak detection."""

    @pytest.mark.asyncio
    async def test_pauses_on_loss_streak(self, mock_settings, mock_store):
        """Should pause on consecutive losses."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-1.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))

        # 5 consecutive losses
        losses = [MagicMock(pnl=Decimal("-0.10")) for _ in range(5)]
        mock_store.get_recent_trades = AsyncMock(return_value=losses)
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "loss_streak"
        assert result.pause_minutes == 30


class TestHourlyLoss:
    """Test hourly loss pause detection."""

    @pytest.mark.asyncio
    async def test_pauses_on_hourly_loss(self, mock_settings, mock_store):
        """Should pause when hourly loss threshold reached."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-2.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-2.50"))

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "hourly_loss"
        assert result.pause_minutes == 30


class TestFillRate:
    """Test fill rate detection."""

    @pytest.mark.asyncio
    async def test_pauses_on_low_fill_rate(self, mock_settings, mock_store):
        """Should pause when fill rate too low."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-1.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.10)  # 10% < 20%

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "low_fill_rate"
        assert result.pause_minutes == 15  # Half of normal pause

    @pytest.mark.asyncio
    async def test_allows_zero_fill_rate_no_data(self, mock_settings, mock_store):
        """Should allow when fill rate is 0 (no data yet)."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.0)

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is False


class TestPauseState:
    """Test pause state management."""

    @pytest.mark.asyncio
    async def test_remains_paused_until_expiry(self, mock_settings, mock_store):
        """Should remain paused until pause expires."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-1.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-2.50"))

        guard = RiskGuard(settings=mock_settings, store=mock_store)

        # First call triggers pause
        result1 = await guard.check()
        assert result1.blocked is True
        assert result1.reason == "hourly_loss"

        # Reset mock to "safe" values
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Second call still paused
        result2 = await guard.check()
        assert result2.blocked is True
        assert result2.reason == "paused"

    @pytest.mark.asyncio
    async def test_reset_clears_pause(self, mock_settings, mock_store):
        """Should clear pause on reset."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-1.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-2.50"))

        guard = RiskGuard(settings=mock_settings, store=mock_store)

        # Trigger pause
        await guard.check()

        # Reset
        guard.reset()

        # Reset mocks to safe values
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        # Now should be allowed
        result = await guard.check()
        assert result.blocked is False


class TestRiskCheckResult:
    """Test RiskCheckResult dataclass."""

    def test_blocked_result_has_reason(self):
        """Blocked result should have a reason."""
        result = RiskCheckResult(blocked=True, reason="daily_loss_cap")
        assert result.blocked is True
        assert result.reason == "daily_loss_cap"

    def test_allowed_result(self):
        """Allowed result should have blocked=False."""
        result = RiskCheckResult(blocked=False)
        assert result.blocked is False
        assert result.reason is None
        assert result.pause_minutes is None

    def test_pause_result_has_timing(self):
        """Pause result should have timing info."""
        resume_at = datetime.now(UTC) + timedelta(minutes=30)
        result = RiskCheckResult(blocked=True, reason="hourly_loss", pause_minutes=30, resume_at=resume_at)
        assert result.pause_minutes == 30
        assert result.resume_at == resume_at
