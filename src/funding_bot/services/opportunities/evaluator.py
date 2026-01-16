"""
Symbol evaluation logic for opportunities.

Refactored for maintainability - extracted dataclasses and helper functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.domain.models import Exchange, Opportunity, Side
from funding_bot.observability.logging import get_logger
from funding_bot.services.liquidity_gates import (
    calculate_depth_cap_by_impact,
    calculate_l1_depth_cap,
    get_depth_gate_config,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

if TYPE_CHECKING:
    from funding_bot.domain.models import (
        FundingData,
        MarketInfo,
        OrderbookSnapshot,
        PriceSnapshot,
    )
    from funding_bot.services.opportunities.engine import OpportunityEngine

logger = get_logger(__name__)

# Track symbols that have missing historical data (log once per symbol)
_missing_historical_data_logged: set[str] = set()

# Constants
_DEFAULT_VELOCITY_LOOKBACK_HOURS = 6  # Default lookback for velocity forecast


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class MarketDataBundle:
    """Bundle of market data for symbol evaluation."""

    symbol: str
    price: "PriceSnapshot | None" = None
    funding: "FundingData | None" = None
    orderbook: "OrderbookSnapshot | None" = None
    lighter_info: "MarketInfo | None" = None
    x10_info: "MarketInfo | None" = None


@dataclass
class SizingConfig:
    """Configuration for position sizing."""

    target_notional: Decimal = Decimal("0")
    max_notional_cap: Decimal = Decimal("999999")
    effective_leverage: Decimal = Decimal("1")
    mid_price: Decimal = Decimal("0")


@dataclass
class DepthSizingConfig:
    """Configuration for depth-aware sizing."""

    enabled: bool = False
    min_l1_usd: Decimal = Decimal("0")
    min_l1_mult: Decimal = Decimal("0")
    max_l1_util: Decimal = Decimal("1.0")
    preflight_mult: Decimal | None = None
    # Base values before preflight adjustment
    base_min_l1_usd: Decimal = Decimal("0")
    base_min_l1_mult: Decimal = Decimal("0")
    base_max_l1_util: Decimal = Decimal("1.0")


@dataclass
class EvaluationResult:
    """Result of symbol evaluation with reject info."""

    opportunity: Opportunity | None = None
    reject_info: dict[str, str] = field(default_factory=dict)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _determine_direction(
    funding: "FundingData",
) -> tuple[Exchange, Exchange, Decimal, Decimal]:
    """
    Determine trade direction based on funding rates.

    Returns: (long_exchange, short_exchange, lighter_rate, x10_rate)
    """
    lighter_rate = funding.lighter_rate.rate_hourly if funding.lighter_rate else Decimal("0")
    x10_rate = funding.x10_rate.rate_hourly if funding.x10_rate else Decimal("0")

    # If Lighter rate > X10 rate: Long X10 (pay less), Short Lighter (receive more)
    # If X10 rate > Lighter rate: Long Lighter (pay less), Short X10 (receive more)
    if lighter_rate > x10_rate:
        long_exchange = Exchange.X10
        short_exchange = Exchange.LIGHTER
    else:
        long_exchange = Exchange.LIGHTER
        short_exchange = Exchange.X10

    return long_exchange, short_exchange, lighter_rate, x10_rate


def _calculate_sizing(
    settings: Any,
    available_equity: Decimal,
    lighter_info: "MarketInfo | None",
    x10_info: "MarketInfo | None",
    mid_price: Decimal,
) -> SizingConfig:
    """Calculate position sizing based on equity and leverage."""
    ts = settings.trading

    lighter_lev = lighter_info.lighter_max_leverage if lighter_info else Decimal("1")
    x10_lev = x10_info.x10_max_leverage if x10_info else Decimal("1")
    effective_leverage = min(lighter_lev, x10_lev)

    max_notional_cap = Decimal("999999")
    if available_equity > 0:
        cap_by_equity = available_equity * effective_leverage * Decimal("0.95")
        max_trade_pct_raw = getattr(settings.risk, "max_trade_size_pct", Decimal("100.0"))
        max_trade_pct = max(Decimal("0"), min(max_trade_pct_raw, Decimal("100.0"))) / Decimal("100.0")
        cap_by_trade_pct = cap_by_equity * max_trade_pct
        max_notional_cap = min(cap_by_equity, cap_by_trade_pct)

    return SizingConfig(
        target_notional=ts.desired_notional_usd,
        max_notional_cap=max_notional_cap,
        effective_leverage=effective_leverage,
        mid_price=mid_price,
    )


def _load_depth_sizing_config(settings: Any) -> DepthSizingConfig:
    """Load depth-aware sizing configuration."""
    ts = settings.trading
    es = getattr(settings, "execution", None)

    enabled = getattr(ts, "depth_gate_enabled", False)
    min_l1_usd = _safe_decimal(getattr(ts, "min_l1_notional_usd", None), Decimal("0"))
    min_l1_mult = _safe_decimal(getattr(ts, "min_l1_notional_multiple", None), Decimal("0"))
    max_l1_util = _safe_decimal(getattr(ts, "max_l1_qty_utilization", None), Decimal("1.0"))

    # Store base values
    base_min_l1_usd = min_l1_usd
    base_min_l1_mult = min_l1_mult
    base_max_l1_util = max_l1_util

    # Apply preflight multiplier if enabled
    preflight_mult: Decimal | None = None
    if es and getattr(es, "hedge_depth_preflight_enabled", False):
        mult = _safe_decimal(getattr(es, "hedge_depth_preflight_multiplier", None), Decimal("1.0"))
        if mult <= 1:
            mult = Decimal("1.0")
        preflight_mult = mult
        if min_l1_usd and min_l1_usd > 0:
            min_l1_usd = min_l1_usd * mult
        if min_l1_mult and min_l1_mult > 0:
            min_l1_mult = min_l1_mult * mult
        if max_l1_util and max_l1_util < 1 and mult > 1:
            max_l1_util = max_l1_util / mult

    return DepthSizingConfig(
        enabled=enabled,
        min_l1_usd=min_l1_usd,
        min_l1_mult=min_l1_mult,
        max_l1_util=max_l1_util,
        preflight_mult=preflight_mult,
        base_min_l1_usd=base_min_l1_usd,
        base_min_l1_mult=base_min_l1_mult,
        base_max_l1_util=base_max_l1_util,
    )


def _has_depth_qty(
    orderbook: "OrderbookSnapshot",
    exchange: Exchange,
    side: Side,
) -> bool:
    """Check if orderbook has valid quantity data for given exchange and side."""
    if exchange == Exchange.LIGHTER:
        if side == Side.BUY:
            return (orderbook.lighter_ask > 0) and (orderbook.lighter_ask_qty > 0)
        return (orderbook.lighter_bid > 0) and (orderbook.lighter_bid_qty > 0)
    if side == Side.BUY:
        return (orderbook.x10_ask > 0) and (orderbook.x10_ask_qty > 0)
    return (orderbook.x10_bid > 0) and (orderbook.x10_bid_qty > 0)


async def _apply_depth_cap(
    self: "OpportunityEngine",
    symbol: str,
    orderbook: "OrderbookSnapshot",
    lighter_side: Side,
    mid_price: Decimal,
    suggested_notional: Decimal,
    depth_cfg: DepthSizingConfig,
    include_reject_info: bool,
) -> tuple[Decimal | None, dict[str, str] | None]:
    """
    Apply depth-based cap to suggested notional.

    Returns: (capped_notional, reject_info) where reject_info is set on failure
    """
    ts = self.settings.trading

    # Check if cached depth is valid
    x10_side = lighter_side.inverse()
    cached_depth_ok = (
        _has_depth_qty(orderbook, Exchange.LIGHTER, lighter_side)
        and _has_depth_qty(orderbook, Exchange.X10, x10_side)
    )

    if not cached_depth_ok:
        # Skip depth-based sizing on partial snapshots
        return suggested_notional, None

    # Calculate L1 depth cap
    cap = calculate_l1_depth_cap(
        orderbook,
        lighter_side=lighter_side,
        x10_side=x10_side,
        mid_price=mid_price,
        min_l1_notional_usd=depth_cfg.min_l1_usd,
        min_l1_notional_multiple=depth_cfg.min_l1_mult,
        max_l1_qty_utilization=depth_cfg.max_l1_util,
        safety_buffer=Decimal("0.98"),
    )

    # Try IMPACT mode if L1-only fails
    use_impact, depth_levels, depth_impact = get_depth_gate_config(ts)
    impact_meta: dict[str, str] = {}

    if not cap.passed and use_impact:
        try:
            depth_book = await self.market_data.get_fresh_orderbook_depth(
                symbol,
                levels=depth_levels,
            )
            cap2 = calculate_depth_cap_by_impact(
                depth_book,
                lighter_side=lighter_side,
                x10_side=x10_side,
                mid_price=mid_price,
                min_l1_notional_usd=depth_cfg.min_l1_usd,
                min_l1_notional_multiple=depth_cfg.min_l1_mult,
                max_l1_qty_utilization=depth_cfg.max_l1_util,
                max_price_impact_pct=depth_impact,
                safety_buffer=Decimal("0.98"),
            )
            impact_meta = {
                "impact_levels": str(depth_levels),
                "impact_max_price_impact_pct": str(depth_impact),
                "impact_passed": str(bool(cap2.passed)),
                "impact_reason": str(cap2.reason),
                "impact_max_notional_usd": str(cap2.max_notional_usd),
            }
            if cap2.passed:
                cap = cap2
        except Exception as e:
            impact_meta = {
                "impact_levels": str(depth_levels),
                "impact_max_price_impact_pct": str(depth_impact),
                "impact_error": f"{type(e).__name__}: {e}",
            }
            logger.debug(f"Depth cap fallback failed for {symbol}: {e}")

    if not cap.passed:
        if include_reject_info:
            lighter_px = orderbook.lighter_ask if lighter_side == Side.BUY else orderbook.lighter_bid
            lighter_qty = orderbook.lighter_ask_qty if lighter_side == Side.BUY else orderbook.lighter_bid_qty
            x10_px = orderbook.x10_ask if x10_side == Side.BUY else orderbook.x10_bid
            x10_qty = orderbook.x10_ask_qty if x10_side == Side.BUY else orderbook.x10_bid_qty

            reject_info = {
                "reason": "depth_cap_failed",
                "depth_reason": str(cap.reason),
                "cap_max_notional_usd": str(getattr(cap, "max_notional_usd", "")),
                "l1_lighter_notional_usd": str(lighter_px * lighter_qty),
                "l1_x10_notional_usd": str(x10_px * x10_qty),
                "min_l1_notional_usd": str(depth_cfg.min_l1_usd),
                "min_l1_notional_multiple": str(depth_cfg.min_l1_mult),
                "max_l1_qty_utilization": str(depth_cfg.max_l1_util),
                "base_min_l1_notional_usd": str(depth_cfg.base_min_l1_usd),
                "base_min_l1_notional_multiple": str(depth_cfg.base_min_l1_mult),
                "base_max_l1_qty_utilization": str(depth_cfg.base_max_l1_util),
                **impact_meta,
            }
            if depth_cfg.preflight_mult is not None:
                reject_info["hedge_depth_preflight_multiplier"] = str(depth_cfg.preflight_mult)
            return None, reject_info
        return None, {"reason": "depth_cap_failed"}

    # Apply cap
    capped_notional = min(suggested_notional, cap.max_notional_usd)
    if capped_notional < Decimal("20.0"):
        if include_reject_info:
            return None, {
                "reason": "notional_too_small_after_depth_cap",
                "suggested_notional": str(capped_notional),
            }
        return None, {"reason": "notional_too_small_after_depth_cap"}

    return capped_notional, None


async def _fetch_historical_apy(
    self: "OpportunityEngine",
    symbol: str,
) -> list[Decimal] | None:
    """Fetch historical APY data for velocity forecast."""
    ts = self.settings.trading
    velocity_enabled = getattr(ts, "velocity_forecast_enabled", False)

    if not velocity_enabled:
        return None

    try:
        lookback = getattr(ts, "velocity_forecast_lookback_hours", _DEFAULT_VELOCITY_LOOKBACK_HOURS)
        historical_apy = await self.store.get_recent_apy_history(
            symbol=symbol,
            hours_back=lookback,
        )
        if not historical_apy or len(historical_apy) < lookback:
            _log_missing_historical_data_once(symbol, lookback)
            return None
        return historical_apy
    except Exception as e:
        logger.warning(f"Failed to fetch historical APY for {symbol}: {e}")
        return None


def _validate_ev_and_breakeven(
    self: "OpportunityEngine",
    ev: dict[str, Any],
    funding: "FundingData",
    suggested_notional: Decimal,
    include_reject_info: bool,
) -> dict[str, str] | None:
    """
    Validate EV and breakeven constraints.

    Returns: reject_info dict if validation fails, None if passed
    """
    ts = self.settings.trading

    # Minimum EV check
    min_ev_usd = getattr(ts, "min_expected_profit_entry_usd", Decimal("0"))
    total_ev = ev.get("total_ev", Decimal("0"))
    if total_ev < min_ev_usd:
        if include_reject_info:
            return {
                "reason": "ev_too_low",
                "ev_total": str(total_ev),
                "min_ev_usd": str(min_ev_usd),
                "apy": f"{funding.apy:.4%}",
                "suggested_notional": str(suggested_notional),
            }
        return {"reason": "ev_too_low"}

    # Breakeven check
    breakeven = self._calculate_breakeven_hours(
        ev_per_hour=ev.get("ev_per_hour", Decimal("0")),
        entry_cost=ev.get("entry_cost", Decimal("0")),
    )
    max_be = getattr(ts, "max_breakeven_hours", Decimal("12"))
    if breakeven > max_be:
        if include_reject_info:
            return {
                "reason": "breakeven_too_long",
                "breakeven_hours": str(breakeven),
                "max_breakeven_hours": str(max_be),
                "ev_per_hour": str(ev.get("ev_per_hour", Decimal("0"))),
                "entry_cost": str(ev.get("entry_cost", Decimal("0"))),
            }
        return {"reason": "breakeven_too_long"}

    # EV spread ratio check
    spread_cost = ev.get("slippage", Decimal("0"))
    if spread_cost > 0:
        min_ratio = getattr(ts, "min_ev_spread_ratio", Decimal("2.0"))
        if total_ev < (spread_cost * min_ratio):
            if include_reject_info:
                return {
                    "reason": "ev_spread_ratio_too_low",
                    "ev_total": str(total_ev),
                    "spread_cost": str(spread_cost),
                    "min_ratio": str(min_ratio),
                    "required": str(spread_cost * min_ratio),
                }
            return {"reason": "ev_spread_ratio_too_low"}

    return None


def _build_opportunity(
    symbol: str,
    funding: "FundingData",
    orderbook: "OrderbookSnapshot | None",
    long_exchange: Exchange,
    short_exchange: Exchange,
    lighter_rate: Decimal,
    x10_rate: Decimal,
    mid_price: Decimal,
    suggested_qty: Decimal,
    suggested_notional: Decimal,
    ev: dict[str, Any],
    breakeven: Decimal,
    liquidity_score: Decimal,
) -> Opportunity:
    """Build the Opportunity object from evaluation results."""
    observed_spread = (
        orderbook.entry_spread_pct(long_exchange.value) if orderbook else Decimal("1")
    )
    confidence = Decimal("1") - (observed_spread * Decimal("10"))
    confidence = max(Decimal("0"), min(confidence, Decimal("1")))

    return Opportunity(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        net_funding_hourly=funding.net_rate_hourly,
        apy=funding.apy,
        spread_pct=observed_spread,
        mid_price=mid_price,
        lighter_best_bid=orderbook.lighter_bid if orderbook else Decimal("0"),
        lighter_best_ask=orderbook.lighter_ask if orderbook else Decimal("0"),
        x10_best_bid=orderbook.x10_bid if orderbook else Decimal("0"),
        x10_best_ask=orderbook.x10_ask if orderbook else Decimal("0"),
        suggested_qty=suggested_qty,
        suggested_notional=suggested_notional,
        expected_value_usd=ev.get("total_ev", Decimal("0")),
        breakeven_hours=breakeven,
        liquidity_score=liquidity_score,
        confidence=confidence,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
    )


def _log_missing_historical_data_once(symbol: str, lookback_hours: int) -> None:
    """
    Track symbols with missing historical APY data (batch logged later).

    This prevents log spam by collecting symbols and logging a summary
    instead of individual warnings for each symbol.

    Args:
        symbol: Trading symbol
        lookback_hours: Number of hours of data requested
    """
    global _missing_historical_data_logged

    if symbol not in _missing_historical_data_logged:
        _missing_historical_data_logged.add(symbol)
        # Log at DEBUG level for individual symbols - summary logged elsewhere
        logger.debug(
            f"Insufficient historical APY data for {symbol}: "
            f"need {lookback_hours} hours for velocity forecast."
        )


def log_missing_historical_data_summary() -> None:
    """
    Log a summary of all symbols missing historical data.
    Call this once per scan cycle to avoid log spam.
    """
    global _missing_historical_data_logged
    if _missing_historical_data_logged:
        count = len(_missing_historical_data_logged)
        # Only log if there are symbols (and only once at startup)
        if count > 0:
            symbols_list = sorted(_missing_historical_data_logged)[:10]  # Show max 10
            more = f" and {count - 10} more" if count > 10 else ""
            logger.info(
                f"Historical APY data building for {count} symbols: "
                f"{', '.join(symbols_list)}{more}. "
                f"Velocity/Z-score features disabled until 6h of data collected."
            )


async def _evaluate_symbol(
    self: OpportunityEngine,
    symbol: str,
    available_equity: Decimal = Decimal("0")
) -> Opportunity | None:
    """Evaluate a single symbol for opportunity.

    Note: This is the high-throughput scan path and intentionally avoids verbose logging.
    For the "fresh data fetch" validation path (top candidates only), use
    `_evaluate_symbol_with_reject_info()` so we can log *why* a candidate dropped.
    """
    opp, _ = await self._evaluate_symbol_with_reject_info(
        symbol,
        available_equity=available_equity,
        include_reject_info=False,
    )
    return opp


async def _evaluate_symbol_with_reject_info(
    self: "OpportunityEngine",
    symbol: str,
    *,
    available_equity: Decimal = Decimal("0"),
    include_reject_info: bool = True,
) -> tuple[Opportunity | None, dict[str, str]]:
    """
    Evaluate a symbol and optionally return a compact reject reason payload.

    This is used only for the low-frequency "fresh data fetch" re-validation of top
    candidates, so it's safe to include extra diagnostics.

    The evaluation pipeline:
    1. Fetch and validate market data
    2. Apply symbol filters
    3. Determine trade direction
    4. Calculate position sizing
    5. Apply depth-based caps
    6. Fetch historical APY (if velocity enabled)
    7. Calculate and validate EV/breakeven
    8. Build opportunity object
    """
    reject: dict[str, str] = {}

    def _rej(reason: str, **details: object) -> tuple[None, dict[str, str]]:
        if include_reject_info:
            reject["reason"] = reason
            for k, v in details.items():
                if v is None:
                    continue
                reject[k] = str(v)
        return None, reject

    # Step 1: Get market data
    price = self.market_data.get_price(symbol)
    funding = self.market_data.get_funding(symbol)
    orderbook = self.market_data.get_orderbook(symbol)

    if not price or not funding:
        return _rej(
            "missing market data",
            has_price=bool(price),
            has_funding=bool(funding),
            has_orderbook=bool(orderbook),
        )

    # Step 2: Apply filters
    filter_result = self._apply_filters(symbol, price, funding, orderbook)
    if not filter_result.passed:
        return _rej(
            "filtered",
            filter_reason=getattr(filter_result, "reason", ""),
            apy=f"{funding.apy:.4%}",
        )

    # Step 3: Validate mid price
    mid_price = price.mid_price
    if mid_price == 0:
        return _rej("mid_price=0")

    # Step 4: Determine direction
    long_exchange, short_exchange, lighter_rate, x10_rate = _determine_direction(funding)

    # Step 5: Calculate sizing
    lighter_info = self.market_data.get_market_info(symbol, Exchange.LIGHTER)
    x10_info = self.market_data.get_market_info(symbol, Exchange.X10)

    sizing = _calculate_sizing(
        self.settings,
        available_equity,
        lighter_info,
        x10_info,
        mid_price,
    )

    suggested_notional = min(sizing.target_notional, sizing.max_notional_cap)
    suggested_qty = suggested_notional / mid_price

    # Step 6: Apply depth-based sizing caps
    depth_cfg = _load_depth_sizing_config(self.settings)
    if orderbook and depth_cfg.enabled:
        lighter_side = Side.BUY if long_exchange == Exchange.LIGHTER else Side.SELL

        capped_notional, depth_reject = await _apply_depth_cap(
            self,
            symbol,
            orderbook,
            lighter_side,
            mid_price,
            suggested_notional,
            depth_cfg,
            include_reject_info,
        )

        if depth_reject is not None:
            return None, depth_reject

        if capped_notional is not None:
            suggested_notional = capped_notional
            suggested_qty = suggested_notional / mid_price

    # Step 7: Fetch fees and historical APY
    fees = self.market_data.get_fee_schedule(symbol)
    historical_apy = await _fetch_historical_apy(self, symbol)

    # Step 8: Calculate expected value
    ev = self._calculate_expected_value(
        symbol=symbol,
        funding=funding,
        notional=suggested_notional,
        orderbook=orderbook,
        fees=fees,
        price_snapshot=price,
        historical_apy=historical_apy,
    )

    # Step 9: Validate EV and breakeven
    ev_reject = _validate_ev_and_breakeven(
        self, ev, funding, suggested_notional, include_reject_info
    )
    if ev_reject is not None:
        return None, ev_reject

    # Step 10: Calculate final metrics
    breakeven = self._calculate_breakeven_hours(
        ev_per_hour=ev.get("ev_per_hour", Decimal("0")),
        entry_cost=ev.get("entry_cost", Decimal("0")),
    )
    liquidity_score = self._calculate_liquidity_score(orderbook, suggested_qty)

    # Step 11: Build opportunity
    opp = _build_opportunity(
        symbol=symbol,
        funding=funding,
        orderbook=orderbook,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        lighter_rate=lighter_rate,
        x10_rate=x10_rate,
        mid_price=mid_price,
        suggested_qty=suggested_qty,
        suggested_notional=suggested_notional,
        ev=ev,
        breakeven=breakeven,
        liquidity_score=liquidity_score,
    )

    return opp, reject
