from decimal import Decimal

import pytest

from config.loader import load_settings


def test_load_settings_from_yaml(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "development")
    settings = load_settings()
    assert settings.trading.min_apy_filter == Decimal("0.20")
    assert settings.logging.level == "DEBUG"
    monkeypatch.delenv("BOT_ENV", raising=False)


def test_env_override(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "development")
    monkeypatch.setenv("BOT_TRADING__MIN_APY_FILTER", "0.50")
    settings = load_settings()
    assert settings.trading.min_apy_filter == Decimal("0.50")
    monkeypatch.delenv("BOT_ENV", raising=False)
    monkeypatch.delenv("BOT_TRADING__MIN_APY_FILTER", raising=False)


def test_missing_env_file(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "nonexistent")
    with pytest.raises(FileNotFoundError):
        load_settings()
    monkeypatch.delenv("BOT_ENV", raising=False)
