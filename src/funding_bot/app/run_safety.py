"""
Run-safety helpers for PAPER vs LIVE mode.

This module exists to avoid ambiguous/unsafe combinations like:
- live_trading=true + testing_mode=true
- live_trading=true without explicit confirmation

The goal is to make "paper mode" the default safe path and make "live mode"
an explicit, confirmed action.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from funding_bot.config.settings import Settings

RunMode = Literal["paper", "live"]


@dataclass(frozen=True)
class RunSafetyDecision:
    settings: Settings
    mode: RunMode
    live_confirmed: bool
    errors: list[str]
    warnings: list[str]


def _env_is_yes(var_name: str) -> bool:
    value = os.getenv(var_name, "").strip().upper()
    return value in {"YES", "I_UNDERSTAND"}


def apply_run_safety(
    settings: Settings,
    *,
    mode_override: RunMode | None,
    confirm_live_flag: bool,
    allow_testing_live_flag: bool,
) -> RunSafetyDecision:
    updated = settings.model_copy(deep=True)

    if mode_override is not None:
        updated.live_trading = mode_override == "live"

    mode: RunMode = "live" if updated.live_trading else "paper"
    errors: list[str] = []
    warnings: list[str] = []

    allow_testing_live = allow_testing_live_flag or _env_is_yes("BOT_ALLOW_TESTING_LIVE")
    live_confirmed = False

    if mode == "live":
        live_confirmed = confirm_live_flag or _env_is_yes("BOT_CONFIRM_LIVE")
        if not live_confirmed:
            errors.append(
                "LIVE mode requested but not confirmed. Use --confirm-live or set BOT_CONFIRM_LIVE=YES."
            )

        if updated.testing_mode and not allow_testing_live:
            errors.append(
                "testing_mode=true is blocked in LIVE mode. Disable it or use --allow-testing-live "
                "(or set BOT_ALLOW_TESTING_LIVE=YES)."
            )

        # SECURITY VALIDATION: Check required credentials for live trading
        validation_errors = updated.validate_for_live_trading()
        if validation_errors:
            errors.append("Live trading requires proper configuration:")
            for error in validation_errors:
                errors.append(f"  - {error}")

    if mode == "paper" and confirm_live_flag:
        warnings.append("--confirm-live was provided but bot is in PAPER mode; ignoring.")

    if updated.testing_mode and mode == "paper":
        warnings.append("testing_mode=true enabled (extra diagnostics).")

    return RunSafetyDecision(
        settings=updated,
        mode=mode,
        live_confirmed=live_confirmed,
        errors=errors,
        warnings=warnings,
    )

