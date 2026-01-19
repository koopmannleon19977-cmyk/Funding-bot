from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.config.settings import Settings
from funding_bot.domain.events import TradeClosed
from funding_bot.services.notification import NotificationService


@pytest.mark.asyncio
async def test_trade_closed_notification_uses_net_pnl_for_emoji_and_headline():
    settings = Settings(env="test", telegram={"enabled": True})
    notifier = MagicMock()
    notifier.send_message = AsyncMock()
    svc = NotificationService(settings=settings, event_bus=MagicMock(), notifier=notifier)

    event = TradeClosed(
        symbol="ETH",
        reason="test",
        realized_pnl=Decimal("-1.00"),
        funding_collected=Decimal("2.00"),
        total_fees=Decimal("0.50"),
        hold_duration_seconds=3600.0,
    )

    await svc._on_trade_closed(event)

    msg = notifier.send_message.call_args.args[0]
    assert "Net PnL: <b>$1.00</b>" in msg
    assert "Realized (after fees): $-1.00" in msg
    assert msg.startswith("💰")
