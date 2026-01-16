"""
Unit tests for Settings validation (offline-only).

Tests for validate_for_live_trading() guard ensuring funding_rate_interval_hours == 1.
Critical for preventing 8x EV/APY errors from misconfiguration.

Ref: docs/FUNDING_CADENCE_RESEARCH.md
"""

from decimal import Decimal

import pytest

from funding_bot.config.settings import ExchangeSettings, Settings


class TestExchangeSettingsFundingIntervalValidation:
    """Test funding_rate_interval_hours validation in ExchangeSettings.validate_for_live_trading()."""

    def test_validate_funding_interval_must_be_1_for_live_trading(self):
        """
        REGRESSION: Test that funding_rate_interval_hours=1 is enforced for live trading.

        GIVEN: ExchangeSettings with funding_rate_interval_hours=8 (WRONG)
        WHEN: validate_for_live_trading() is called
        THEN: returns error about interval must be 1

        This prevents the 8x EV/APY bug where rates would be divided by 8 incorrectly.
        """
        settings = ExchangeSettings(
            api_key="test_key",
            private_key="0x" + "1" * 64,
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("8"),  # WRONG!
        )

        errors = settings.validate_for_live_trading("TestExchange")

        # Should have error about interval_hours
        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) > 0, "Expected error about funding_rate_interval_hours=8"
        assert "must be 1" in interval_errors[0]
        assert "8" in interval_errors[0]

    def test_validate_funding_interval_2_rejects(self):
        """
        REGRESSION: Test that funding_rate_interval_hours=2 is also rejected.

        GIVEN: ExchangeSettings with funding_rate_interval_hours=2
        WHEN: validate_for_live_trading() is called
        THEN: returns error (only interval=1 is valid)

        This ensures the guard rejects ANY value other than 1.
        """
        settings = ExchangeSettings(
            api_key="test_key",
            private_key="0x" + "1" * 64,
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("2"),  # WRONG!
        )

        errors = settings.validate_for_live_trading("TestExchange")

        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) > 0
        assert "must be 1" in interval_errors[0]

    def test_validate_funding_interval_1_passes(self):
        """
        VALID: Test that funding_rate_interval_hours=1 passes validation.

        GIVEN: ExchangeSettings with funding_rate_interval_hours=1 (CORRECT)
        WHEN: validate_for_live_trading() is called
        THEN: NO errors about interval_hours

        This is the expected/correct configuration.
        """
        settings = ExchangeSettings(
            api_key="test_key",
            private_key="0x" + "1" * 64,
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("1"),  # CORRECT
        )

        errors = settings.validate_for_live_trading("TestExchange")

        # Should NOT have errors about interval_hours
        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) == 0

    def test_validate_funding_interval_0_5_rejects(self):
        """
        REGRESSION: Test that sub-hourly intervals are also rejected.

        GIVEN: ExchangeSettings with funding_rate_interval_hours=0.5
        WHEN: validate_for_live_trading() is called
        THEN: returns error (only interval=1 is valid)

        Both exchanges apply funding hourly, so 0.5 would also cause errors.
        """
        settings = ExchangeSettings(
            api_key="test_key",
            private_key="0x" + "1" * 64,
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("0.5"),  # WRONG!
        )

        errors = settings.validate_for_live_trading("TestExchange")

        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) > 0

    def test_validate_error_message_includes_docs_reference(self):
        """
        DOCUMENTATION: Test that error message references research doc.

        GIVEN: ExchangeSettings with funding_rate_interval_hours=8
        WHEN: validate_for_live_trading() returns errors
        THEN: error message includes reference to FUNDING_CADENCE_RESEARCH.md

        This helps operators understand WHY the validation exists.
        """
        settings = ExchangeSettings(
            api_key="test_key",
            private_key="0x" + "1" * 64,
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("8"),
        )

        errors = settings.validate_for_live_trading("TestExchange")

        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) > 0
        assert "FUNDING_CADENCE_RESEARCH.md" in interval_errors[0]

    def test_validate_interval_checked_alongside_credentials(self):
        """
        INTEGRATION: Test that interval validation runs alongside credential checks.

        GIVEN: ExchangeSettings with missing api_key AND interval_hours=8
        WHEN: validate_for_live_trading() is called
        THEN: returns BOTH credential error AND interval error

        This ensures the guard doesn't bypass other validations.
        """
        settings = ExchangeSettings(
            api_key="",  # MISSING!
            private_key="",
            base_url="https://test.com",
            funding_rate_interval_hours=Decimal("8"),  # WRONG!
        )

        errors = settings.validate_for_live_trading("TestExchange")

        # Should have BOTH api_key error AND interval_hours error
        api_errors = [e for e in errors if "api_key" in e]
        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]

        assert len(api_errors) > 0, "Expected error about missing api_key"
        assert len(interval_errors) > 0, "Expected error about interval_hours"


