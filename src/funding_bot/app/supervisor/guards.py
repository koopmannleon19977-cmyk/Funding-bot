"""
Trading pause guards and account safety checks.

These functions are designed to be used as methods of the Supervisor class.
They are defined externally and assigned to the class in manager.py.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.domain.events import AlertEvent, BrokenHedgeDetected, CircuitBreakerTripped
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.app.supervisor.manager import Supervisor

logger = get_logger(__name__)


async def _pause_trading(
    self: Supervisor,
    reason: str,
    cooldown_seconds: float,
    *,
    failures: int = 0,
    severity: str = "WARNING",
    indefinite: bool = False,
) -> None:
    """
    Pause NEW trades (position management keeps running).

    If `indefinite=True`, trading stays paused until restart/manual intervention.
    """
    async with self._risk_lock:
        # Avoid spamming alerts by repeatedly extending a pause for the same reason
        # while we're already in a paused window.
        if (
            not indefinite
            and self._trading_paused_until is not None
            and self._trading_paused_until != float("inf")
            and self._trading_pause_reason == reason
            and time.time() < self._trading_paused_until
        ):
            return

        until = float("inf") if indefinite else (time.time() + cooldown_seconds)
        should_update = (
            self._trading_paused_until is None
            or until > self._trading_paused_until
            or (indefinite and self._trading_paused_until != float("inf"))
        )
        if not should_update:
            return

        self._trading_paused_until = until
        self._trading_pause_reason = reason

    if self.event_bus:
        await self.event_bus.publish(
            CircuitBreakerTripped(
                reason=reason,
                consecutive_failures=failures,
                cooldown_seconds=cooldown_seconds,
            )
        )
        await self.event_bus.publish(
            AlertEvent(
                level=severity,
                message=f"Trading paused: {reason}",
                details={
                    "cooldown_seconds": cooldown_seconds,
                    "indefinite": indefinite,
                    "consecutive_failures": failures,
                },
            )
        )

    logger.warning(
        f"Trading paused: {reason} (cooldown={cooldown_seconds}s, indefinite={indefinite})"
    )


def _compact_error(err: Exception, *, max_chars: int = 240) -> str:
    """
    Reduce noisy multi-line HTTP/SDK errors to a stable 1-liner.

    This keeps logs/alerts readable and prevents huge payloads being sent to Telegram.
    """
    text = str(err).strip()
    first = text.splitlines()[0] if text else repr(err)
    if len(first) > max_chars:
        return first[:max_chars] + "..."
    return first


async def _on_broken_hedge(self: Supervisor, event: BrokenHedgeDetected) -> None:
    """
    Handle BrokenHedgeDetected event.

    Triggers a TIMED global trading pause to prevent further exposure.
    After cooldown, self-healing logic verifies positions before resuming.
    """
    logger.critical(
        f"BROKEN HEDGE KILL-SWITCH TRIGGERED for {event.symbol}: "
        f"missing {event.missing_exchange.value}, remaining_qty={event.remaining_qty}"
    )

    # Track which symbol caused the broken hedge for verification during resume
    self._broken_hedge_symbol = event.symbol

    # Get cooldown from config (default 15 min)
    cooldown_seconds = float(
        getattr(self.settings.risk, "broken_hedge_cooldown_seconds", 900)
    )

    await self._pause_trading(
        reason=f"BROKEN HEDGE: {event.symbol} missing {event.missing_exchange.value}",
        cooldown_seconds=cooldown_seconds,
        failures=0,
        severity="CRITICAL",
        indefinite=False,  # Use timed cooldown with self-healing
    )

    # Log instruction for what will happen
    logger.critical(
        f"TRADING PAUSED FOR {cooldown_seconds}s. After cooldown:\n"
        "  1. Bot will verify all positions are closed/hedged\n"
        "  2. If clean ƒÅ' trading resumes automatically\n"
        "  3. If not clean ƒÅ' pause extended\n"
        "  (Set risk.broken_hedge_cooldown_seconds in config.yaml to adjust)"
    )


async def _fetch_balances_with_retry(
    self: Supervisor,
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.5,
) -> tuple[Any, Any]:
    if not self.lighter or not self.x10:
        raise RuntimeError("Adapters not initialized")

    last_err: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            l_bal = await self.lighter.get_available_balance()
            x_bal = await self.x10.get_available_balance()
            return l_bal, x_bal
        except Exception as e:
            last_err = e
            if i >= attempts - 1:
                break
            await asyncio.sleep(base_delay_seconds * (2**i))

    assert last_err is not None
    raise last_err


async def _maybe_resume_trading(self: Supervisor) -> None:
    async with self._risk_lock:
        if self._trading_paused_until is None:
            return
        if self._trading_paused_until == float("inf"):
            return
        if time.time() < self._trading_paused_until:
            return

        # --- SELF-HEALING: Verify positions before resuming after broken hedge ---
        if self._broken_hedge_symbol and self.lighter and self.x10:
            symbol = self._broken_hedge_symbol
            try:
                lighter_pos = await self.lighter.get_position(symbol)
                x10_pos = await self.x10.get_position(symbol)

                qty_threshold = Decimal("0.0001")
                has_lighter = bool(lighter_pos and lighter_pos.qty > qty_threshold)
                has_x10 = bool(x10_pos and x10_pos.qty > qty_threshold)

                # Both must be balanced: either both present or both absent
                if has_lighter != has_x10:
                    # Still unbalanced! Extend the pause.
                    cooldown_seconds = float(
                        getattr(self.settings.risk, "broken_hedge_cooldown_seconds", 900)
                    )
                    self._trading_paused_until = time.time() + cooldown_seconds
                    logger.warning(
                        f"Self-Healing: Positions still unbalanced for {symbol} "
                        f"(lighter={has_lighter}, x10={has_x10}). "
                        f"Extending pause for {cooldown_seconds}s."
                    )
                    if self.event_bus:
                        await self.event_bus.publish(
                            AlertEvent(
                                level="WARNING",
                                message="Self-Healing: still unbalanced, extending pause",
                                details={
                                    "symbol": symbol,
                                    "lighter_has_position": has_lighter,
                                    "x10_has_position": has_x10,
                                    "next_check_in_seconds": cooldown_seconds,
                                },
                            )
                        )
                    return  # Don't resume yet
                else:
                    # Positions are balanced! Clear the broken hedge tracking.
                    logger.info(
                        f"Self-Healing: Positions balanced for {symbol}. Resuming trading."
                    )
                    self._broken_hedge_symbol = None

            except Exception as e:
                # Verification failed - extend pause to be safe
                cooldown_seconds = float(
                    getattr(self.settings.risk, "broken_hedge_cooldown_seconds", 900)
                )
                self._trading_paused_until = time.time() + cooldown_seconds
                logger.warning(
                    f"Self-Healing: Verification failed for {symbol}: {e}. "
                    f"Extending pause for {cooldown_seconds}s."
                )
                return  # Don't resume yet

        self._trading_paused_until = None
        self._trading_pause_reason = None
        self._consecutive_trade_failures = 0

    if self.event_bus:
        await self.event_bus.publish(
            AlertEvent(level="INFO", message="Trading resumed", details={})
        )
    logger.info("Trading resumed")


async def _is_trading_paused(self: Supervisor) -> bool:
    async with self._risk_lock:
        if self._trading_paused_until is None:
            return False
        if self._trading_paused_until == float("inf"):
            return True
        return time.time() < self._trading_paused_until


async def _check_account_guards(self: Supervisor, *, l_bal: Any | None = None, x_bal: Any | None = None) -> None:
    """
    Enforce drawdown and free margin rules.

    This does NOT close positions; it only stops opening new ones.
    """
    if not self.lighter or not self.x10:
        return

    if l_bal is None or x_bal is None:
        try:
            l_bal, x_bal = await self._fetch_balances_with_retry()
        except Exception as e:
            async with self._risk_lock:
                failures = self._consecutive_trade_failures
            await self._pause_trading(
                reason=f"balance fetch failed: {self._compact_error(e)}",
                cooldown_seconds=60.0,
                failures=failures,
                severity="ERROR",
            )
            return

    total_equity = l_bal.total + x_bal.total
    total_available = l_bal.available + x_bal.available

    free_margin_pct = total_available / total_equity if total_equity > 0 else Decimal("0")

    async with self._risk_lock:
        self._last_total_equity = total_equity
        self._last_free_margin_pct = free_margin_pct
        if self._start_equity is None and total_equity > 0:
            self._start_equity = total_equity
        if self._peak_equity is None:
            self._peak_equity = total_equity
        if total_equity > (self._peak_equity or Decimal("0")):
            self._peak_equity = total_equity

    # Free margin guard (stop opening new trades when too tight)
    min_free = self.settings.risk.min_free_margin_pct
    if total_equity > 0 and free_margin_pct < min_free:
        async with self._risk_lock:
            failures = self._consecutive_trade_failures
        await self._pause_trading(
            reason=f"free margin too low: {free_margin_pct:.2%} < {min_free:.2%}",
            cooldown_seconds=120.0,
            failures=failures,
            severity="WARNING",
        )

    # Drawdown guard (more severe: manual intervention / restart recommended)
    peak = self._peak_equity or total_equity
    drawdown_pct = (peak - total_equity) / peak if peak > 0 else Decimal("0")

    max_dd = self.settings.risk.max_drawdown_pct
    if peak > 0 and drawdown_pct >= max_dd:
        async with self._risk_lock:
            failures = self._consecutive_trade_failures
        await self._pause_trading(
            reason=f"max drawdown exceeded: {drawdown_pct:.2%} >= {max_dd:.2%}",
            cooldown_seconds=0.0,
            failures=failures,
            severity="CRITICAL",
            indefinite=True,
        )
