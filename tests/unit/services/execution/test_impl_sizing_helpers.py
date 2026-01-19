"""
Unit tests for execution_impl_sizing helper functions.

Tests the refactored dataclasses and helper functions extracted in Phase 2.5.
These are pure functions and dataclasses that don't require exchange connections.

OFFLINE-FIRST: These tests do NOT require exchange SDK or network access.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from funding_bot.services.execution_impl_sizing import (
    BalanceData,
    DepthGateConfig,
    LeverageConfig,
    RiskCapacity,
    SizingResult,
    SlotAllocation,
    _calculate_leverage,
    _calculate_slot_allocation,
    _load_depth_gate_config,
)

# =============================================================================
# DATACLASS TESTS
# =============================================================================


class TestBalanceData:
    """Tests for BalanceData dataclass."""

    def test_default_values(self):
        """BalanceData should have sensible defaults."""
        balance = BalanceData()
        assert balance.lighter_total == Decimal("0")
        assert balance.lighter_available == Decimal("0")
        assert balance.x10_total == Decimal("0")
        assert balance.x10_available == Decimal("0")

    def test_total_equity_calculation(self):
        """total_equity property should sum both exchange totals."""
        balance = BalanceData(
            lighter_total=Decimal("1000"),
            x10_total=Decimal("500"),
        )
        assert balance.total_equity == Decimal("1500")

    def test_total_equity_with_zero(self):
        """total_equity should handle zero values."""
        balance = BalanceData(
            lighter_total=Decimal("0"),
            x10_total=Decimal("1000"),
        )
        assert balance.total_equity == Decimal("1000")


class TestLeverageConfig:
    """Tests for LeverageConfig dataclass."""

    def test_default_values(self):
        """LeverageConfig should have sensible defaults."""
        config = LeverageConfig()
        assert config.lighter_imr_limit == Decimal("3")
        assert config.x10_imr_limit == Decimal("5")
        assert config.eff_lighter == Decimal("3")
        assert config.eff_x10 == Decimal("5")

    def test_custom_leverage(self):
        """LeverageConfig should accept custom values."""
        config = LeverageConfig(
            lighter_imr_limit=Decimal("10"),
            x10_imr_limit=Decimal("20"),
            eff_lighter=Decimal("5"),
            eff_x10=Decimal("10"),
        )
        assert config.lighter_imr_limit == Decimal("10")
        assert config.eff_lighter == Decimal("5")


class TestRiskCapacity:
    """Tests for RiskCapacity dataclass."""

    def test_default_values(self):
        """RiskCapacity should have zero defaults."""
        risk = RiskCapacity()
        assert risk.global_risk_cap == Decimal("0")
        assert risk.current_exposure == Decimal("0")
        assert risk.remaining_risk_cap == Decimal("0")

    def test_custom_values(self):
        """RiskCapacity should accept custom values."""
        risk = RiskCapacity(
            global_risk_cap=Decimal("10000"),
            current_exposure=Decimal("2000"),
            remaining_risk_cap=Decimal("8000"),
            max_lighter_pos=Decimal("5000"),
            max_x10_pos=Decimal("4000"),
            liquidity_cap=Decimal("4000"),
            available_exposure=Decimal("4000"),
        )
        assert risk.remaining_risk_cap == Decimal("8000")
        assert risk.available_exposure == Decimal("4000")


class TestSlotAllocation:
    """Tests for SlotAllocation dataclass."""

    def test_default_values(self):
        """SlotAllocation should have sensible defaults."""
        slot = SlotAllocation()
        assert slot.remaining_slots == 1
        assert slot.slot_based_size == Decimal("0")

    def test_custom_allocation(self):
        """SlotAllocation should accept custom values."""
        slot = SlotAllocation(
            remaining_slots=3,
            slot_based_size=Decimal("1000"),
            risk_based_cap=Decimal("5000"),
            target_notional=Decimal("1000"),
        )
        assert slot.remaining_slots == 3
        assert slot.target_notional == Decimal("1000")


class TestDepthGateConfig:
    """Tests for DepthGateConfig dataclass."""

    def test_default_values(self):
        """DepthGateConfig should have sensible defaults."""
        config = DepthGateConfig()
        assert config.enabled is False
        assert config.mode == "L1"
        assert config.levels == 20
        assert config.max_price_impact_pct == Decimal("0.0015")

    def test_impact_mode(self):
        """DepthGateConfig should support IMPACT mode."""
        config = DepthGateConfig(
            enabled=True,
            mode="IMPACT",
            levels=50,
            max_price_impact_pct=Decimal("0.002"),
        )
        assert config.mode == "IMPACT"
        assert config.levels == 50


class TestSizingResult:
    """Tests for SizingResult dataclass."""

    def test_default_values(self):
        """SizingResult should have sensible defaults."""
        result = SizingResult()
        assert result.target_notional == Decimal("0")
        assert result.target_qty == Decimal("0")
        assert result.rejected is False
        assert result.reject_reason is None

    def test_rejected_result(self):
        """SizingResult should track rejection info."""
        result = SizingResult(
            rejected=True,
            reject_reason="Insufficient liquidity",
            reject_stage="depth_gate",
        )
        assert result.rejected is True
        assert "liquidity" in result.reject_reason.lower()


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================


class TestCalculateLeverage:
    """Tests for _calculate_leverage helper."""

    def _make_mock_trade(self, symbol: str = "BTC") -> MagicMock:
        """Create a mock Trade object."""
        trade = MagicMock()
        trade.symbol = symbol
        return trade

    def _make_mock_settings(
        self,
        leverage: Decimal = Decimal("3"),
        x10_leverage: Decimal | None = None,
    ) -> MagicMock:
        """Create mock Settings object."""
        settings = MagicMock()
        settings.trading.leverage_multiplier = leverage
        if x10_leverage is not None:
            settings.trading.x10_leverage_multiplier = x10_leverage
        else:
            # Simulate missing attribute
            del settings.trading.x10_leverage_multiplier
        return settings

    def _make_mock_market_data(
        self,
        lighter_max_lev: Decimal = Decimal("10"),
        x10_max_lev: Decimal = Decimal("20"),
    ) -> MagicMock:
        """Create mock MarketDataService."""
        market_data = MagicMock()

        lighter_info = MagicMock()
        lighter_info.lighter_max_leverage = lighter_max_lev

        x10_info = MagicMock()
        x10_info.x10_max_leverage = x10_max_lev

        def get_market_info(symbol, exchange):
            from funding_bot.domain.models import Exchange

            if exchange == Exchange.LIGHTER:
                return lighter_info
            return x10_info

        market_data.get_market_info = get_market_info
        return market_data

    def test_leverage_capped_by_exchange_limit(self):
        """User leverage should be capped by exchange IMR limit."""
        trade = self._make_mock_trade()
        settings = self._make_mock_settings(leverage=Decimal("15"))  # User wants 15x
        market_data = self._make_mock_market_data(
            lighter_max_lev=Decimal("10"),  # Exchange allows max 10x
            x10_max_lev=Decimal("20"),
        )

        result = _calculate_leverage(trade, settings, market_data)

        # Effective leverage should be capped at exchange limit
        assert result.eff_lighter == Decimal("10")
        assert result.lighter_user_lev == Decimal("15")
        assert result.lighter_imr_limit == Decimal("10")

    def test_leverage_uses_user_when_below_limit(self):
        """User leverage should be used when below exchange limit."""
        trade = self._make_mock_trade()
        settings = self._make_mock_settings(leverage=Decimal("3"))
        market_data = self._make_mock_market_data(
            lighter_max_lev=Decimal("10"),
            x10_max_lev=Decimal("20"),
        )

        result = _calculate_leverage(trade, settings, market_data)

        assert result.eff_lighter == Decimal("3")
        assert result.eff_x10 == Decimal("3")  # Falls back to lighter leverage

    def test_separate_x10_leverage(self):
        """X10 leverage can be configured separately."""
        trade = self._make_mock_trade()
        settings = self._make_mock_settings(
            leverage=Decimal("3"),
            x10_leverage=Decimal("5"),
        )
        market_data = self._make_mock_market_data()

        result = _calculate_leverage(trade, settings, market_data)

        assert result.eff_lighter == Decimal("3")
        assert result.eff_x10 == Decimal("5")


class TestCalculateSlotAllocation:
    """Tests for _calculate_slot_allocation helper."""

    def _make_mock_settings(
        self,
        max_open_trades: int = 5,
        max_trade_size_pct: Decimal = Decimal("50"),
        desired_notional_usd: Decimal | None = None,
    ) -> MagicMock:
        """Create mock Settings object."""
        settings = MagicMock()
        settings.trading.max_open_trades = max_open_trades
        settings.risk.max_trade_size_pct = max_trade_size_pct
        if desired_notional_usd is not None:
            settings.trading.desired_notional_usd = desired_notional_usd
        else:
            del settings.trading.desired_notional_usd
        return settings

    def _make_mock_opportunity(self, suggested_notional: Decimal = Decimal("10000")) -> MagicMock:
        """Create mock Opportunity object."""
        opp = MagicMock()
        opp.suggested_notional = suggested_notional
        return opp

    def test_slot_allocation_with_no_other_trades(self):
        """Full capacity available with no other trades."""
        risk = RiskCapacity(
            global_risk_cap=Decimal("10000"),
            available_exposure=Decimal("10000"),
        )
        settings = self._make_mock_settings(max_open_trades=5)
        opp = self._make_mock_opportunity(suggested_notional=Decimal("50000"))

        result = _calculate_slot_allocation(risk, settings, opp, num_other_trades=0)

        assert result.remaining_slots == 5
        assert result.slot_based_size == Decimal("2000")  # 10000 / 5

    def test_slot_allocation_with_existing_trades(self):
        """Remaining slots reduced by existing trades."""
        risk = RiskCapacity(
            global_risk_cap=Decimal("10000"),
            available_exposure=Decimal("6000"),  # 4000 already used
        )
        settings = self._make_mock_settings(max_open_trades=5)
        opp = self._make_mock_opportunity(suggested_notional=Decimal("50000"))

        result = _calculate_slot_allocation(risk, settings, opp, num_other_trades=2)

        assert result.remaining_slots == 3  # 5 - 2
        assert result.slot_based_size == Decimal("2000")  # 6000 / 3

    def test_allocation_capped_by_risk_limit(self):
        """Target notional capped by risk-based limit."""
        risk = RiskCapacity(
            global_risk_cap=Decimal("10000"),
            available_exposure=Decimal("10000"),
        )
        settings = self._make_mock_settings(
            max_open_trades=1,  # Only 1 trade allowed
            max_trade_size_pct=Decimal("30"),  # Max 30% per trade
        )
        opp = self._make_mock_opportunity(suggested_notional=Decimal("50000"))

        result = _calculate_slot_allocation(risk, settings, opp, num_other_trades=0)

        # Risk cap = 10000 * 30% = 3000
        assert result.risk_based_cap == Decimal("3000")
        assert result.target_notional == Decimal("3000")

    def test_allocation_capped_by_opportunity_suggestion(self):
        """Target notional capped by opportunity's suggested size."""
        risk = RiskCapacity(
            global_risk_cap=Decimal("100000"),
            available_exposure=Decimal("100000"),
        )
        settings = self._make_mock_settings(max_open_trades=1)
        opp = self._make_mock_opportunity(suggested_notional=Decimal("5000"))  # Small opp

        result = _calculate_slot_allocation(risk, settings, opp, num_other_trades=0)

        # Should be capped at opportunity suggestion
        assert result.target_notional == Decimal("5000")


