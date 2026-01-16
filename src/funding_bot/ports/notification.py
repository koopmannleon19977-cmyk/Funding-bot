"""
Notification Port.

Defines the interface for sending user notifications.
"""

from __future__ import annotations

from typing import Protocol


class NotificationPort(Protocol):
    """Interface for notification adapters."""

    async def send_message(self, message: str) -> bool:
        """
        Send a message to the user.

        Args:
            message: The message text to send.

        Returns:
            True if sent successfully, False otherwise.
        """
        ...
