"""
Execution result types.
"""

from __future__ import annotations

from dataclasses import dataclass

from funding_bot.domain.models import Trade


@dataclass
class ExecutionResult:
    """Result of trade execution."""

    success: bool
    trade: Trade | None = None
    error: str | None = None
