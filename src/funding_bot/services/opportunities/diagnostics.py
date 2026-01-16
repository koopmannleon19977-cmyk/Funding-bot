"""
Logging and diagnostics helpers for opportunity scanning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import Opportunity
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


def _scan_signature(self, opp: Opportunity) -> tuple[str, str, str]:
    apy = opp.apy.quantize(Decimal("0.0001"))
    ev = opp.expected_value_usd.quantize(Decimal("0.01"))
    return opp.symbol, str(apy), str(ev)


async def _reserve_scan_log(self, top: Opportunity | None) -> bool:
    interval = float(
        getattr(self.settings.trading, "opportunity_scan_log_interval_seconds", 30.0) or 30.0
    )
    if interval <= 0:
        return True
    now = datetime.now(UTC)
    signature = self._scan_signature(top) if top else None
    async with self._scan_log_lock:
        if self._last_scan_log_at is None:
            self._last_scan_log_at = now
            self._last_scan_signature = signature
            return True
        if (now - self._last_scan_log_at).total_seconds() >= interval:
            self._last_scan_log_at = now
            self._last_scan_signature = signature
            return True
        if signature and signature != self._last_scan_signature:
            self._last_scan_log_at = now
            self._last_scan_signature = signature
            return True
    return False


async def _reserve_candidate_log(self, symbol: str, *, suppress: bool = False) -> bool:
    if suppress:
        return False
    interval = float(
        getattr(self.settings.trading, "candidate_log_interval_seconds", 20.0) or 20.0
    )
    if interval <= 0:
        return True
    now = datetime.now(UTC)
    async with self._candidate_log_lock:
        last = self._last_candidate_log_at.get(symbol)
        if last is None or (now - last).total_seconds() >= interval:
            self._last_candidate_log_at[symbol] = now
            return True
    return False


async def _log_zero_scan_diagnostics(
    self,
    *,
    open_symbols: set[str],
    available_equity: Decimal,
) -> None:
    """
    When scan() finds 0 opportunities, log a compact reason summary.

    This runs at most once per minute and only samples a handful of symbols to avoid spam.
    """
    ts = self.settings.trading
    symbols = list(self.market_data.get_common_symbols())

    # Pick top symbols by cached APY (fast) to explain "why nothing passes"
    scored: list[tuple[Decimal, str]] = []
    for symbol in symbols:
        if symbol in open_symbols:
            continue
        funding = self.market_data.get_funding(symbol)
        if not funding:
            continue
        scored.append((funding.apy, symbol))

    scored.sort(reverse=True, key=lambda x: x[0])
    sample = [s for _, s in scored[:5]]

    # If even funding isn't available yet, just log the baseline.
    if not sample:
        logger.info(
            "No opportunities found (no funding cache yet). "
            f"Available=${available_equity:.2f} "
            f"min_apy_filter={ts.min_apy_filter:.2%} "
            f"min_expected_profit_entry_usd=${ts.min_expected_profit_entry_usd}"
        )
        return

    reasons: list[str] = []
    for symbol in sample:
        try:
            _, reject = await self._evaluate_symbol_with_reject_info(
                symbol,
                available_equity=available_equity,
                include_reject_info=True,
            )
            reason = reject.get("reason", "unknown")
            # Keep details short
            details_keys = (
                "filter_reason",
                "ev_total",
                "min_ev_usd",
                "breakeven_hours",
                "max_breakeven_hours",
                "required",
                "spread_cost",
            )
            details = ", ".join(
                f"{k}={reject[k]}" for k in details_keys if k in reject
            )
            reasons.append(f"{symbol}={reason}" + (f" ({details})" if details else ""))
        except Exception as e:
            reasons.append(f"{symbol}=error({e})")

    logger.info(
        f"[SCAN] No opportunities. Best: {sample[0] if sample else 'None'} - {reasons[0] if reasons else 'No candidates'} | "
        f"Available: ${available_equity:.0f}"
    )
    logger.debug(
        "Scan details: "
        f"min_apy={ts.min_apy_filter:.1%} "
        f"min_ev=${ts.min_expected_profit_entry_usd} "
        f"sample_rejects=[{'; '.join(reasons)}]"
    )
