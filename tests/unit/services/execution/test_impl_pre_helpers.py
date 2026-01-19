"""
Unit tests for execution_impl_pre.py helper functions.

Tests the refactored dataclasses and helper functions extracted in Phase 2.5:
- Spread check configuration
- Smart pricing configuration
- Preflight liquidity configuration
- KPI data building

OFFLINE-FIRST: These tests do NOT require exchange SDK or network access.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from funding_bot.services.execution_impl_pre import (
    SmartPricingConfig,
    SpreadCheckConfig,
    SpreadCheckResult,
    _build_spread_kpi_data,
    _load_spread_check_config,
)

# =============================================================================
# DATACLASS TESTS
# =============================================================================


class TestSmartPricingConfig:
    """Tests for SmartPricingConfig dataclass."""

    def test_default_values(self):
        """SmartPricingConfig should have sensible defaults."""
        config = SmartPricingConfig()
        assert config.enabled is False
        assert config.max_price_impact_pct == Decimal("0.5")
        assert config.depth_levels == 10

    def test_enabled_config(self):
        """SmartPricingConfig should accept custom values."""
        config = SmartPricingConfig(
            enabled=True,
            max_price_impact_pct=Decimal("0.002"),
            depth_levels=50,
        )
        assert config.enabled is True
        assert config.max_price_impact_pct == Decimal("0.002")
        assert config.depth_levels == 50


class TestSpreadCheckConfig:
    """Tests for SpreadCheckConfig dataclass."""

    def test_default_values(self):
        """SpreadCheckConfig should have sensible defaults."""
        config = SpreadCheckConfig()
        assert config.max_spread == Decimal("0.01")
        assert config.negative_guard_multiple == Decimal("2.0")
        assert config.use_impact is False
        assert isinstance(config.smart_pricing, SmartPricingConfig)

    def test_custom_values(self):
        """SpreadCheckConfig should accept custom values."""
        smart = SmartPricingConfig(enabled=True, depth_levels=20)
        config = SpreadCheckConfig(
            max_spread=Decimal("0.005"),
            smart_pricing=smart,
            negative_guard_multiple=Decimal("3.0"),
            use_impact=True,
            depth_levels=30,
        )
        assert config.max_spread == Decimal("0.005")
        assert config.smart_pricing.enabled is True
        assert config.negative_guard_multiple == Decimal("3.0")
        assert config.use_impact is True
        assert config.depth_levels == 30


class TestSpreadCheckResult:
    """Tests for SpreadCheckResult dataclass."""

    def test_basic_result(self):
        """SpreadCheckResult should store spread data."""
        result = SpreadCheckResult(
            entry_spread=Decimal("0.001"),
            spread_cost=Decimal("0.0005"),
        )
        assert result.entry_spread == Decimal("0.001")
        assert result.spread_cost == Decimal("0.0005")
        assert result.depth_ob is None
        assert result.used_smart_pricing is False

    def test_with_smart_pricing(self):
        """SpreadCheckResult should track smart pricing usage."""
        result = SpreadCheckResult(
            entry_spread=Decimal("0.0008"),
            spread_cost=Decimal("0.0004"),
            used_smart_pricing=True,
        )
        assert result.used_smart_pricing is True


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================


class TestLoadSpreadCheckConfig:
    """Tests for _load_spread_check_config helper."""

    def _make_mock_settings(
        self,
        max_spread_filter: Decimal = Decimal("0.005"),
        max_spread_cap: Decimal = Decimal("0.01"),
        depth_gate_enabled: bool = False,
        smart_pricing_enabled: bool = False,
        smart_pricing_levels: int = 10,
        negative_guard_multiple: Decimal | None = None,
    ) -> MagicMock:
        """Create mock settings object."""
        settings = MagicMock()

        # Trading settings
        settings.trading.max_spread_filter_percent = max_spread_filter
        settings.trading.max_spread_cap_percent = max_spread_cap
        settings.trading.depth_gate_enabled = depth_gate_enabled
        settings.trading.depth_gate_mode = "L1"
        settings.trading.depth_gate_levels = 20
        settings.trading.depth_gate_max_price_impact_percent = Decimal("0.0015")

        if negative_guard_multiple is not None:
            settings.trading.negative_spread_guard_multiple = negative_guard_multiple
        else:
            del settings.trading.negative_spread_guard_multiple

        # Execution settings
        settings.execution.smart_pricing_enabled = smart_pricing_enabled
        settings.execution.smart_pricing_depth_levels = smart_pricing_levels
        if smart_pricing_enabled:
            settings.execution.smart_pricing_max_price_impact_percent = Decimal("0.002")
        else:
            del settings.execution.smart_pricing_max_price_impact_percent

        return settings

    def test_loads_basic_config(self):
        """Should load basic spread configuration."""
        settings = self._make_mock_settings()
        opp_apy = Decimal("50")  # 50% APY

        config = _load_spread_check_config(settings, opp_apy)

        # Max spread should be calculated dynamically
        assert config.max_spread > Decimal("0")
        assert config.smart_pricing.enabled is False

    def test_loads_smart_pricing_when_enabled(self):
        """Should load smart pricing config when enabled."""
        settings = self._make_mock_settings(
            smart_pricing_enabled=True,
            smart_pricing_levels=30,
        )
        opp_apy = Decimal("50")

        config = _load_spread_check_config(settings, opp_apy)

        assert config.smart_pricing.enabled is True
        assert config.smart_pricing.depth_levels == 30

    def test_clamps_negative_guard_multiple(self):
        """Negative guard multiple should be clamped between 1.0 and 10.0."""
        # Test minimum
        settings = self._make_mock_settings(negative_guard_multiple=Decimal("0.5"))
        config = _load_spread_check_config(settings, Decimal("50"))
        assert config.negative_guard_multiple >= Decimal("1.0")

        # Test maximum
        settings = self._make_mock_settings(negative_guard_multiple=Decimal("20"))
        config = _load_spread_check_config(settings, Decimal("50"))
        assert config.negative_guard_multiple <= Decimal("10.0")

    def test_defaults_negative_guard_multiple(self):
        """Negative guard multiple should default to 2.0."""
        settings = self._make_mock_settings()
        config = _load_spread_check_config(settings, Decimal("50"))
        assert config.negative_guard_multiple == Decimal("2.0")


class TestBuildSpreadKpiData:
    """Tests for _build_spread_kpi_data helper."""

    def _make_mock_opportunity(
        self,
        apy: Decimal = Decimal("50"),
        ev_usd: Decimal = Decimal("1.5"),
        breakeven_hours: Decimal = Decimal("4"),
        spread_pct: Decimal = Decimal("0.001"),
    ) -> MagicMock:
        """Create mock Opportunity object."""
        opp = MagicMock()
        opp.apy = apy
        opp.expected_value_usd = ev_usd
        opp.breakeven_hours = breakeven_hours
        opp.spread_pct = spread_pct
        return opp

    def _make_mock_orderbook(self) -> MagicMock:
        """Create mock OrderbookSnapshot."""
        ob = MagicMock()
        ob.lighter_bid = Decimal("100")
        ob.lighter_ask = Decimal("100.10")
        ob.x10_bid = Decimal("100.05")
        ob.x10_ask = Decimal("100.15")
        return ob

    def test_builds_basic_kpi_data(self):
        """Should build basic KPI data structure."""
        opp = self._make_mock_opportunity()
        ob = self._make_mock_orderbook()
        smart_config = SmartPricingConfig()

        kpi = _build_spread_kpi_data(
            opp=opp,
            fresh_ob=ob,
            long_exchange="lighter",
            entry_spread=Decimal("0.001"),
            spread_cost=Decimal("0.0005"),
            max_spread=Decimal("0.005"),
            smart_config=smart_config,
            used_depth=False,
        )

        assert kpi["stage"] == "SPREAD_CHECK"
        assert kpi["apy"] == Decimal("50")
        assert kpi["entry_spread_pct"] == Decimal("0.001")
        assert kpi["max_spread_pct"] == Decimal("0.005")
        assert kpi["data"]["long_exchange"] == "lighter"

    def test_includes_smart_pricing_info(self):
        """Should include smart pricing info when enabled."""
        opp = self._make_mock_opportunity()
        ob = self._make_mock_orderbook()
        smart_config = SmartPricingConfig(
            enabled=True,
            depth_levels=20,
            max_price_impact_pct=Decimal("0.002"),
        )

        kpi = _build_spread_kpi_data(
            opp=opp,
            fresh_ob=ob,
            long_exchange="x10",
            entry_spread=Decimal("0.0008"),
            spread_cost=Decimal("0.0004"),
            max_spread=Decimal("0.005"),
            smart_config=smart_config,
            used_depth=True,
        )

        assert kpi["data"]["smart_pricing"]["enabled"] is True
        assert kpi["data"]["smart_pricing"]["used_depth"] is True
        assert kpi["data"]["smart_pricing"]["levels"] == 20

    def test_includes_orderbook_prices(self):
        """Should include orderbook price data."""
        opp = self._make_mock_opportunity()
        ob = self._make_mock_orderbook()
        smart_config = SmartPricingConfig()

        kpi = _build_spread_kpi_data(
            opp=opp,
            fresh_ob=ob,
            long_exchange="lighter",
            entry_spread=Decimal("0.001"),
            spread_cost=Decimal("0.0005"),
            max_spread=Decimal("0.005"),
            smart_config=smart_config,
            used_depth=False,
        )

        assert kpi["data"]["lighter_bid"] == "100"
        assert kpi["data"]["lighter_ask"] == "100.10"
        assert kpi["data"]["x10_bid"] == "100.05"
        assert kpi["data"]["x10_ask"] == "100.15"

    def test_includes_opportunity_metrics(self):
        """Should include opportunity metrics."""
        opp = self._make_mock_opportunity(
            apy=Decimal("75"),
            ev_usd=Decimal("2.5"),
            breakeven_hours=Decimal("3"),
        )
        ob = self._make_mock_orderbook()
        smart_config = SmartPricingConfig()

        kpi = _build_spread_kpi_data(
            opp=opp,
            fresh_ob=ob,
            long_exchange="lighter",
            entry_spread=Decimal("0.001"),
            spread_cost=Decimal("0.0005"),
            max_spread=Decimal("0.005"),
            smart_config=smart_config,
            used_depth=False,
        )

        assert kpi["apy"] == Decimal("75")
        assert kpi["expected_value_usd"] == Decimal("2.5")
        assert kpi["breakeven_hours"] == Decimal("3")
