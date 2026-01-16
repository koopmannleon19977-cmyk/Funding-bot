"""
Execution helper utilities.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.models import Side


def _safe_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    try:
        text = str(value).strip().lower()
    except Exception:
        return default
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def _slippage_bps(*, side: Side, fill_price: Decimal, reference_price: Decimal) -> Decimal:
    if reference_price <= 0 or fill_price <= 0:
        return Decimal("0")
    ratio = fill_price / reference_price
    if side == Side.BUY:
        return (ratio - Decimal("1")) * Decimal("10000")
    return (Decimal("1") - ratio) * Decimal("10000")


def _compute_attempt_timeouts(
    *,
    total_seconds: float,
    attempts: int,
    min_per_attempt_seconds: float,
    schedule: str,
) -> list[float]:
    if attempts <= 1:
        return [max(total_seconds, min_per_attempt_seconds)]

    schedule_norm = (schedule or "equal").strip().lower()
    total_seconds = max(total_seconds, attempts * min_per_attempt_seconds)

    if schedule_norm == "increasing":
        # Allocate more time to later attempts (1..N weights).
        weights = list(range(1, attempts + 1))
        denom = float(sum(weights))
        return [max(min_per_attempt_seconds, total_seconds * (w / denom)) for w in weights]

    # Default: equal distribution
    per = max(min_per_attempt_seconds, total_seconds / attempts)
    return [per for _ in range(attempts)]
