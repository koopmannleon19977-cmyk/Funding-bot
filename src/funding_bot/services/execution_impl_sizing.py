"""
Execution sizing logic for execution_impl.

Refactored for maintainability - extracted dataclasses and helper functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.domain.models import (
    Exchange,
    ExecutionAttemptStatus,
    ExecutionState,
    Opportunity,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.services.liquidity_gates import (
    calculate_depth_cap_by_impact,
    calculate_l1_depth_cap,
)
from funding_bot.services.market_data import (
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

if TYPE_CHECKING:
    from funding_bot.adapters.exchanges.lighter.adapter import LighterExchangeAdapter
    from funding_bot.adapters.exchanges.x10.adapter import X10ExchangeAdapter
    from funding_bot.config.settings import Settings
    from funding_bot.services.market_data import MarketDataService

logger = get_logger("funding_bot.services.execution")


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class BalanceData:
    """
    Balance information from both exchanges.

    Attributes:
        lighter_total: Total equity on Lighter exchange (USD).
        lighter_available: Available margin on Lighter exchange (USD).
        x10_total: Total equity on X10 exchange (USD).
        x10_available: Available margin on X10 exchange (USD).
    """

    lighter_total: Decimal = Decimal("0")
    lighter_available: Decimal = Decimal("0")
    x10_total: Decimal = Decimal("0")
    x10_available: Decimal = Decimal("0")

    @property
    def total_equity(self) -> Decimal:
        """Combined total equity across both exchanges."""
        return self.lighter_total + self.x10_total


@dataclass
class LeverageConfig:
    """
    Effective leverage configuration for both exchanges.

    The effective leverage is the minimum of user-configured leverage
    and the exchange's IMR (Initial Margin Requirement) limit.

    Attributes:
        lighter_imr_limit: Max leverage allowed by Lighter exchange.
        x10_imr_limit: Max leverage allowed by X10 exchange.
        lighter_user_lev: User-configured leverage for Lighter.
        x10_user_lev: User-configured leverage for X10.
        eff_lighter: Effective leverage for Lighter (min of user/limit).
        eff_x10: Effective leverage for X10 (min of user/limit).
    """

    lighter_imr_limit: Decimal = Decimal("3")
    x10_imr_limit: Decimal = Decimal("5")
    lighter_user_lev: Decimal = Decimal("3")
    x10_user_lev: Decimal = Decimal("5")
    eff_lighter: Decimal = Decimal("3")
    eff_x10: Decimal = Decimal("5")


@dataclass
class RiskCapacity:
    """
    Risk and liquidity capacity calculations.

    Determines how much notional exposure is available for a new trade
    based on risk limits and available margin on both exchanges.

    Attributes:
        global_risk_cap: Max total exposure based on equity and risk %.
        current_exposure: Sum of notional from existing open trades.
        remaining_risk_cap: global_risk_cap - current_exposure.
        max_lighter_pos: Max position size on Lighter (70% of available * lev).
        max_x10_pos: Max position size on X10 (95% of available * lev).
        liquidity_cap: Minimum of max_lighter_pos and max_x10_pos.
        available_exposure: Final available = min(remaining_risk_cap, liquidity_cap).
    """

    global_risk_cap: Decimal = Decimal("0")
    current_exposure: Decimal = Decimal("0")
    remaining_risk_cap: Decimal = Decimal("0")
    max_lighter_pos: Decimal = Decimal("0")
    max_x10_pos: Decimal = Decimal("0")
    liquidity_cap: Decimal = Decimal("0")
    available_exposure: Decimal = Decimal("0")


@dataclass
class SlotAllocation:
    """
    Slot-based allocation result.

    Divides available exposure across remaining trade slots to ensure
    diversification and prevent over-concentration in a single trade.

    Attributes:
        remaining_slots: Number of trade slots still available (max_open - current).
        slot_based_size: available_exposure / remaining_slots.
        risk_based_cap: Max per-trade size from risk.max_trade_size_pct.
        target_notional: Final target = min(slot_based, risk_cap, opp suggestion).
    """

    remaining_slots: int = 1
    slot_based_size: Decimal = Decimal("0")
    risk_based_cap: Decimal = Decimal("0")
    target_notional: Decimal = Decimal("0")


@dataclass
class DepthGateConfig:
    """
    Configuration for depth gate sizing.

    Controls liquidity-aware position sizing to prevent excessive
    slippage and ensure orderbook can support the trade size.

    Attributes:
        enabled: Whether depth gating is active.
        mode: "L1" for top-of-book only, "IMPACT" for full depth analysis.
        levels: Number of orderbook levels to analyze (IMPACT mode).
        max_price_impact_pct: Max allowed price impact (e.g., 0.0015 = 0.15%).
        strict_preflight: Apply stricter checks for hedge depth.
        multiplier: Multiplier for preflight thresholds (>= 1.0).
        min_l1_notional_usd: Minimum L1 depth required in USD.
        min_l1_notional_multiple: Required L1 depth as multiple of trade size.
        max_l1_qty_utilization: Max fraction of L1 qty to consume (0.0-1.0).
    """

    enabled: bool = False
    mode: str = "L1"  # "L1" or "IMPACT"
    levels: int = 20
    max_price_impact_pct: Decimal = Decimal("0.0015")
    strict_preflight: bool = False
    multiplier: Decimal = Decimal("1.0")
    min_l1_notional_usd: Decimal = Decimal("0")
    min_l1_notional_multiple: Decimal = Decimal("0")
    max_l1_qty_utilization: Decimal = Decimal("1.0")


@dataclass
class SizingResult:
    """
    Result of sizing operation.

    Contains the calculated trade size and any rejection information
    if the trade could not be sized due to insufficient liquidity or risk limits.

    Attributes:
        target_notional: Final target notional in USD.
        target_qty: Final target quantity in base asset.
        depth_ob: Orderbook depth snapshot used for sizing (if IMPACT mode).
        depth_cap_metrics: Detailed metrics from depth cap calculation.
        rejected: True if trade was rejected during sizing.
        reject_reason: Human-readable rejection reason.
        reject_stage: Stage where rejection occurred (e.g., "depth_gate").
    """

    target_notional: Decimal = Decimal("0")
    target_qty: Decimal = Decimal("0")
    depth_ob: OrderbookDepthSnapshot | None = None
    depth_cap_metrics: dict[str, Any] = field(default_factory=dict)
    rejected: bool = False
    reject_reason: str | None = None
    reject_stage: str | None = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


async def _fetch_balances(
    lighter: "LighterExchangeAdapter",
    x10: "X10ExchangeAdapter",
) -> BalanceData:
    """
    Fetch balance data from both exchanges.

    Args:
        lighter: Lighter exchange adapter instance.
        x10: X10 exchange adapter instance.

    Returns:
        BalanceData with total and available balances from both exchanges.
    """
    lighter_balance = await lighter.get_available_balance()
    x10_balance = await x10.get_available_balance()

    return BalanceData(
        lighter_total=lighter_balance.total,
        lighter_available=lighter_balance.available,
        x10_total=x10_balance.total,
        x10_available=x10_balance.available,
    )


def _calculate_leverage(
    trade: Trade,
    settings: "Settings",
    market_data: "MarketDataService",
) -> LeverageConfig:
    """
    Calculate effective leverage for both exchanges.

    Effective leverage is capped at the exchange's IMR limit to prevent
    margin calls and ensure positions can be maintained.

    Args:
        trade: Trade object with symbol information.
        settings: Application settings with user leverage config.
        market_data: Market data service for IMR limits.

    Returns:
        LeverageConfig with effective leverage for both exchanges.
    """
    lighter_info = market_data.get_market_info(trade.symbol, Exchange.LIGHTER)
    x10_info = market_data.get_market_info(trade.symbol, Exchange.X10)

    lighter_imr_limit = lighter_info.lighter_max_leverage if lighter_info else Decimal("3")
    x10_imr_limit = x10_info.x10_max_leverage if x10_info else Decimal("5")

    lighter_user_lev = settings.trading.leverage_multiplier
    x10_user_lev = getattr(settings.trading, "x10_leverage_multiplier", None) or lighter_user_lev

    eff_lighter = min(lighter_user_lev, lighter_imr_limit)
    eff_x10 = min(x10_user_lev, x10_imr_limit)

    return LeverageConfig(
        lighter_imr_limit=lighter_imr_limit,
        x10_imr_limit=x10_imr_limit,
        lighter_user_lev=lighter_user_lev,
        x10_user_lev=x10_user_lev,
        eff_lighter=eff_lighter,
        eff_x10=eff_x10,
    )


async def _calculate_risk_capacity(
    balance: BalanceData,
    leverage: LeverageConfig,
    settings: "Settings",
    store: Any,
    current_trade_id: str,
) -> RiskCapacity:
    """
    Calculate risk and liquidity capacity.

    Determines the maximum exposure available for new trades based on:
    - Global risk cap (equity * max_exposure_pct * leverage)
    - Current exposure from existing trades
    - Available margin on each exchange

    Args:
        balance: Current balance data from both exchanges.
        leverage: Effective leverage configuration.
        settings: Application settings with risk parameters.
        store: Trade store for querying active trades.
        current_trade_id: ID of current trade (excluded from exposure calc).

    Returns:
        RiskCapacity with all calculated limits and available exposure.
    """
    max_exp_pct = settings.risk.max_exposure_pct / Decimal("100.0")
    global_risk_cap = balance.total_equity * max_exp_pct * leverage.eff_lighter

    active_trades_all = await store.list_open_trades()
    active_other_trades = [t for t in active_trades_all if t.trade_id != current_trade_id]
    current_exposure = sum(t.target_notional_usd for t in active_other_trades)

    remaining_risk_cap = max(Decimal("0"), global_risk_cap - current_exposure)

    max_lighter_pos = balance.lighter_available * leverage.eff_lighter * Decimal("0.70")
    max_x10_pos = balance.x10_available * leverage.eff_x10 * Decimal("0.95")
    liquidity_cap = min(max_lighter_pos, max_x10_pos)

    available_exposure = min(remaining_risk_cap, liquidity_cap)

    return RiskCapacity(
        global_risk_cap=global_risk_cap,
        current_exposure=current_exposure,
        remaining_risk_cap=remaining_risk_cap,
        max_lighter_pos=max_lighter_pos,
        max_x10_pos=max_x10_pos,
        liquidity_cap=liquidity_cap,
        available_exposure=available_exposure,
    )


def _calculate_slot_allocation(
    risk: RiskCapacity,
    settings: "Settings",
    opp: Opportunity,
    num_other_trades: int,
) -> SlotAllocation:
    """
    Calculate slot-based allocation.

    Divides available exposure across remaining trade slots to ensure
    diversification. The final target is capped by risk limits and
    the opportunity's suggested notional.

    Args:
        risk: Calculated risk capacity with available exposure.
        settings: Application settings with slot and risk limits.
        opp: Opportunity with suggested notional size.
        num_other_trades: Number of other active trades.

    Returns:
        SlotAllocation with target notional for this trade.
    """
    max_open = settings.trading.max_open_trades
    remaining_slots = max(1, max_open - num_other_trades)
    slot_based_size = risk.available_exposure / Decimal(remaining_slots)

    max_trade_pct_raw = getattr(settings.risk, "max_trade_size_pct", Decimal("100.0"))
    max_trade_pct = max_trade_pct_raw / Decimal("100.0")
    risk_based_cap = risk.global_risk_cap * max_trade_pct

    target_notional = min(slot_based_size, risk_based_cap)

    desired_cap = _safe_decimal(
        getattr(settings.trading, "desired_notional_usd", None),
        Decimal("1e18"),
    )
    target_notional = min(target_notional, desired_cap, opp.suggested_notional)

    return SlotAllocation(
        remaining_slots=remaining_slots,
        slot_based_size=slot_based_size,
        risk_based_cap=risk_based_cap,
        target_notional=target_notional,
    )


def _load_depth_gate_config(settings: "Settings") -> DepthGateConfig:
    """
    Load depth gate configuration from settings.

    Extracts all depth gate parameters and applies preflight multipliers
    if strict preflight mode is enabled.

    Args:
        settings: Application settings with trading and execution config.

    Returns:
        DepthGateConfig with all parameters for liquidity gating.
    """
    ts = settings.trading
    es = settings.execution

    enabled = getattr(ts, "depth_gate_enabled", False)
    gate_mode = getattr(ts, "depth_gate_mode", "L1")
    levels = int(getattr(ts, "depth_gate_levels", 20) or 20)
    max_impact = _safe_decimal(getattr(ts, "depth_gate_max_price_impact_percent", None), Decimal("0.0015"))

    strict_preflight = bool(getattr(es, "hedge_depth_preflight_enabled", False))
    multiplier = _safe_decimal(getattr(es, "hedge_depth_preflight_multiplier", None), Decimal("1.0"))
    if multiplier <= 1:
        multiplier = Decimal("1.0")

    base_min_usd = _safe_decimal(getattr(ts, "min_l1_notional_usd", None), Decimal("0"))
    base_min_mult = _safe_decimal(getattr(ts, "min_l1_notional_multiple", None), Decimal("0"))
    base_max_util = _safe_decimal(getattr(ts, "max_l1_qty_utilization", None), Decimal("1.0"))

    if strict_preflight:
        sizing_min_usd = base_min_usd * multiplier if base_min_usd > 0 else Decimal("0")
        sizing_min_mult = base_min_mult * multiplier if base_min_mult > 0 else Decimal("0")
        sizing_max_util = base_max_util
        if sizing_max_util and sizing_max_util < 1 and multiplier > 1:
            sizing_max_util = sizing_max_util / multiplier
    else:
        sizing_min_usd = base_min_usd
        sizing_min_mult = base_min_mult
        sizing_max_util = base_max_util

    return DepthGateConfig(
        enabled=enabled,
        mode=gate_mode,
        levels=levels,
        max_price_impact_pct=max_impact,
        strict_preflight=strict_preflight,
        multiplier=multiplier,
        min_l1_notional_usd=sizing_min_usd,
        min_l1_notional_multiple=sizing_min_mult,
        max_l1_qty_utilization=sizing_max_util,
    )


async def _apply_depth_cap(
    target_notional: Decimal,
    trade: Trade,
    mid_price: Decimal,
    fresh_ob: OrderbookSnapshot,
    prefetched_depth_ob: OrderbookDepthSnapshot | None,
    depth_config: DepthGateConfig,
    market_data: "MarketDataService",
) -> tuple[Decimal, OrderbookDepthSnapshot | None, dict[str, Any], bool, str | None]:
    """
    Apply depth cap to target notional.

    Returns: (capped_notional, depth_ob, metrics, rejected, reject_reason)
    """
    if not depth_config.enabled:
        return target_notional, prefetched_depth_ob, {}, False, None

    use_impact = depth_config.mode == "IMPACT"
    depth_ob = prefetched_depth_ob

    if use_impact and depth_ob is None:
        try:
            depth_ob = await market_data.get_fresh_orderbook_depth(
                trade.symbol,
                levels=depth_config.levels,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch depth for impact sizing cap for {trade.symbol}: {e}")
            depth_ob = None

    if use_impact and depth_ob is not None:
        cap = calculate_depth_cap_by_impact(
            depth_ob,
            lighter_side=trade.leg1.side,
            x10_side=trade.leg2.side,
            mid_price=mid_price,
            min_l1_notional_usd=depth_config.min_l1_notional_usd,
            min_l1_notional_multiple=depth_config.min_l1_notional_multiple,
            max_l1_qty_utilization=depth_config.max_l1_qty_utilization,
            max_price_impact_pct=depth_config.max_price_impact_pct,
            safety_buffer=Decimal("0.98"),
        )
    else:
        cap = calculate_l1_depth_cap(
            fresh_ob,
            lighter_side=trade.leg1.side,
            x10_side=trade.leg2.side,
            mid_price=mid_price,
            min_l1_notional_usd=depth_config.min_l1_notional_usd,
            min_l1_notional_multiple=depth_config.min_l1_notional_multiple,
            max_l1_qty_utilization=depth_config.max_l1_qty_utilization,
            safety_buffer=Decimal("0.98"),
        )

    metrics = cap.metrics or {}

    if not cap.passed:
        public_error = "Insufficient hedge depth" if depth_config.strict_preflight else "Insufficient L1 depth"
        return target_notional, depth_ob, metrics, True, f"{public_error}: {cap.reason}"

    if cap.max_notional_usd > 0:
        before_cap = target_notional
        capped = min(target_notional, cap.max_notional_usd)
        if capped < before_cap:
            logger.info(
                f"Depth-aware sizing for {trade.symbol}: "
                f"${before_cap:.2f} -> ${capped:.2f} (L1 cap ${cap.max_notional_usd:.2f})"
            )
        return capped, depth_ob, metrics, False, None

    return target_notional, depth_ob, metrics, False, None


def _calculate_mid_price(opp: Opportunity, fresh_ob: OrderbookSnapshot) -> Decimal:
    """
    Calculate mid price from opportunity or orderbook.

    Uses opportunity's mid_price if available, otherwise calculates
    average of all available bid/ask prices from both exchanges.

    Args:
        opp: Opportunity with optional mid_price.
        fresh_ob: Fresh orderbook snapshot with exchange prices.

    Returns:
        Mid price as Decimal, or 0 if no prices available.
    """
    mid_price = opp.mid_price
    if mid_price <= 0:
        mids = [
            fresh_ob.lighter_bid,
            fresh_ob.lighter_ask,
            fresh_ob.x10_bid,
            fresh_ob.x10_ask,
        ]
        mids = [p for p in mids if p and p > 0]
        mid_price = sum(mids) / Decimal(str(len(mids))) if mids else Decimal("0")
    return mid_price


async def _apply_margin_adjustment(
    trade: Trade,
    mid_price: Decimal,
    leverage: LeverageConfig,
    lighter_available: Decimal,
    calculate_quantity_fn: Any,
    opp: Opportunity,
) -> None:
    """Adjust sizing if Lighter margin is insufficient."""
    lev = leverage.eff_lighter
    if lev <= 0:
        lev = Decimal("1.0")

    req_margin = trade.target_notional_usd / lev
    if lighter_available < req_margin:
        logger.warning(
            f"Sizing reduced by Lighter Margin: {lighter_available} * Lev"
        )
        new_notional = lighter_available * lev * Decimal("0.95")
        trade.target_qty = new_notional / mid_price
        qty = await calculate_quantity_fn(trade, opp)
        trade.target_qty = qty
        if mid_price > 0:
            trade.target_notional_usd = trade.target_qty * mid_price
        trade.leg1.qty = qty
        trade.leg2.qty = qty


# =============================================================================
# MAIN FUNCTION (ORCHESTRATOR)
# =============================================================================


async def _execute_impl_sizing(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
    fresh_ob: OrderbookSnapshot,
    prefetched_depth_ob: OrderbookDepthSnapshot | None,
) -> OrderbookDepthSnapshot | ExecutionResult | None:
    """
    Execute position sizing for a trade.

    Orchestrates: balance fetch → leverage calc → risk capacity → slot allocation
    → depth cap → sanity checks → final sizing.
    """
    # 1. Fetch balances
    balance = await _fetch_balances(self.lighter, self.x10)

    # 2. Calculate leverage
    leverage = _calculate_leverage(trade, self.settings, self.market_data)
    logger.info(
        f"Leverage Sizing for {trade.symbol}: "
        f"Lighter={leverage.eff_lighter}x (Limit {leverage.lighter_imr_limit}x), "
        f"X10={leverage.eff_x10}x (Limit {leverage.x10_imr_limit}x)"
    )

    # 3. Calculate risk capacity
    risk = await _calculate_risk_capacity(
        balance, leverage, self.settings, self.store, trade.trade_id
    )
    logger.info(
        f"Sizing Check: Equity=${balance.total_equity:.2f} "
        f"RiskCap=${risk.global_risk_cap:.2f} Used=${risk.current_exposure:.2f} "
        f"-> RemRisk=${risk.remaining_risk_cap:.2f}. "
        f"LiquidityCap=${risk.liquidity_cap:.2f} "
        f"(L-85%:${risk.max_lighter_pos:.2f}/X-95%:${risk.max_x10_pos:.2f}) "
        f"-> FINAL AVAILABLE=${risk.available_exposure:.2f}"
    )

    # 4. Calculate slot allocation
    active_trades = await self.store.list_open_trades()
    num_other = len([t for t in active_trades if t.trade_id != trade.trade_id])
    allocation = _calculate_slot_allocation(risk, self.settings, opp, num_other)

    # 5. Calculate mid price
    mid_price = _calculate_mid_price(opp, fresh_ob)

    # 6. Apply depth cap
    depth_config = _load_depth_gate_config(self.settings)
    target_notional, depth_ob, depth_metrics, rejected, reject_reason = await _apply_depth_cap(
        allocation.target_notional,
        trade,
        mid_price,
        fresh_ob,
        prefetched_depth_ob,
        depth_config,
        self.market_data,
    )

    if rejected:
        logger.warning(f"Rejecting {opp.symbol}: cannot size to depth gates ({reject_reason})")
        trade.status = TradeStatus.REJECTED
        trade.execution_state = ExecutionState.ABORTED
        trade.error = reject_reason
        await self._kpi_update_attempt(
            attempt_id,
            {
                "status": ExecutionAttemptStatus.REJECTED,
                "stage": "REJECTED_DEPTH_CAP",
                "reason": reject_reason,
                "data": {"depth_cap": depth_metrics},
            },
        )
        return ExecutionResult(success=False, trade=trade, error=reject_reason)

    # 7. Sanity check: minimum trade size
    if target_notional < Decimal("20.0"):
        logger.warning(
            f"Insufficient capacity for min trade size: Target=${target_notional:.2f} (Min $20)"
        )
        trade.status = TradeStatus.REJECTED
        trade.execution_state = ExecutionState.ABORTED
        trade.error = "Insufficient capacity for min trade size"
        await self._kpi_update_attempt(
            attempt_id,
            {
                "status": ExecutionAttemptStatus.REJECTED,
                "stage": "REJECTED_MIN_TRADE_SIZE",
                "reason": "Insufficient capacity for min trade size",
                "data": {"depth_cap": depth_metrics},
            },
        )
        return ExecutionResult(success=False, trade=trade, error="Insufficient capacity")

    # 8. Log sizing result
    logger.info(
        f"Dynamic Sizing Result: Slots={allocation.remaining_slots} "
        f"Available=${risk.available_exposure:.2f} "
        f"SlotSize=${allocation.slot_based_size:.2f} "
        f"MaxTradeCap=${allocation.risk_based_cap:.2f} "
        f"-> Target=${target_notional:.2f}"
    )

    # 9. Apply target notional
    if mid_price > 0:
        trade.target_notional_usd = target_notional
        trade.target_qty = target_notional / mid_price

    # 10. Calculate final quantity (with step size rounding)
    qty = await self._calculate_quantity(trade, opp)
    trade.target_qty = qty
    if mid_price > 0:
        trade.target_notional_usd = trade.target_qty * mid_price
    trade.leg1.qty = qty
    trade.leg2.qty = qty

    # 11. Record KPI
    await self._kpi_update_attempt(
        attempt_id,
        {
            "stage": "SIZED",
            "data": {
                "eff_lev_lighter": str(leverage.eff_lighter),
                "eff_lev_x10": str(leverage.eff_x10),
                "target_notional_usd": str(trade.target_notional_usd),
                "target_qty": str(trade.target_qty),
                "remaining_slots": allocation.remaining_slots,
                "depth_cap": depth_metrics,
            },
        },
    )

    # 12. Final margin re-check
    await _apply_margin_adjustment(
        trade, mid_price, leverage, balance.lighter_available,
        self._calculate_quantity, opp
    )

    return depth_ob
