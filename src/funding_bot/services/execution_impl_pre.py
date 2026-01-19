"""
Execution preflight checks for execution_impl.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.domain.models import (
    ExecutionAttemptStatus,
    ExecutionState,
    Opportunity,
    Trade,
    TradeStatus,
)
from funding_bot.domain.rules import get_dynamic_max_spread
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.services.liquidity_gates import (
    estimate_entry_spread_pct_by_impact,
    get_depth_gate_config,
)
from funding_bot.services.liquidity_gates_preflight import (
    PreflightLiquidityConfig,
    check_preflight_liquidity,
)
from funding_bot.services.market_data import (
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

if TYPE_CHECKING:
    from funding_bot.services.market_data import MarketDataService

logger = get_logger("funding_bot.services.execution")


# ---------------------------------------------------------------------------
# Dataclasses for Preflight Check Configuration
# ---------------------------------------------------------------------------


@dataclass
class SmartPricingConfig:
    """
    Configuration for smart pricing depth-VWAP spread check.

    Smart pricing uses orderbook depth to calculate volume-weighted
    average prices, providing more accurate spread estimates for
    larger orders that would consume multiple price levels.

    Attributes:
        enabled: Whether smart pricing is active.
        max_price_impact_pct: Max allowed price impact for VWAP calc.
        depth_levels: Number of orderbook levels to include in VWAP.
    """

    enabled: bool = False
    max_price_impact_pct: Decimal = Decimal("0.5")
    depth_levels: int = 10


@dataclass
class SpreadCheckConfig:
    """
    Configuration for entry spread validation.

    Controls how entry spreads are calculated and validated before
    executing a trade. Supports both L1 (top-of-book) and depth-based
    spread calculations.

    Attributes:
        max_spread: Maximum allowed entry spread (e.g., 0.01 = 1%).
        smart_pricing: Config for depth-VWAP based spread calculation.
        negative_guard_multiple: Multiplier for revalidating favorable spreads.
        use_impact: Use impact-based (depth) spread calculation.
        depth_levels: Number of levels for depth-based calculations.
    """

    max_spread: Decimal = Decimal("0.01")
    smart_pricing: SmartPricingConfig = field(default_factory=SmartPricingConfig)
    negative_guard_multiple: Decimal = Decimal("2.0")
    use_impact: bool = False
    depth_levels: int = 10


@dataclass
class SpreadCheckResult:
    """
    Result of spread check with optional depth orderbook.

    Contains the calculated entry spread and cost, along with any
    depth orderbook data that was fetched during the check.

    Attributes:
        entry_spread: Calculated entry spread (can be negative if favorable).
        spread_cost: Spread cost for P&L (max of 0 and entry_spread).
        depth_ob: Depth orderbook snapshot if fetched during smart pricing.
        used_smart_pricing: True if depth-VWAP was used instead of L1.
    """

    entry_spread: Decimal
    spread_cost: Decimal
    depth_ob: OrderbookDepthSnapshot | None = None
    used_smart_pricing: bool = False


# ---------------------------------------------------------------------------
# Helper Functions for Spread Check
# ---------------------------------------------------------------------------


def _load_spread_check_config(settings: Any, opp_apy: Decimal) -> SpreadCheckConfig:
    """
    Load spread check configuration from settings.

    Calculates dynamic max spread based on opportunity APY and loads
    all related configuration for spread validation.

    Args:
        settings: Application settings with trading and execution config.
        opp_apy: Opportunity APY for dynamic spread calculation.

    Returns:
        SpreadCheckConfig with all spread validation parameters.
    """
    ts = settings.trading
    es = settings.execution

    max_spread = get_dynamic_max_spread(
        opp_apy,
        ts.max_spread_filter_percent,
        ts.max_spread_cap_percent,
    )

    use_impact, depth_levels, depth_impact = get_depth_gate_config(ts)

    smart_enabled = bool(getattr(es, "smart_pricing_enabled", False))
    smart_impact = _safe_decimal(
        getattr(es, "smart_pricing_max_price_impact_percent", None),
        default=depth_impact,
    )
    smart_levels = int(getattr(es, "smart_pricing_depth_levels", depth_levels) or depth_levels)
    smart_levels = max(1, min(smart_levels, 250))

    guard_mult = _safe_decimal(
        getattr(ts, "negative_spread_guard_multiple", None),
        Decimal("2.0"),
    )
    guard_mult = max(Decimal("1.0"), min(guard_mult, Decimal("10.0")))

    return SpreadCheckConfig(
        max_spread=max_spread,
        smart_pricing=SmartPricingConfig(
            enabled=smart_enabled,
            max_price_impact_pct=smart_impact,
            depth_levels=smart_levels,
        ),
        negative_guard_multiple=guard_mult,
        use_impact=use_impact,
        depth_levels=depth_levels,
    )


async def _apply_smart_pricing_spread(
    market_data: MarketDataService,
    symbol: str,
    long_exchange: str,
    suggested_qty: Decimal,
    config: SmartPricingConfig,
) -> tuple[Decimal | None, OrderbookDepthSnapshot | None]:
    """
    Apply smart pricing depth-VWAP spread calculation.

    Fetches orderbook depth and calculates volume-weighted spread
    based on the suggested trade quantity.

    Args:
        market_data: Market data service for fetching depth.
        symbol: Trading symbol (e.g., "BTC").
        long_exchange: Exchange for long side ("lighter" or "x10").
        suggested_qty: Target trade quantity for VWAP calculation.
        config: Smart pricing configuration.

    Returns:
        Tuple of (calculated_spread, depth_orderbook).
        spread is None if calculation failed.
    """
    depth_ob: OrderbookDepthSnapshot | None = None
    with contextlib.suppress(Exception):
        depth_ob = await market_data.get_fresh_orderbook_depth(
            symbol,
            levels=config.depth_levels,
        )
    if depth_ob is not None:
        depth_spread, depth_metrics = estimate_entry_spread_pct_by_impact(
            depth_ob,
            long_exchange=long_exchange,
            target_qty=suggested_qty,
            max_price_impact_pct=config.max_price_impact_pct,
        )
        if depth_metrics.get("ok"):
            return depth_spread, depth_ob
    return None, depth_ob


async def _revalidate_orderbook_for_guard(
    market_data: MarketDataService,
    symbol: str,
    long_exchange: str,
    entry_spread: Decimal,
    guard_limit: Decimal,
) -> tuple[OrderbookSnapshot | None, Decimal]:
    """
    Revalidate orderbook if favorable spread exceeds guard.

    Suspiciously favorable spreads (large negative values) may indicate
    stale data. This function re-fetches the orderbook to confirm.

    Args:
        market_data: Market data service for fetching orderbook.
        symbol: Trading symbol (e.g., "BTC").
        long_exchange: Exchange for long side ("lighter" or "x10").
        entry_spread: Currently calculated entry spread.
        guard_limit: Threshold for triggering revalidation.

    Returns:
        Tuple of (new_orderbook, updated_spread).
        orderbook is None if no revalidation was needed.
    """
    if entry_spread < 0 and abs(entry_spread) > guard_limit:
        logger.info(
            f"Favorable spread exceeds guard for {symbol}; "
            f"revalidating orderbook (spread={entry_spread:.4%}, guard={guard_limit:.4%})"
        )
        with contextlib.suppress(Exception):
            fresh_ob = await market_data.get_fresh_orderbook(symbol)
            return fresh_ob, fresh_ob.entry_spread_pct(long_exchange)
    return None, entry_spread


def _build_spread_kpi_data(
    opp: Opportunity,
    fresh_ob: OrderbookSnapshot,
    long_exchange: str,
    entry_spread: Decimal,
    spread_cost: Decimal,
    max_spread: Decimal,
    smart_config: SmartPricingConfig,
    used_depth: bool,
) -> dict[str, Any]:
    """
    Build KPI data for spread check stage.

    Creates a structured dictionary for KPI logging with all relevant
    spread check metrics and orderbook data.

    Args:
        opp: Opportunity being evaluated.
        fresh_ob: Fresh orderbook snapshot with exchange prices.
        long_exchange: Exchange for long side ("lighter" or "x10").
        entry_spread: Calculated entry spread.
        spread_cost: Spread cost for P&L calculation.
        max_spread: Maximum allowed spread.
        smart_config: Smart pricing configuration.
        used_depth: Whether depth-based pricing was used.

    Returns:
        Dict with KPI data for the spread check stage.
    """
    return {
        "stage": "SPREAD_CHECK",
        "apy": opp.apy,
        "expected_value_usd": opp.expected_value_usd,
        "breakeven_hours": opp.breakeven_hours,
        "opp_spread_pct": opp.spread_pct,
        "entry_spread_pct": entry_spread,
        "entry_spread_cost_pct": spread_cost,
        "max_spread_pct": max_spread,
        "data": {
            "long_exchange": long_exchange,
            "lighter_bid": str(fresh_ob.lighter_bid),
            "lighter_ask": str(fresh_ob.lighter_ask),
            "x10_bid": str(fresh_ob.x10_bid),
            "x10_ask": str(fresh_ob.x10_ask),
            "smart_pricing": {
                "enabled": smart_config.enabled,
                "used_depth": used_depth,
                "levels": smart_config.depth_levels if smart_config.enabled else None,
                "max_price_impact_pct": str(smart_config.max_price_impact_pct) if smart_config.enabled else None,
            },
        },
    }


# ---------------------------------------------------------------------------
# Helper Functions for Preflight Liquidity Check
# ---------------------------------------------------------------------------


def _load_preflight_liquidity_config(settings: Any) -> PreflightLiquidityConfig:
    """
    Load preflight liquidity configuration from settings.

    Extracts all parameters needed for the preflight liquidity check
    which verifies sufficient depth on both exchanges before execution.

    Args:
        settings: Application settings with trading configuration.

    Returns:
        PreflightLiquidityConfig with all liquidity check parameters.
    """
    ts = settings.trading
    return PreflightLiquidityConfig(
        enabled=True,
        depth_levels=int(getattr(ts, "preflight_liquidity_depth_levels", 5) or 5),
        safety_factor=_safe_decimal(
            getattr(ts, "preflight_liquidity_safety_factor", None),
            Decimal("3.0"),
        ),
        max_spread_bps=_safe_decimal(
            getattr(ts, "preflight_liquidity_max_spread_bps", None),
            Decimal("50"),
        ),
        orderbook_cache_ttl_seconds=_safe_decimal(
            getattr(ts, "preflight_liquidity_orderbook_cache_ttl_seconds", None),
            Decimal("5.0"),
        ),
        fallback_to_l1=bool(getattr(ts, "preflight_liquidity_fallback_to_l1", True)),
        min_liquidity_threshold=_safe_decimal(
            getattr(ts, "preflight_liquidity_min_liquidity_threshold", None),
            Decimal("0.01"),
        ),
    )


def _build_liquidity_kpi_data(
    liquidity_result: Any,
    config: PreflightLiquidityConfig,
    required_qty: Decimal,
) -> dict[str, Any]:
    """
    Build KPI data for liquidity check stage.

    Creates a structured dictionary for KPI logging with liquidity
    check results from both exchanges.

    Args:
        liquidity_result: Result from check_preflight_liquidity.
        config: Liquidity check configuration used.
        required_qty: Quantity that was checked for liquidity.

    Returns:
        Dict with KPI data for the liquidity check stage.
    """
    return {
        "stage": "LIQUIDITY_CHECK",
        "data": {
            "preflight_liquidity": {
                "passed": liquidity_result.passed,
                "required_qty": str(required_qty),
                "lighter_available_qty": str(liquidity_result.lighter_available_qty),
                "x10_available_qty": str(liquidity_result.x10_available_qty),
                "lighter_depth_levels": liquidity_result.lighter_depth_levels,
                "x10_depth_levels": liquidity_result.x10_depth_levels,
                "spread_bps": str(liquidity_result.spread_bps),
                "latency_ms": liquidity_result.latency_ms,
                "safety_factor": str(config.safety_factor),
                "depth_levels": config.depth_levels,
            },
        },
    }


# ---------------------------------------------------------------------------
# Main Preflight Function (Orchestrator)
# ---------------------------------------------------------------------------


async def _execute_impl_pre(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
) -> tuple[OrderbookSnapshot, Decimal, OrderbookDepthSnapshot | None] | ExecutionResult:
    """
    Pre-execution checks: spread validation, depth fetch, and liquidity verification.

    Orchestrates:
    1. WebSocket warmup for the symbol
    2. Entry spread check (L1 or smart pricing depth-VWAP)
    3. Negative spread guard revalidation
    4. Optional depth orderbook fetch
    5. Basic preflight checks (balance)
    6. Preflight liquidity verification
    """
    # 1. WebSocket warmup
    with contextlib.suppress(Exception):
        await self.lighter.ensure_orderbook_ws(opp.symbol, timeout=2.0)  # type: ignore[attr-defined]

    # 2. Load configuration
    config = _load_spread_check_config(self.settings, opp.apy)
    long_exchange = opp.long_exchange.value
    ts = self.settings.trading

    # 3. Get fresh orderbook and calculate entry spread
    fresh_ob = await self._get_best_effort_orderbook(opp.symbol, max_age_seconds=1.0)
    entry_spread = fresh_ob.entry_spread_pct(long_exchange)
    depth_ob: OrderbookDepthSnapshot | None = None
    used_smart_pricing = False

    # 4. Smart Pricing: try depth-VWAP if L1 spread fails
    if (
        config.smart_pricing.enabled
        and getattr(ts, "depth_gate_enabled", False)
        and config.use_impact
        and (entry_spread > config.max_spread or entry_spread >= Decimal("1.0"))
    ):
        smart_spread, depth_ob = await _apply_smart_pricing_spread(
            self.market_data,
            opp.symbol,
            long_exchange,
            opp.suggested_qty,
            config.smart_pricing,
        )
        if smart_spread is not None:
            entry_spread = smart_spread
            used_smart_pricing = True

    # 5. Negative spread guard revalidation
    guard_limit = config.max_spread * config.negative_guard_multiple
    revalidated_ob, entry_spread = await _revalidate_orderbook_for_guard(
        self.market_data, opp.symbol, long_exchange, entry_spread, guard_limit
    )
    if revalidated_ob is not None:
        fresh_ob = revalidated_ob

    # 6. Calculate spread cost and log KPI
    spread_cost = entry_spread if entry_spread > 0 else Decimal("0")
    await self._kpi_update_attempt(
        attempt_id,
        _build_spread_kpi_data(
            opp,
            fresh_ob,
            long_exchange,
            entry_spread,
            spread_cost,
            config.max_spread,
            config.smart_pricing,
            used_smart_pricing or depth_ob is not None,
        ),
    )

    logger.debug(
        f"Entry spread check: {opp.symbol} "
        f"direction=Long {long_exchange} "
        f"L={fresh_ob.lighter_bid:.6f}/{fresh_ob.lighter_ask:.6f} "
        f"X10={fresh_ob.x10_bid:.6f}/{fresh_ob.x10_ask:.6f} "
        f"spread={entry_spread:.4%} cost={spread_cost:.4%} vs max={config.max_spread:.4%}"
    )

    # 7. Reject if spread too high
    if spread_cost > config.max_spread:
        logger.warning(
            f"Rejecting {opp.symbol}: spread cost {spread_cost:.4%} > "
            f"max {config.max_spread:.4%} (raw={entry_spread:.4%}, {long_exchange})"
        )
        trade.status = TradeStatus.REJECTED
        trade.execution_state = ExecutionState.ABORTED
        trade.error = f"Entry spread cost too high: {spread_cost:.4%}"
        await self._kpi_update_attempt(
            attempt_id,
            {
                "status": ExecutionAttemptStatus.REJECTED,
                "stage": "REJECTED_SPREAD",
                "reason": "Entry spread too high",
                "entry_spread_pct": entry_spread,
                "entry_spread_cost_pct": spread_cost,
                "max_spread_pct": config.max_spread,
                "data": {"error": "Entry spread cost too high"},
            },
        )
        return ExecutionResult(success=False, trade=trade, error="Entry spread too high")

    # 8. Fetch depth orderbook if needed for gating (reuse if already fetched)
    if getattr(ts, "depth_gate_enabled", False) and config.use_impact and depth_ob is None:
        try:
            depth_ob = await self.market_data.get_fresh_orderbook_depth(opp.symbol, levels=config.depth_levels)
        except Exception as e:
            logger.warning(f"Failed to fetch depth orderbook for {opp.symbol}; falling back to L1: {e}")
            depth_ob = None

    # 9. Run basic preflight checks
    await self._preflight_checks(trade, opp)
    await self._kpi_update_attempt(attempt_id, {"stage": "PREFLIGHT_OK"})

    # 10. Preflight liquidity check
    if bool(getattr(ts, "preflight_liquidity_enabled", True)):
        liquidity_result = await _run_preflight_liquidity_check(self, opp, trade, attempt_id)
        if liquidity_result is not None:
            return liquidity_result

    return fresh_ob, config.max_spread, depth_ob


async def _run_preflight_liquidity_check(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
) -> ExecutionResult | None:
    """
    Run preflight liquidity check. Returns ExecutionResult if failed, None if passed.
    """
    try:
        liquidity_config = _load_preflight_liquidity_config(self.settings)
        check_side = trade.leg1.side

        liquidity_result = await check_preflight_liquidity(
            lighter_adapter=self.lighter,
            x10_adapter=self.x10,
            symbol=opp.symbol,
            required_qty=opp.suggested_qty,
            side=check_side,
            config=liquidity_config,
        )

        logger.info(
            f"Pre-flight liquidity check for {opp.symbol}: "
            f"{'PASSED' if liquidity_result.passed else 'FAILED'} | "
            f"Required={opp.suggested_qty:.6f} | "
            f"Lighter={liquidity_result.lighter_available_qty:.6f} ({liquidity_result.lighter_depth_levels} levels) | "
            f"X10={liquidity_result.x10_available_qty:.6f} ({liquidity_result.x10_depth_levels} levels) | "
            f"Spread={liquidity_result.spread_bps:.1f} bps | "
            f"Latency={liquidity_result.latency_ms:.1f}ms"
        )

        await self._kpi_update_attempt(
            attempt_id,
            _build_liquidity_kpi_data(liquidity_result, liquidity_config, opp.suggested_qty),
        )

        if not liquidity_result.passed:
            logger.warning(
                f"Rejecting {opp.symbol}: Pre-flight liquidity check failed | "
                f"Reason: {liquidity_result.failure_reason} | "
                f"Lighter={liquidity_result.lighter_available_qty:.6f}, X10={liquidity_result.x10_available_qty:.6f}"
            )
            trade.status = TradeStatus.REJECTED
            trade.execution_state = ExecutionState.ABORTED
            trade.error = f"Insufficient liquidity: {liquidity_result.failure_reason}"
            await self._kpi_update_attempt(
                attempt_id,
                {
                    "status": ExecutionAttemptStatus.REJECTED,
                    "stage": "REJECTED_LIQUIDITY",
                    "reason": "Pre-flight liquidity check failed",
                    "data": {
                        "failure_reason": liquidity_result.failure_reason,
                        "lighter_available_qty": str(liquidity_result.lighter_available_qty),
                        "x10_available_qty": str(liquidity_result.x10_available_qty),
                    },
                },
            )
            return ExecutionResult(
                success=False,
                trade=trade,
                error=f"Pre-flight liquidity check failed: {liquidity_result.failure_reason}",
            )

    except Exception as liquidity_err:
        logger.error(
            f"Pre-flight liquidity check error for {opp.symbol}: {liquidity_err}. "
            f"Proceeding with trade (degraded mode)."
        )
        await self._kpi_update_attempt(
            attempt_id,
            {"stage": "LIQUIDITY_CHECK_ERROR", "data": {"error": str(liquidity_err), "degraded_mode": True}},
        )

    return None