class TestLoadDepthGateConfig:
    """Tests for _load_depth_gate_config helper."""

    def _make_mock_settings(
        self,
        depth_gate_enabled: bool = False,
        depth_gate_mode: str = "L1",
        depth_gate_levels: int = 20,
        depth_gate_max_price_impact_percent: Decimal | None = None,
        hedge_depth_preflight_enabled: bool = False,
        hedge_depth_preflight_multiplier: Decimal | None = None,
    ) -> MagicMock:
        """Create mock Settings object."""
        settings = MagicMock()

        # Trading settings
        settings.trading.depth_gate_enabled = depth_gate_enabled
        settings.trading.depth_gate_mode = depth_gate_mode
        settings.trading.depth_gate_levels = depth_gate_levels
        if depth_gate_max_price_impact_percent is not None:
            settings.trading.depth_gate_max_price_impact_percent = depth_gate_max_price_impact_percent
        else:
            del settings.trading.depth_gate_max_price_impact_percent

        # Set defaults for min_l1 settings
        settings.trading.min_l1_notional_usd = None
        settings.trading.min_l1_notional_multiple = None
        settings.trading.max_l1_qty_utilization = None

        # Execution settings
        settings.execution.hedge_depth_preflight_enabled = hedge_depth_preflight_enabled
        if hedge_depth_preflight_multiplier is not None:
            settings.execution.hedge_depth_preflight_multiplier = hedge_depth_preflight_multiplier
        else:
            del settings.execution.hedge_depth_preflight_multiplier

        return settings

    def test_disabled_by_default(self):
        """Depth gate should be disabled by default."""
        settings = self._make_mock_settings()
        config = _load_depth_gate_config(settings)

        assert config.enabled is False
        assert config.mode == "L1"

    def test_l1_mode_configuration(self):
        """L1 mode configuration should be loaded correctly."""
        settings = self._make_mock_settings(
            depth_gate_enabled=True,
            depth_gate_mode="L1",
            depth_gate_levels=30,
        )
        config = _load_depth_gate_config(settings)

        assert config.enabled is True
        assert config.mode == "L1"
        assert config.levels == 30

    def test_impact_mode_configuration(self):
        """IMPACT mode configuration should be loaded correctly."""
        settings = self._make_mock_settings(
            depth_gate_enabled=True,
            depth_gate_mode="IMPACT",
            depth_gate_max_price_impact_percent=Decimal("0.002"),
        )
        config = _load_depth_gate_config(settings)

        assert config.mode == "IMPACT"
        assert config.max_price_impact_pct == Decimal("0.002")

    def test_strict_preflight_multiplier(self):
        """Strict preflight should apply multiplier to config values."""
        settings = self._make_mock_settings(
            depth_gate_enabled=True,
            hedge_depth_preflight_enabled=True,
            hedge_depth_preflight_multiplier=Decimal("2.0"),
        )
        # Set base values
        settings.trading.min_l1_notional_usd = Decimal("1000")
        settings.trading.min_l1_notional_multiple = Decimal("2")

        config = _load_depth_gate_config(settings)

        assert config.strict_preflight is True
        assert config.multiplier == Decimal("2.0")
        # Min USD should be doubled
        assert config.min_l1_notional_usd == Decimal("2000")
        assert config.min_l1_notional_multiple == Decimal("4")

    def test_multiplier_minimum_is_one(self):
        """Multiplier should be at least 1.0."""
        settings = self._make_mock_settings(
            hedge_depth_preflight_enabled=True,
            hedge_depth_preflight_multiplier=Decimal("0.5"),  # Invalid
        )
        config = _load_depth_gate_config(settings)

        assert config.multiplier == Decimal("1.0")
