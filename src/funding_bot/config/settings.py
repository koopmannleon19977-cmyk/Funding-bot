"""
Settings management using Pydantic.

Loads configuration from YAML files and environment variables.
Environment variables override YAML values.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class ExchangeSettings(BaseModel):
    """Settings for a single exchange."""

    api_key: str = Field(default="", min_length=0, description="API key for exchange authentication")
    private_key: str = Field(default="", min_length=0, description="Private key for signing transactions")
    public_key: str = ""
    vault_id: str = ""
    account_index: int | None = 0  # None = disabled/not set, 0 = valid first/default account
    api_key_index: int = 3  # For Lighter: 3-254 for custom API keys
    l1_address: str = ""  # Ethereum L1 wallet address
    base_url: str = ""
    funding_rate_interval_hours: Decimal = Field(
        default=Decimal("1"),
        ge=Decimal("0.1"),
        description="Funding rate normalization interval in hours (default: 1 = hourly). "
                    "Both exchanges return HOURLY rates directly (API formula includes /8). "
                    "Set to 8 ONLY if API changes to return 8h rates in the future. "
                    "We divide raw rate by this value to normalize to hourly storage."
    )

    def validate_for_live_trading(self, exchange_name: str) -> list[str]:
        """
        Validate that required credentials are present for live trading.

        Returns a list of validation errors. Empty list means validation passed.
        """
        import os

        errors = []

        # Base URL is always required
        if not self.base_url or self.base_url == "":
            errors.append(f"{exchange_name}: base_url is required")

        # Check if credentials are in settings OR environment variables
        has_api_key = bool(self.api_key and self.api_key != "")
        has_private_key = bool(self.private_key and self.private_key != "")

        # Check environment variables as fallback
        if exchange_name == "Lighter":
            has_api_key = has_api_key or bool(os.getenv("LIGHTER_API_KEY_PRIVATE_KEY"))
            has_private_key = has_private_key or bool(
                os.getenv("LIGHTER_API_KEY_PRIVATE_KEY") or os.getenv("LIGHTER_PRIVATE_KEY")
            )
        elif exchange_name == "X10":
            has_api_key = has_api_key or bool(os.getenv("X10_API_KEY"))
            has_private_key = has_private_key or bool(os.getenv("X10_PRIVATE_KEY"))

        if not has_api_key:
            errors.append(f"{exchange_name}: api_key is required for live trading")

        if not has_private_key:
            errors.append(f"{exchange_name}: private_key is required for trading")

        # CRITICAL: Validate funding_rate_interval_hours == 1 (HOURLY)
        # Both exchanges return HOURLY rates directly from their APIs.
        # The /8 in the formula converts 8h premium to hourly, NOT the data.
        # Setting interval=8 would cause 8x EV/APY errors.
        # Ref: docs/FUNDING_CADENCE_RESEARCH.md
        if self.funding_rate_interval_hours != Decimal("1"):
            errors.append(
                f"{exchange_name}: funding_rate_interval_hours must be 1 (hourly). "
                f"Current value: {self.funding_rate_interval_hours}. "
                "Both exchanges return HOURLY rates per official documentation. "
                "Setting 8 would cause 8x EV/APY errors. "
                "See docs/FUNDING_CADENCE_RESEARCH.md for details."
            )

        return errors


class TradingSettings(BaseModel):
    """Core trading parameters."""

    # Position sizing
    desired_notional_usd: Decimal = Decimal("150.0")
    max_open_trades: int = 2
    leverage_multiplier: Decimal = Decimal("5.0")  # Unified for both exchanges
    cooldown_minutes: int = 60  # Cooldown after exit before re-entering same symbol

    # Profitability filters
    min_apy_filter: Decimal = Decimal("0.15")
    min_expected_profit_entry_usd: Decimal = Decimal("0.10")
    min_profit_exit_usd: Decimal = Decimal("0.25")
    min_ev_spread_ratio: Decimal = Decimal("2.0")  # EV must be > 2x estimated exit cost
    max_breakeven_hours: Decimal = Decimal("12.0")

    # Spread limits
    max_spread_filter_percent: Decimal = Decimal("0.001")
    max_spread_cap_percent: Decimal = Decimal("0.02")
    negative_spread_guard_multiple: Decimal = Decimal("2.0")

    # Liquidity / Depth gates
    # These gates help avoid illiquid "APY traps" where spread/fees/slippage eat the edge.
    # Gate is applied on fresh orderbook snapshots (final guard before execution).
    depth_gate_enabled: bool = False
    depth_gate_mode: str = "L1"  # "L1" (best-level only) or "IMPACT" (cumulative depth within price impact)
    depth_gate_levels: int = 20  # top-N levels per side when depth_gate_mode="IMPACT"
    depth_gate_max_price_impact_percent: Decimal = Decimal("0.0015")  # 0.15% window from best bid/ask
    min_l1_notional_usd: Decimal = Decimal("0")  # 0 disables absolute notional threshold
    min_l1_notional_multiple: Decimal = Decimal("0")  # 0 disables relative threshold (x target_notional)
    max_l1_qty_utilization: Decimal = Decimal("1.0")  # >=1 disables utilization threshold

    # Pre-flight liquidity checks (Phase 1: Rollback Prevention)
    # These checks run BEFORE executing Leg1 to prevent rollbacks caused by insufficient liquidity.
    # Validates both exchanges have sufficient liquidity using multi-level depth aggregation.
    preflight_liquidity_enabled: bool = True
    preflight_liquidity_depth_levels: int = 5  # Number of orderbook levels to aggregate
    preflight_liquidity_safety_factor: Decimal = Decimal("3.0")  # Require 3x available liquidity
    preflight_liquidity_max_spread_bps: Decimal = Decimal("50")  # Max acceptable spread (50 bps = 0.5%)
    preflight_liquidity_orderbook_cache_ttl_seconds: Decimal = Decimal("5.0")
    preflight_liquidity_fallback_to_l1: bool = True  # Use L1 if multi-level fails
    preflight_liquidity_min_liquidity_threshold: Decimal = Decimal("0.01")  # Minimum quantity to consider liquid

    # Hold times
    min_hold_seconds: int = 7200  # 2 hours
    max_hold_hours: Decimal = Decimal("72.0")

    # Exit conditions
    funding_flip_hours_threshold: Decimal = Decimal("4.0")
    volatility_panic_threshold: Decimal = Decimal("8.0")

    # Early take-profit (bypass min_hold ONLY if net PnL meets threshold)
    # Net PnL here means: estimated "if closed now" PnL after estimated taker fees and bid/ask valuation.
    early_take_profit_enabled: bool = False
    early_take_profit_net_usd: Decimal = Decimal("0")
    # Slippage buffer: effective_threshold = net_usd + (exit_cost * slippage_multiple)
    # Accounts for price movement during multi-attempt smart close (3+ attempts over 2+ minutes)
    early_take_profit_slippage_multiple: Decimal = Decimal("1.5")

    # Early-TP Fast-Close: Hybrid time-based escalation to prevent drift
    # When early-TP is triggered, use faster close strategy:
    # - Shorter maker timeout before escalating to taker
    # - Optional: Skip maker entirely and go direct to taker
    early_tp_fast_close_enabled: bool = True
    early_tp_fast_close_total_timeout_seconds: Decimal = Decimal("30.0")  # Max time for maker chase
    early_tp_fast_close_attempt_timeout_seconds: Decimal = Decimal("5.0")  # Per-attempt timeout
    early_tp_fast_close_max_attempts: int = 2  # Max maker attempts before taker
    early_tp_fast_close_use_taker_directly: bool = False  # Skip maker, go straight to taker

    # Early edge-exit (bypass min_hold on structural edge collapse)
    # This is NOT a profit-taker: it is a safety valve when the edge disappears quickly (APY crash / funding flip).
    early_edge_exit_enabled: bool = False
    early_edge_exit_min_age_seconds: int = 3600

    # Exit EV (netto) - reduce overtrading by exiting only when it makes sense after costs
    exit_ev_enabled: bool = False
    exit_ev_horizon_hours: Decimal = Decimal("12.0")
    exit_ev_exit_cost_multiple: Decimal = Decimal("1.2")  # require 20% buffer over estimated exit cost
    exit_ev_skip_profit_target_when_edge_good: bool = True
    exit_ev_skip_opportunity_cost_when_edge_good: bool = True

    # Z-Score Exit (statistical crash detection) - Phase 1 Holy Grail
    # ONLY for older trades as fallback (7+ days) - FundingVelocityExit preferred for new trades
    z_score_exit_enabled: bool = False
    z_score_exit_threshold: Decimal = Decimal("-2.0")  # Exit at 2 sigma crash
    z_score_emergency_threshold: Decimal = Decimal("-3.0")  # Emergency at 3 sigma
    z_score_lookback_hours: int = 24  # Hours of history for mean/std calculation
    z_score_exit_min_age_hours: int = 168  # Only trigger for trades 7+ days old (168 hours)

    # Yield vs. Cost Exit (unholdable position filter) - Phase 1 Holy Grail
    yield_cost_exit_enabled: bool = False
    yield_cost_max_hours: Decimal = Decimal("24.0")  # Max hours to cover exit cost

    # Basis Convergence Exit (spread convergence lock-in) - Phase 2 Holy Grail
    basis_convergence_exit_enabled: bool = False
    basis_convergence_threshold_pct: Decimal = Decimal("0.0005")  # 0.05% absolute threshold
    basis_convergence_ratio: Decimal = Decimal("0.20")  # 20% of entry = 80% collapse
    basis_convergence_min_profit_usd: Decimal = Decimal("0.50")  # Min profit to trigger

    # NEW EXIT STRATEGIES v3.1 LEAN (2026-01-10)
    # Layer 1: EMERGENCY EXITS (Override min_hold gate)
    liquidation_distance_monitoring_enabled: bool = False  # Requires adapter extension (liquidation_price)
    liquidation_distance_min_pct: Decimal = Decimal("0.10")  # 10% minimum buffer to liquidation
    liquidation_distance_check_interval_seconds: int = 15  # Check every 15s

    delta_bound_enabled: bool = True
    delta_bound_max_delta_pct: Decimal = Decimal("0.03")  # 3% max delta deviation
    delta_bound_check_interval_seconds: int = 15  # Check every 15s

    # Phase 3: DELTA REBALANCE (Restore neutrality without full exit)
    rebalance_enabled: bool = True  # Enable rebalance instead of full exit at 1-3% drift
    rebalance_min_delta_pct: Decimal = Decimal("0.01")  # 1% = log warning only
    rebalance_max_delta_pct: Decimal = Decimal("0.03")  # 3% = rebalance trigger (same as delta_bound)
    rebalance_maker_timeout_seconds: Decimal = Decimal("6.0")  # Maker timeout before IOC fallback
    rebalance_use_ioc_fallback: bool = True  # Escalate to IOC if maker doesn't fill

    # Phase 3: COORDINATED CLOSE (Dual-leg parallel close)
    coordinated_close_enabled: bool = True  # Enable coordinated close on both legs
    coordinated_close_maker_timeout_seconds: Decimal = Decimal("6.0")  # Timeout for maker orders
    coordinated_close_ioc_timeout_seconds: Decimal = Decimal("2.0")  # Timeout for IOC escalation
    coordinated_close_escalate_to_ioc: bool = True  # Auto-escalate unfilled legs to IOC
    coordinated_close_x10_use_maker: bool = True  # Use maker on X10 (not just IOC)

    # Layer 2: PROFIT TAKING (ATR-based trailing stop)
    atr_trailing_enabled: bool = True
    atr_period: int = 14  # ATR lookback period (14 bars)
    atr_multiplier: Decimal = Decimal("2.0")  # ATR multiplier for stop distance
    atr_min_activation_usd: Decimal = Decimal("3.0")  # Min profit to activate trailing
    atr_check_interval_seconds: int = 60  # Recalculate every 60s

    # Layer 3: STATISTICAL (Funding Velocity - proactive crash detection)
    funding_velocity_exit_enabled: bool = True
    velocity_threshold_hourly: Decimal = Decimal("-0.0015")  # -0.15% per hour
    acceleration_threshold: Decimal = Decimal("-0.0008")  # Negative acceleration threshold
    velocity_lookback_hours: int = 6  # Lookback for velocity calc

    # Opportunity cost rotation
    opportunity_cost_apy_diff: Decimal = Decimal("0.40")  # Exit if new opp has 40% higher APY
    opportunity_rotation_cooldown_minutes: int = 30  # Cooldown for rotation (separate from entry cooldown)

    # Phase 4: SCORING OPTIMIZATION (Weighted Exit Cost & Rotation)
    # Weighted exit cost model for more accurate EV calculation
    # Maker fill probabilities by exchange/account type (exchange-specific)
    maker_fill_probability: dict[str, str] = Field(
        default={
            "lighter_standard": "0.70",  # 70% for Lighter Standard
            "lighter_premium": "0.80",   # 80% for Lighter Premium (better liquidity)
            "x10": "0.50",               # 50% for X10 (less liquid)
        },
        description="Maker fill probability by exchange/account type. "
                    "Used for weighted exit cost calculation in EV model."
    )
    switching_cost_latency_usd: Decimal = Decimal("0.20")  # Latency penalty for rotation

    # Phase 4: VELOCITY FORECAST (Slope-based APY adjustment for entry scoring)
    velocity_forecast_enabled: bool = True  # Use funding velocity to adjust expected APY
    velocity_forecast_lookback_hours: int = 6  # Hours to look back for slope calculation
    velocity_forecast_weight: Decimal = Decimal("2.0")  # Multiplier for velocity adjustment

    # Fees (fallback)
    taker_fee_x10: Decimal = Decimal("0.000225")  # 0.0225%
    maker_fee_x10: Decimal = Decimal("0.0")  # 0.0%
    maker_fee_lighter: Decimal = Decimal("0.00002")  # 0.002% (Premium)
    taker_fee_lighter: Decimal = Decimal("0.0002")  # 0.02% (Premium)

    # Blacklist
    blacklist_symbols: set[str] = Field(
        default_factory=set  # Empty by default - all symbols allowed
    )

    # Opportunity scan cadence (Supervisor loop)
    opportunity_scan_interval_seconds: Decimal = Decimal("5.0")
    opportunity_scan_log_interval_seconds: Decimal = Decimal("30.0")
    candidate_log_interval_seconds: Decimal = Decimal("20.0")
    opportunity_scan_pause_during_execution_seconds: Decimal = Decimal("1.0")


class ExecutionSettings(BaseModel):
    """Order execution settings."""

    lead_exchange: str = "LIGHTER"  # LIGHTER or X10
    maker_order_timeout_seconds: Decimal = Decimal("45.0")
    maker_order_max_retries: int = 3
    maker_attempt_timeout_min_seconds: Decimal = Decimal("5.0")
    maker_attempt_timeout_schedule: str = "equal"  # "equal" or "increasing"
    maker_max_aggressiveness: Decimal = Decimal("0.5")  # 0..1 (fraction of spread, maker-only)
    maker_force_post_only: bool = False  # if true: never submit non-post-only maker orders
    parallel_execution_timeout: Decimal = Decimal("15.0")
    rollback_delay_seconds: Decimal = Decimal("3.0")
    taker_order_slippage: Decimal = Decimal("0.05")  # 5% slippage for hedge orders
    taker_close_slippage: Decimal = Decimal("0.01")  # 1% min slippage for reduce-only closes (Lighter MARKET emulation)

    # Maker -> Taker escalation (Lighter Leg1)
    # Goal: avoid multi-minute maker hangs that turn into hedge-depth failures.
    leg1_escalate_to_taker_enabled: bool = False
    leg1_escalate_to_taker_after_seconds: Decimal = Decimal("30.0")
    leg1_escalate_to_taker_slippage: Decimal = Decimal("0.0015")  # 0.15% slippage cap
    leg1_escalate_to_taker_fill_timeout_seconds: Decimal = Decimal("6.0")

    # Hedge IOC robustness (X10)
    # X10 "market" is emulated via aggressive LIMIT IOC in the SDK; these settings reduce churn/rollbacks
    # by retrying quickly with fresh L1 snapshots and slightly wider slippage caps.
    hedge_ioc_max_attempts: int = 3
    hedge_ioc_fill_timeout_seconds: Decimal = Decimal("8.0")
    hedge_ioc_retry_delay_seconds: Decimal = Decimal("0.2")
    hedge_ioc_slippage_step: Decimal = Decimal("0.0005")  # +0.05% per attempt
    hedge_ioc_max_slippage: Decimal = Decimal("0.003")  # cap at 0.30%

    # Dynamic hedge timing (adaptive retry delay)
    hedge_dynamic_timing_enabled: bool = True
    hedge_dynamic_timing_min_retry_delay_seconds: Decimal = Decimal("0.05")
    hedge_dynamic_timing_max_retry_delay_seconds: Decimal = Decimal("0.8")
    hedge_dynamic_timing_low_volatility_pct: Decimal = Decimal("0.0002")  # 2 bps
    hedge_dynamic_timing_high_volatility_pct: Decimal = Decimal("0.0010")  # 10 bps

    # Post-Leg1 hedge-depth salvage
    # If X10 L1 can't hedge 100% after Leg1 fills: hedge what's safe and flatten the remainder on Lighter.
    hedge_depth_salvage_enabled: bool = False
    hedge_depth_salvage_min_fraction: Decimal = Decimal("0.35")  # require at least 35% hedgeable
    hedge_depth_salvage_close_slippage: Decimal = Decimal("0.0020")  # 0.20% cap for flattening remainder
    hedge_depth_salvage_close_fill_timeout_seconds: Decimal = Decimal("6.0")

    # Microfill policy
    microfill_grace_seconds: Decimal = Decimal("8.0")
    microfill_max_wait_seconds: Decimal = Decimal("20.0")
    microfill_max_unhedged_usd: Decimal = Decimal("5.0")

    # Min-Qty bump policy
    # If exchange min_qty would force notional to exceed the sized target by too much,
    # reject the trade instead of bumping size and violating risk/sizing constraints.
    max_min_qty_bump_multiple: Decimal = Decimal("1.5")

    # Hedge depth preflight (avoid Leg1->rollback churn)
    hedge_depth_preflight_enabled: bool = True
    hedge_depth_preflight_multiplier: Decimal = Decimal("1.5")
    hedge_depth_preflight_checks: int = 2
    hedge_depth_preflight_delay_seconds: Decimal = Decimal("0.5")

    # Turbo mode: use WS order updates to wait for fills (fallback to polling automatically).
    ws_fill_wait_enabled: bool = True
    ws_fill_wait_fallback_poll_seconds: Decimal = Decimal("2.0")
    ws_ready_gate_enabled: bool = True
    ws_ready_gate_timeout_seconds: Decimal = Decimal("2.0")
    ws_ready_gate_cooldown_seconds: Decimal = Decimal("15.0")

    # Hedge fast-path: submit hedge immediately after Leg1 fill, without blocking on
    # slow post-Leg1 depth/impact preflights.
    hedge_submit_immediately_on_fill_enabled: bool = False
    hedge_post_leg1_impact_depth_enabled: bool = False

    # X10 close tuning (IOC limit retries before market fallback)
    x10_close_attempts: int = 3
    x10_close_slippage: Decimal = Decimal("0.005")
    x10_sdk_call_timeout_seconds: Decimal = Decimal("30.0")  # Increased from 8.0s to handle slow API responses
    x10_close_max_slippage: Decimal = Decimal("0.02")

    # Smart Pricing (depth-aware)
    # Uses depth-VWAP within a capped impact window when L1 is too thin.
    smart_pricing_enabled: bool = False
    smart_pricing_depth_levels: int = 20
    smart_pricing_max_price_impact_percent: Decimal = Decimal("0.0015")
    # Trigger when (target_qty / l1_qty) exceeds this threshold (1.0 => only when L1 qty < target qty).
    smart_pricing_l1_utilization_trigger: Decimal = Decimal("1.0")
    # For maker repricing: minimum aggressiveness when L1 is thin (0..1).
    smart_pricing_maker_aggressiveness_floor: Decimal = Decimal("0.25")

    # Grid Step Close (Dynamic Order Distribution)
    # Distributes close orders across multiple price levels to reduce order pollution
    # and increase maker-fill rate. Only for large positions with thin orderbooks.
    grid_step_enabled: bool = False
    grid_step_adaptive: bool = True
    grid_step_min_pct: Decimal = Decimal("0.0002")  # 0.02% minimal step (conservative)
    grid_step_max_pct: Decimal = Decimal("0.001")  # 0.1% maximal step (aggressive)
    grid_step_max_levels: int = 5
    grid_step_min_notional_usd: Decimal = Decimal("200.0")  # Min $200 position for grid
    grid_step_l1_depth_trigger: Decimal = Decimal("0.5")  # Grid when L1 < 50% of position
    grid_step_volatility_threshold_high: Decimal = Decimal("0.01")  # >1% vol = aggressive
    grid_step_volatility_threshold_low: Decimal = Decimal("0.005")  # 0.5-1% vol = balanced


class ReconciliationSettings(BaseModel):
    """Reconciliation safety settings."""

    soft_close_enabled: bool = True
    soft_close_timeout_seconds: Decimal = Decimal("15.0")
    soft_close_max_attempts: int = 2
    auto_import_ghosts: bool = False  # Import ghost positions into DB instead of closing them


class RiskSettings(BaseModel):
    """Risk management settings."""

    max_consecutive_failures: int = 5
    max_drawdown_pct: Decimal = Decimal("0.20")
    max_exposure_pct: Decimal = Decimal("10.0")
    max_trade_size_pct: Decimal = Decimal("100.0")
    min_free_margin_pct: Decimal = Decimal("0.05")

    # Broken Hedge Self-Healing
    # After detecting a broken hedge, pause trading for this duration.
    # After cooldown, verify all positions are balanced before resuming.
    broken_hedge_cooldown_seconds: Decimal = Decimal("900.0")  # 15 min default


class WebSocketSettings(BaseModel):
    """WebSocket connection settings."""

    ping_interval: Decimal = Decimal("15.0")
    ping_timeout: Decimal = Decimal("10.0")
    reconnect_delay_initial: Decimal = Decimal("2.0")
    reconnect_delay_max: Decimal = Decimal("120.0")
    health_check_interval: Decimal = Decimal("5.0")  # Faster market data polling (5s)

    # 🔧 WebSocket Stability Improvements (Circuit Breaker, Health Monitor)
    # Jitter factor for reconnect delays (prevents thundering herd when multiple WS fail)
    reconnect_jitter_factor: Decimal = Decimal("0.15")  # 15% jitter
    # Circuit breaker: stop reconnecting after N failures for cooldown period
    circuit_breaker_threshold: int = 10
    circuit_breaker_cooldown_seconds: Decimal = Decimal("60.0")
    # Health monitor: detect unhealthy connections (no messages, too many errors)
    health_check_message_threshold: int = 5  # Min messages before health check is meaningful
    health_check_error_threshold: int = 5  # Max errors before connection is unhealthy
    # Quick resync: attempt REST API snapshot on nonce gap (future enhancement)
    gap_recovery_quick_resync_enabled: bool = False  # Not yet implemented
    gap_recovery_quick_resync_timeout_seconds: Decimal = Decimal("5.0")

    # MarketData L1 robustness: retry and short fallback window for depth-valid orderbooks.
    orderbook_l1_retry_attempts: int = 3
    orderbook_l1_retry_delay_seconds: Decimal = Decimal("0.3")
    orderbook_l1_fallback_max_age_seconds: Decimal = Decimal("10.0")

    # Turbo mode: public orderbook streams to reduce REST polling.
    # NOTE: Keep disabled by default. Lighter can disconnect with
    # `Too Many Websocket Messages` when subscribing to many markets.
    # Prefer on-demand per-symbol WS in ExecutionEngine / PositionManager.
    market_data_streams_enabled: bool = False
    # Optional allowlist for MarketDataService to keep a few markets WS-hot (e.g. ["ETH", "BTC"]).
    market_data_streams_symbols: list[str] = Field(default_factory=list)
    # Safety cap so an accidental huge list can't spawn dozens of WS connections.
    market_data_streams_max_symbols: int = 6
    market_data_batch_refresh_interval_seconds: Decimal = Decimal("30.0")
    # 🚀 PERFORMANCE PREMIUM-OPTIMIZED: Market metadata cache TTL
    # Exchanges rarely change tick sizes, min qty, etc. Cache for longer to reduce API calls.
    # Premium accounts can afford larger caches and more aggressive caching.
    market_cache_ttl_seconds: Decimal = Decimal("3600.0")  # 1 hour (was: cache forever)
    # Provider-specific: X10 supports either full book (no depth param) or `depth=1` (best bid/ask).
    # We default to `1` for lowest latency + minimal bandwidth.
    orderbook_stream_depth: int = 1
    # X10 specific: subscribe to all markets (True) or single market (False)
    # IMPORTANT: ALL-markets orderbook stream is very high throughput and tends to disconnect.
    # Prefer per-market streams started on-demand for execution / active trades.
    x10_ws_orderbook_multi_market: bool = False

    # Lighter per-market orderbook WS caps to avoid connection/subscription limits.
    lighter_orderbook_ws_max_connections: int = 20
    lighter_orderbook_ws_ttl_seconds: Decimal = Decimal("120.0")

    # Lighter: order submission over WS (`jsonapi/sendtx`) for lower latency.
    lighter_ws_order_submission_enabled: bool = True
    lighter_ws_order_submission_connect_timeout_seconds: Decimal = Decimal("2.0")
    lighter_ws_order_submission_response_timeout_seconds: Decimal = Decimal("2.0")


class DatabaseSettings(BaseModel):
    """Database settings."""

    path: str = "data/funding.db"
    pool_size: int = 5
    write_batch_size: int = 50
    wal_mode: bool = True


class LoggingSettings(BaseModel):
    """Logging settings."""

    level: str = "INFO"
    json_enabled: bool = True
    json_file: str = "logs/funding_bot_json.jsonl"
    # Rotate JSONL log to prevent unbounded growth (disk + I/O).
    # Set to 0 to disable rotation.
    json_max_bytes: int = 50_000_000
    json_backup_count: int = 3


class TelegramSettings(BaseModel):
    """Telegram notification settings."""

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class HistoricalSettings(BaseModel):
    """Historical analysis settings for APY crash detection."""

    enabled: bool = False
    backfill_days: int = 90
    daily_update_hour: int = 2  # 0-23, UTC
    minute_candles_enabled: bool = True

    # Crash detection settings
    z_score_threshold: float = 3.0
    min_crash_duration_minutes: int = 5
    recovery_threshold_pct: float = 80.0

    # Recovery prediction settings
    depth_bin_size_pct: float = 20.0

    # Storage settings
    retention_days: int = 365
    crash_events_retention_days: int = 730

    # Ingestion performance settings
    x10_rate_limit_per_minute: int = 1000
    lighter_rate_limit_per_minute: int = 10
    backfill_chunk_days: int = 7
    x10_chunk_days: int = 1

    # Priority 1: Real-time crash detection settings
    realtime_crash_detection_enabled: bool = True
    crash_detection_interval_seconds: int = 60
    crash_detection_lookback_minutes: int = 60

    # Priority 2: Automatic volatility updates
    volatility_update_enabled: bool = True
    volatility_update_hour: int = 2  # UTC
    volatility_lookback_days: int = 90


class ShutdownSettings(BaseModel):
    """Shutdown behavior settings."""

    close_positions_on_exit: bool = False
    timeout_seconds: Decimal = Decimal("20.0")


class Settings(BaseSettings):
    """
    Main settings container.

    Loads from YAML file based on environment, then applies env var overrides.
    """

    # Environment
    env: str = Field(default="development", alias="BOT_ENV")

    # Live trading toggle
    live_trading: bool = False
    testing_mode: bool = False

    # Sub-settings
    lighter: ExchangeSettings = Field(default_factory=ExchangeSettings)
    x10: ExchangeSettings = Field(default_factory=ExchangeSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    reconciliation: ReconciliationSettings = Field(default_factory=ReconciliationSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    websocket: WebSocketSettings = Field(default_factory=WebSocketSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    shutdown: ShutdownSettings = Field(default_factory=ShutdownSettings)
    historical: HistoricalSettings = Field(default_factory=HistoricalSettings)

    model_config = {
        "env_prefix": "BOT_",
        "env_nested_delimiter": "__",
        "extra": "ignore",
    }

    def validate_for_live_trading(self) -> list[str]:
        """
        Validate that all required settings are present for live trading.

        This method should be called during bot startup when live_trading=True
        to fail fast with clear error messages instead of cryptic runtime errors.

        Returns:
            List of validation error messages. Empty list means all validations passed.

        Example:
            >>> settings = get_settings()
            >>> if settings.live_trading:
            ...     errors = settings.validate_for_live_trading()
            ...     if errors:
            ...         print("Configuration errors:")
            ...         for error in errors:
            ...             print(f"  - {error}")
            ...         sys.exit(1)
        """
        errors = []

        # Validate exchange credentials
        errors.extend(self.lighter.validate_for_live_trading("Lighter"))
        errors.extend(self.x10.validate_for_live_trading("X10"))

        # Validate database path
        if not self.database.path or self.database.path == "":
            errors.append("Database path is required")

        # Validate trading parameters
        if self.trading.desired_notional_usd <= 0:
            errors.append("trading.desired_notional_usd must be positive")

        if self.trading.max_open_trades < 1:
            errors.append("trading.max_open_trades must be at least 1")

        # Validate risk settings (can be expressed as percentage or decimal)
        # Allow both formats: 0.20 (decimal) or 20.0 (percentage)
        if self.risk.max_drawdown_pct <= 0:
            errors.append("risk.max_drawdown_pct must be positive")

        if self.risk.max_exposure_pct <= 0:
            errors.append("risk.max_exposure_pct must be positive")

        return errors

    @classmethod
    def from_yaml(cls, env: str = "development") -> Settings:
        """
        Load settings from config.yaml.

        Note: All settings are now in a single config.yaml file.
        The 'env' parameter is still used to set `settings.env` (for banners/logging).
        """
        config_dir = Path(__file__).parent
        yaml_file = config_dir / "config.yaml"

        data: dict = {}
        if yaml_file.exists():
            with open(yaml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            # Fallback to old structure for backwards compatibility
            default_file = config_dir / "default.yaml"
            env_file = config_dir / f"{env}.yaml"

            if default_file.exists():
                with open(default_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}

            if env_file.exists():
                with open(env_file, encoding="utf-8") as f:
                    env_data = yaml.safe_load(f) or {}
                data = _deep_merge(data, env_data)

        # Backwards compatibility: `max_trade_size_pct` used to live under `trading.*`.
        # Single source of truth is now `risk.max_trade_size_pct`.
        if isinstance(data.get("trading"), dict) and "max_trade_size_pct" in data["trading"]:
            data.setdefault("risk", {})
            if isinstance(data.get("risk"), dict) and "max_trade_size_pct" not in data["risk"]:
                data["risk"]["max_trade_size_pct"] = data["trading"]["max_trade_size_pct"]
            # Ensure we don't have two competing sources in runtime config
            del data["trading"]["max_trade_size_pct"]

        # Apply environment variable overrides for exchange settings
        # Lighter settings from env
        if "lighter" not in data:
            data["lighter"] = {}
        if os.getenv("LIGHTER_L1_ADDRESS"):
            data["lighter"]["l1_address"] = os.getenv("LIGHTER_L1_ADDRESS")
        if os.getenv("LIGHTER_ACCOUNT_INDEX"):
            # Parse as int, allow 0 as valid value
            val = os.getenv("LIGHTER_ACCOUNT_INDEX").strip()
            if val.lower() in ("none", "null", ""):
                data["lighter"]["account_index"] = None
            else:
                data["lighter"]["account_index"] = int(val)
        if os.getenv("LIGHTER_API_KEY_PRIVATE_KEY"):
            data["lighter"]["private_key"] = os.getenv("LIGHTER_API_KEY_PRIVATE_KEY")
        if os.getenv("LIGHTER_API_KEY_INDEX"):
            data["lighter"]["api_key_index"] = int(os.getenv("LIGHTER_API_KEY_INDEX"))
        # Legacy support for LIGHTER_PRIVATE_KEY
        if os.getenv("LIGHTER_PRIVATE_KEY") and not data["lighter"].get("private_key"):
            data["lighter"]["private_key"] = os.getenv("LIGHTER_PRIVATE_KEY")

        # X10 settings from env
        if "x10" not in data:
            data["x10"] = {}
        if os.getenv("X10_API_KEY"):
            data["x10"]["api_key"] = os.getenv("X10_API_KEY")
        if os.getenv("X10_PRIVATE_KEY"):
            data["x10"]["private_key"] = os.getenv("X10_PRIVATE_KEY")
        if os.getenv("X10_PUBLIC_KEY"):
            data["x10"]["public_key"] = os.getenv("X10_PUBLIC_KEY")
        if os.getenv("X10_VAULT_ID"):
            data["x10"]["vault_id"] = os.getenv("X10_VAULT_ID")

        # Telegram settings from env
        if "telegram" not in data:
            data["telegram"] = {}
        if os.getenv("TELEGRAM_BOT_TOKEN"):
            data["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
        if os.getenv("TELEGRAM_CHAT_ID"):
            data["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID")
        if os.getenv("TELEGRAM_ENABLED"):
            val = os.getenv("TELEGRAM_ENABLED").lower()
            data["telegram"]["enabled"] = val in ("true", "1", "yes")

        data["env"] = env

        # Warn about unknown keys before creating model (helps catch typos in config.yaml)
        _warn_unknown_keys(data, cls)

        return cls(**data)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, override wins on conflicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _collect_all_keys(data: dict, prefix: str = "") -> set[str]:
    """
    Recursively collect all keys from a nested dict.

    Returns keys in dot-notation format (e.g., "trading.velocity_forecast_enabled").
    """
    keys = set()
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        keys.add(full_key)
        if isinstance(value, dict):
            keys.update(_collect_all_keys(value, full_key))
    return keys


def _collect_model_fields(model_class: type[BaseModel], prefix: str = "") -> set[str]:
    """
    Recursively collect all field names from a Pydantic model.

    Returns field names in dot-notation format.
    """
    fields = set()
    for field_name, field_info in model_class.model_fields.items():
        full_key = f"{prefix}.{field_name}" if prefix else field_name
        fields.add(full_key)

        # Recursively collect fields from nested BaseModel types
        if field_info.default is not None:
            # Check if default is a BaseModel instance
            if isinstance(field_info.default, BaseModel):
                fields.update(_collect_model_fields(type(field_info.default), full_key))
            # Check if the field type annotation is a BaseModel subclass
            elif hasattr(field_info.annotation, "__origin__"):
                # Generic types like list[str] - skip
                continue
            elif (
                isinstance(field_info.annotation, type)
                and issubclass(field_info.annotation, BaseModel)
                and field_info.annotation.__name__ != "BaseModel"  # Skip BaseModel itself
            ):
                # This is a BaseModel subclass
                fields.update(_collect_model_fields(field_info.annotation, full_key))

    return fields


def _warn_unknown_keys(data: dict, model_class: type[BaseModel]) -> None:
    """
    Warn about unknown keys in YAML config that don't match model fields.

    This prevents silent config bugs where typos in key names are ignored.
    """
    yaml_keys = _collect_all_keys(data)
    model_fields = _collect_model_fields(model_class)

    unknown_keys = yaml_keys - model_fields

    if unknown_keys:
        logger.warning(
            f"Unknown configuration keys found (will be ignored due to extra='ignore'): {sorted(unknown_keys)}. "
            f"This may indicate typos in config.yaml or outdated config keys."
        )


@lru_cache(maxsize=4)
def get_settings(env: str | None = None) -> Settings:
    """Get cached settings instance."""
    resolved_env = env or os.getenv("BOT_ENV", "development")
    return Settings.from_yaml(env=resolved_env)
