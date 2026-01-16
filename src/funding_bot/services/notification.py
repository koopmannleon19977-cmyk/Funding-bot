"""
Notification Service.

Listens to domain events and sends notifications.
"""

from __future__ import annotations

import json

from funding_bot.config.settings import Settings
from funding_bot.domain.events import (
    AlertEvent,
    CircuitBreakerTripped,
    DomainEvent,
    PositionReconciled,
    RollbackCompleted,
    TradeClosed,
    TradeOpened,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.notification import NotificationPort

logger = get_logger(__name__)


class NotificationService:
    """Handles event-driven notifications."""

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBusPort,
        notifier: NotificationPort,
    ):
        self.settings = settings
        self.event_bus = event_bus
        self.notifier = notifier
        self._running = False
        self._handlers = {
            TradeOpened: self._on_trade_opened,
            TradeClosed: self._on_trade_closed,
            RollbackCompleted: self._on_rollback_completed,
            PositionReconciled: self._on_position_reconciled,
            CircuitBreakerTripped: self._on_circuit_breaker,
            AlertEvent: self._on_alert,
        }

    async def start(self) -> None:
        """Start listening to events."""
        if self._running:
            return

        # Start notifier (e.g. login to API)
        if hasattr(self.notifier, "start"):
            await self.notifier.start()

        # subscribe to events
        for event_type in self._handlers:
            self.event_bus.subscribe(event_type, self._handle_event)

        self._running = True
        logger.info("NotificationService started")

        # Send startup message
        if self.settings.telegram.enabled:
            await self._send("🚀 <b>Funding Bot Started</b>\nEnv: " + self.settings.env)

    async def stop(self) -> None:
        """Stop service."""
        if not self._running:
            return

        if self.settings.telegram.enabled:
            await self._send("🛑 <b>Funding Bot Stopped</b>")

        if hasattr(self.notifier, "stop"):
            await self.notifier.stop()

        self._running = False
        logger.info("NotificationService stopped")

    async def _handle_event(self, event: DomainEvent) -> None:
        """Dispatch event specific handler."""
        handler = self._handlers.get(type(event))
        if handler:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"Error handling notification for {event.event_type}: {e}")

    async def _send(self, message: str) -> None:
        """Send message via notifier."""
        await self.notifier.send_message(message)

    @staticmethod
    def _format_details(details: dict) -> str:
        if not details:
            return ""
        try:
            text = json.dumps(
                details,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=str,
            )
        except Exception:
            text = str(details)
        # Telegram hard limit is 4096 chars; keep headroom for header text.
        if len(text) > 3200:
            return text[:3200] + "\n..."
        return text

    async def _on_trade_opened(self, event: TradeOpened) -> None:
        """Handle TradeOpened."""
        emoji = "🟢"
        msg = (
            f"{emoji} <b>Trade Opened: {event.symbol}</b>\n"
            f"APY: <b>{event.entry_apy:.1%}</b>\n"
            f"Size: ${event.notional_usd:.0f}\n"
            f"Lighter: {event.leg1_price:.4f}\n"
            f"X10: {event.leg2_price:.4f}"
        )
        await self._send(msg)

    async def _on_trade_closed(self, event: TradeClosed) -> None:
        """Handle TradeClosed."""
        net_pnl = event.realized_pnl + event.funding_collected
        emoji = "💰" if net_pnl >= 0 else "🔻"

        msg = (
            f"{emoji} <b>Trade Closed: {event.symbol}</b>\n"
            f"Reason: {event.reason}\n"
            f"Net PnL: <b>${net_pnl:.2f}</b>\n"
            f"Realized (after fees): ${event.realized_pnl:.2f}\n"
            f"Funding: ${event.funding_collected:.2f}\n"
            f"Fees (included): -${event.total_fees:.2f}\n"
            f"Duration: {event.hold_duration_seconds / 3600:.1f}h"
        )
        await self._send(msg)

    async def _on_rollback_completed(self, event: RollbackCompleted) -> None:
        """Handle RollbackCompleted."""
        if event.success:
            msg = (
                f"⚠️ <b>Rollback Handled: {event.symbol}</b>\n"
                f"Loss: ${event.loss_usd:.2f}\n"
                f"Trade failed but hedged successfully."
            )
        else:
            msg = (
                f"🚨 <b>Rollback FAILED: {event.symbol}</b>\n"
                f"CRITICAL: Check positions manually!"
            )
        await self._send(msg)

    async def _on_position_reconciled(self, event: PositionReconciled) -> None:
        """Handle PositionReconciled."""
        if event.action == "ignored":
            return

        emoji = "👻" if "ghost" in event.action else "🧟"
        msg = (
            f"{emoji} <b>Reconciliation: {event.symbol}</b>\n"
            f"Action: <b>{event.action}</b>"
        )
        details_text = self._format_details(event.details or {})
        if details_text:
            msg += f"\n<pre>{details_text}</pre>"
        await self._send(msg)

    async def _on_circuit_breaker(self, event: CircuitBreakerTripped) -> None:
        """Handle CircuitBreakerTripped."""
        msg = (
            f"⛔ <b>Circuit Breaker Tripped!</b>\n"
            f"Reason: {event.reason}\n"
            f"Failures: {event.consecutive_failures}\n"
            f"Cooldown: {event.cooldown_seconds}s"
        )
        await self._send(msg)

    async def _on_alert(self, event: AlertEvent) -> None:
        """Handle generic Alert."""
        emoji_map = {"WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🚨"}
        emoji = emoji_map.get(event.level, "ℹ️")

        msg = f"{emoji} <b>{event.level}: {event.message}</b>"
        if event.details:
            msg += f"\n<pre>{self._format_details(event.details)}</pre>"
        await self._send(msg)
