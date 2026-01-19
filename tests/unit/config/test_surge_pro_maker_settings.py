"""Tests for Surge Pro maker mode settings."""

from funding_bot.config.settings import SurgeProSettings


def test_maker_mode_default():
    """Maker mode should default to 'taker' for backwards compatibility."""
    settings = SurgeProSettings()
    assert settings.order_mode == "taker"


def test_maker_mode_can_be_set():
    """Maker mode can be set to 'maker'."""
    settings = SurgeProSettings(order_mode="maker")
    assert settings.order_mode == "maker"


def test_maker_entry_timeout_default():
    """Entry fill timeout should have sensible default."""
    settings = SurgeProSettings()
    assert settings.maker_entry_timeout_s == 2.0


def test_maker_exit_timeout_default():
    """Exit fill timeout should have sensible default."""
    settings = SurgeProSettings()
    assert settings.maker_exit_timeout_s == 1.5


def test_maker_exit_max_retries_default():
    """Max exit retries before taker fallback."""
    settings = SurgeProSettings()
    assert settings.maker_exit_max_retries == 3


def test_symbols_whitelist():
    """Symbols whitelist for liquid markets."""
    settings = SurgeProSettings(symbols=["BTC", "ETH", "SOL"])
    assert settings.symbols == ["BTC", "ETH", "SOL"]