class TestSettingsIntegration:
    """Integration tests for Settings.validate_for_live_trading()."""

    def test_settings_validate_calls_exchange_validate(self):
        """
        INTEGRATION: Test that Settings.validate_for_live_trading() calls Exchange validation.

        GIVEN: Settings with lighter.funding_rate_interval_hours=8
        WHEN: settings.validate_for_live_trading() is called
        THEN: returns error from Lighter ExchangeSettings validation

        This ensures the Settings-level validation properly delegates to exchange-level.
        """
        settings_dict = {
            "lighter": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("8"),  # WRONG!
            },
            "x10": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("1"),  # CORRECT
            },
            "live_trading": True,
            "database": {"path": "test.db"},
        }

        settings = Settings(**settings_dict)
        errors = settings.validate_for_live_trading()

        # Should have error about Lighter interval=8
        lighter_errors = [e for e in errors if "Lighter" in e and "funding_rate_interval_hours" in e]
        assert len(lighter_errors) > 0

    def test_settings_validate_both_exchanges_interval_1_passes(self):
        """
        VALID: Test that Settings with both exchanges interval=1 passes validation.

        GIVEN: Settings with lighter.x10 interval_hours=1 (both CORRECT)
        WHEN: settings.validate_for_live_trading() is called
        THEN: NO errors about interval_hours (may have other errors)

        This is the expected production configuration.
        """
        settings_dict = {
            "lighter": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("1"),  # CORRECT
            },
            "x10": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("1"),  # CORRECT
            },
            "live_trading": True,
            "database": {"path": "test.db"},
        }

        settings = Settings(**settings_dict)
        errors = settings.validate_for_live_trading()

        # Should NOT have errors about interval_hours
        interval_errors = [e for e in errors if "funding_rate_interval_hours" in e]
        assert len(interval_errors) == 0

    def test_settings_validate_both_exchanges_interval_8_fails(self):
        """
        REGRESSION: Test that Settings with BOTH exchanges interval=8 fails validation.

        GIVEN: Settings with lighter.x10 interval_hours=8 (both WRONG)
        WHEN: settings.validate_for_live_trading() is called
        THEN: returns TWO errors (one per exchange)

        This ensures both exchanges are validated independently.
        """
        settings_dict = {
            "lighter": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("8"),  # WRONG!
            },
            "x10": {
                "api_key": "test_key",
                "private_key": "0x" + "1" * 64,
                "base_url": "https://test.com",
                "funding_rate_interval_hours": Decimal("8"),  # WRONG!
            },
            "live_trading": True,
            "database": {"path": "test.db"},
        }

        settings = Settings(**settings_dict)
        errors = settings.validate_for_live_trading()

        # Should have TWO errors (Lighter + X10)
        lighter_errors = [e for e in errors if "Lighter" in e and "funding_rate_interval_hours" in e]
        x10_errors = [e for e in errors if "X10" in e and "funding_rate_interval_hours" in e]

        assert len(lighter_errors) > 0, "Expected error for Lighter interval=8"
        assert len(x10_errors) > 0, "Expected error for X10 interval=8"
