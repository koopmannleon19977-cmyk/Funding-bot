from __future__ import annotations

from funding_bot.app.run_safety import apply_run_safety
from funding_bot.config.settings import Settings


def test_live_mode_requires_confirmation(monkeypatch):
    monkeypatch.delenv("BOT_CONFIRM_LIVE", raising=False)
    settings = Settings(live_trading=True, testing_mode=False)

    decision = apply_run_safety(
        settings,
        mode_override=None,
        confirm_live_flag=False,
        allow_testing_live_flag=False,
    )

    assert decision.mode == "live"
    assert decision.errors


def test_live_mode_confirm_flag_allows_start(monkeypatch):
    monkeypatch.delenv("BOT_CONFIRM_LIVE", raising=False)
    settings = Settings(live_trading=True, testing_mode=False)

    decision = apply_run_safety(
        settings,
        mode_override=None,
        confirm_live_flag=True,
        allow_testing_live_flag=False,
    )

    assert decision.mode == "live"
    assert decision.live_confirmed is True
    assert decision.errors == []


def test_live_mode_confirm_env_allows_start(monkeypatch):
    monkeypatch.setenv("BOT_CONFIRM_LIVE", "YES")
    settings = Settings(live_trading=True, testing_mode=False)

    decision = apply_run_safety(
        settings,
        mode_override=None,
        confirm_live_flag=False,
        allow_testing_live_flag=False,
    )

    assert decision.mode == "live"
    assert decision.live_confirmed is True
    assert decision.errors == []


def test_testing_mode_blocked_in_live_without_allow(monkeypatch):
    monkeypatch.setenv("BOT_CONFIRM_LIVE", "YES")
    monkeypatch.delenv("BOT_ALLOW_TESTING_LIVE", raising=False)
    settings = Settings(live_trading=True, testing_mode=True)

    decision = apply_run_safety(
        settings,
        mode_override=None,
        confirm_live_flag=False,
        allow_testing_live_flag=False,
    )

    assert decision.mode == "live"
    assert decision.errors


def test_testing_mode_allowed_in_live_with_allow_flag(monkeypatch):
    monkeypatch.setenv("BOT_CONFIRM_LIVE", "YES")
    monkeypatch.delenv("BOT_ALLOW_TESTING_LIVE", raising=False)
    settings = Settings(live_trading=True, testing_mode=True)

    decision = apply_run_safety(
        settings,
        mode_override=None,
        confirm_live_flag=False,
        allow_testing_live_flag=True,
    )

    assert decision.mode == "live"
    assert decision.errors == []

