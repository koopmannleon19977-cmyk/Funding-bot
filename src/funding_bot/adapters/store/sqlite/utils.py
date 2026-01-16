"""
Shared helpers for SQLite store modules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from funding_bot.domain.models import ExecutionAttemptStatus


def _maybe_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _safe_attempt_status(value: Any) -> ExecutionAttemptStatus:
    try:
        return ExecutionAttemptStatus(str(value))
    except Exception:
        return ExecutionAttemptStatus.STARTED
