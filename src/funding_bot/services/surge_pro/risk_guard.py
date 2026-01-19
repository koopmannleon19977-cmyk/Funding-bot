"""
Risk Guard for Surge Pro strategy.

Monitors trading activity and pauses on anomalies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.config.settings import SurgeProSettings

logger = get_logger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    blocked: bool
    reason: str | None = None
    pause_minutes: int | None = None
    resume_at: datetime | None = None


class RiskGuard:
    """Monitors and guards against excessive risk."""

    def __init__(self, *, settings: SurgeProSettings, store: Any):
        self.settings = settings
        self.store = store
        self._paused_until: datetime | None = None
        self._pause_count: int = 0

    async def check(self) -> RiskCheckResult:
        """Check all risk conditions."""
        now = datetime.now(UTC)

        # Check if currently paused
        if self._paused_until and now < self._paused_until:
            remaining = (self._paused_until - now).total_seconds() / 60
            return RiskCheckResult(
                blocked=True,
                reason="paused",
                pause_minutes=int(remaining),
                resume_at=self._paused_until,
            )

        # Clear pause if expired
        if self._paused_until and now >= self._paused_until:
            self._paused_until = None

        # Check daily loss cap (hard limit)
        daily_pnl = await self.store.get_daily_pnl()
        if daily_pnl <= -self.settings.daily_loss_cap_usd:
            return RiskCheckResult(
                blocked=True,
                reason="daily_loss_cap",
            )

        # Check hourly loss (soft limit - pause)
        hourly_pnl = await self.store.get_hourly_pnl()
        if hourly_pnl <= -self.settings.hourly_loss_pause_usd:
            self._trigger_pause("hourly_loss")
            return RiskCheckResult(
                blocked=True,
                reason="hourly_loss",
                pause_minutes=self.settings.pause_duration_minutes,
                resume_at=self._paused_until,
            )

        # Check loss streak
        recent_trades = await self.store.get_recent_trades(count=self.settings.loss_streak_pause_count)
        if len(recent_trades) >= self.settings.loss_streak_pause_count:
            all_losses = all(t.pnl < 0 for t in recent_trades)
            if all_losses:
                self._trigger_pause("loss_streak")
                return RiskCheckResult(
                    blocked=True,
                    reason="loss_streak",
                    pause_minutes=self.settings.pause_duration_minutes,
                    resume_at=self._paused_until,
                )

        # Check fill rate
        fill_rate = await self.store.get_hourly_fill_rate()
        min_rate = self.settings.min_fill_rate_percent / 100
        if fill_rate < min_rate and fill_rate > 0:  # 0 means no data
            pause_mins = self.settings.pause_duration_minutes // 2
            self._trigger_pause("low_fill_rate", duration_minutes=pause_mins)
            return RiskCheckResult(
                blocked=True,
                reason="low_fill_rate",
                pause_minutes=pause_mins,
                resume_at=self._paused_until,
            )

        return RiskCheckResult(blocked=False)

    def _trigger_pause(self, reason: str, duration_minutes: int | None = None) -> None:
        """Trigger a pause."""
        duration = duration_minutes or self.settings.pause_duration_minutes
        self._paused_until = datetime.now(UTC) + timedelta(minutes=duration)
        self._pause_count += 1

        logger.warning(
            "RiskGuard pause triggered: reason=%s duration=%dm count=%d",
            reason,
            duration,
            self._pause_count,
        )

    def reset(self) -> None:
        """Reset pause state (for testing/manual intervention)."""
        self._paused_until = None
        self._pause_count = 0
