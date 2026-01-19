"""
Execution post-leg1 logic for execution_impl.

Refactored for maintainability - extracted dataclasses and helper functions.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from funding_bot.domain.models import (
    ExecutionAttemptStatus,
    Opportunity,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_helpers import _safe_bool
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.services.liquidity_gates import (
    check_x10_depth_compliance_by_impact,
    check_x10_l1_compliance,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

if TYPE_CHECKING:
    from funding_bot.domain.models import OrderbookSnapshot

logger = get_logger("funding_bot.services.execution")


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class DepthGateConfig:
    """Configuration for depth gate checks."""

    enabled: bool = False
    mode: str = "L1"  # "L1" or "IMPACT"
    levels: int = 20
    max_price_impact_pct: Decimal = Decimal("0.0015")
    min_l1_notional_usd: Decimal = Decimal("0")
    min_l1_notional_multiple: Decimal = Decimal("0")
    max_l1_qty_utilization: Decimal = Decimal("1.0")
    use_impact_post_leg1: bool = False


@dataclass
class SalvageConfig:
    """Configuration for hedge depth salvage mode."""

    enabled: bool = False
    min_fraction: Decimal = Decimal("0")
    close_slippage: Decimal = Decimal("0.002")
    close_timeout: float = 6.0
    dust_usd: Decimal = Decimal("0")


@dataclass
class SalvageResult:
    """Result of salvage operation."""

    success: bool = False
    closed_qty: Decimal = Decimal("0")
    close_fee: Decimal = Decimal("0")
    extra_cost: Decimal = Decimal("0")
    new_open_qty: Decimal = Decimal("0")
    error: str | None = None


@dataclass
class DepthCheckResult:
    """Result of depth compliance check."""

    passed: bool = False
    reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _load_depth_gate_config(settings: Any) -> DepthGateConfig:
    """Load depth gate configuration from settings."""
    ts = settings.trading
    es = settings.execution

    gate_mode = getattr(ts, "depth_gate_mode", "L1")
    use_impact = gate_mode == "IMPACT"

    fast_hedge_enabled = _safe_bool(getattr(es, "hedge_submit_immediately_on_fill_enabled", None), False)
    post_leg1_impact_enabled = _safe_bool(getattr(es, "hedge_post_leg1_impact_depth_enabled", None), False)
    use_impact_post_leg1 = use_impact and (not fast_hedge_enabled or post_leg1_impact_enabled)

    return DepthGateConfig(
        enabled=getattr(ts, "depth_gate_enabled", False),
        mode=gate_mode,
        levels=int(getattr(ts, "depth_gate_levels", 20) or 20),
        max_price_impact_pct=_safe_decimal(getattr(ts, "depth_gate_max_price_impact_percent", None), Decimal("0.0015")),
        min_l1_notional_usd=_safe_decimal(getattr(ts, "min_l1_notional_usd", None), Decimal("0")),
        min_l1_notional_multiple=_safe_decimal(getattr(ts, "min_l1_notional_multiple", None), Decimal("0")),
        max_l1_qty_utilization=_safe_decimal(getattr(ts, "max_l1_qty_utilization", None), Decimal("1.0")),
        use_impact_post_leg1=use_impact_post_leg1,
    )


def _load_salvage_config(settings: Any) -> SalvageConfig:
    """Load salvage mode configuration from settings."""
    es = settings.execution

    return SalvageConfig(
        enabled=_safe_bool(getattr(es, "hedge_depth_salvage_enabled", None), False),
        min_fraction=_safe_decimal(getattr(es, "hedge_depth_salvage_min_fraction", None), Decimal("0")),
        close_slippage=_safe_decimal(getattr(es, "hedge_depth_salvage_close_slippage", None), Decimal("0.002")),
        close_timeout=float(
            _safe_decimal(
                getattr(es, "hedge_depth_salvage_close_fill_timeout_seconds", None),
                Decimal("6.0"),
            )
        ),
        dust_usd=_safe_decimal(getattr(es, "microfill_max_unhedged_usd", None), Decimal("0")),
    )


async def _check_depth_compliance(
    self,
    trade: Trade,
    x10_ob: OrderbookSnapshot,
    cfg: DepthGateConfig,
) -> DepthCheckResult:
    """Check X10 depth compliance using configured mode."""
    if cfg.use_impact_post_leg1:
        try:
            depth_book = await self._get_best_effort_x10_depth_snapshot(
                trade.symbol,
                levels=cfg.levels,
                max_age_seconds=1.0,
            )
            result = check_x10_depth_compliance_by_impact(
                depth_book,
                x10_side=trade.leg2.side,
                target_qty=trade.target_qty,
                target_notional_usd=trade.target_notional_usd,
                min_l1_notional_usd=cfg.min_l1_notional_usd,
                min_l1_notional_multiple=cfg.min_l1_notional_multiple,
                max_l1_qty_utilization=cfg.max_l1_qty_utilization,
                max_price_impact_pct=cfg.max_price_impact_pct,
            )
        except Exception as e:
            logger.warning(f"Depth hedge gate fallback to X10 L1 for {trade.symbol} (post-leg1): {e}")
            result = check_x10_l1_compliance(
                x10_ob,
                x10_side=trade.leg2.side,
                target_qty=trade.target_qty,
                target_notional_usd=trade.target_notional_usd,
                min_l1_notional_usd=cfg.min_l1_notional_usd,
                min_l1_notional_multiple=cfg.min_l1_notional_multiple,
                max_l1_qty_utilization=cfg.max_l1_qty_utilization,
            )
    else:
        # L1-only mode
        result = check_x10_l1_compliance(
            x10_ob,
            x10_side=trade.leg2.side,
            target_qty=trade.target_qty,
            target_notional_usd=trade.target_notional_usd,
            min_l1_notional_usd=cfg.min_l1_notional_usd,
            min_l1_notional_multiple=cfg.min_l1_notional_multiple,
            max_l1_qty_utilization=cfg.max_l1_qty_utilization,
        )

    return DepthCheckResult(
        passed=result.passed,
        reason=result.reason,
        metrics=result.metrics or {},
    )


def _calculate_salvage_quantities(
    trade: Trade,
    depth_result: DepthCheckResult,
    x10_ob: OrderbookSnapshot,
    depth_cfg: DepthGateConfig,
) -> tuple[Decimal, Decimal, Decimal, bool]:
    """
    Calculate salvage quantities based on available depth.

    Returns: (available_qty, cap_qty, desired_open_qty, can_proceed)
    """
    mx = (depth_result.metrics or {}).get("x10") or {}
    avail_qty = _safe_decimal(
        mx.get("available_qty", None) or mx.get("l1_qty", None),
        Decimal("0"),
    )
    ref_price = _safe_decimal(
        mx.get("best_price", None) or mx.get("l1_price", None),
        Decimal("0"),
    )
    avail_notional = _safe_decimal(
        mx.get("available_notional_usd", None) or mx.get("l1_notional_usd", None),
        Decimal("0"),
    )

    if avail_qty <= 0 or ref_price <= 0:
        # Fallback to fresh snapshot values
        ref_price = x10_ob.x10_bid if trade.leg2.side == Side.SELL else x10_ob.x10_ask
        avail_qty = x10_ob.x10_bid_qty if trade.leg2.side == Side.SELL else x10_ob.x10_ask_qty
        avail_notional = Decimal("0")

    util = depth_cfg.max_l1_qty_utilization
    multiple = depth_cfg.min_l1_notional_multiple
    min_abs_notional = depth_cfg.min_l1_notional_usd

    # Check minimum notional
    depth_notional = avail_notional if avail_notional > 0 else (ref_price * avail_qty)
    if min_abs_notional > 0 and depth_notional < min_abs_notional:
        return avail_qty, Decimal("0"), Decimal("0"), False

    # Calculate cap_qty
    cap_qty = avail_qty
    if util > 0 and util < 1:
        cap_qty = min(cap_qty, avail_qty * util)
    if multiple and multiple > 0:
        cap_qty = min(cap_qty, avail_qty / multiple)

    desired_full_qty = trade.leg1.filled_qty
    desired_open_qty = min(desired_full_qty, cap_qty)

    return avail_qty, cap_qty, desired_open_qty, True


async def _execute_salvage_close(
    self,
    trade: Trade,
    remainder_qty: Decimal,
    salvage_cfg: SalvageConfig,
) -> SalvageResult:
    """Execute salvage close orders to reduce Leg1 position."""
    dust_qty = (salvage_cfg.dust_usd / trade.leg1.entry_price) if trade.leg1.entry_price > 0 else Decimal("0")

    closed_qty_total = Decimal("0")
    close_fee_total = Decimal("0")
    extra_cost_total = Decimal("0")
    current_remainder = remainder_qty

    for _ in range(3):
        if current_remainder <= max(Decimal("0"), dust_qty):
            break

        filled_close = await self._place_lighter_taker_ioc(
            symbol=trade.symbol,
            side=trade.leg1.side.inverse(),
            qty=current_remainder,
            slippage=salvage_cfg.close_slippage,
            reduce_only=True,
            timeout_seconds=salvage_cfg.close_timeout,
            purpose="hedge_depth_salvage_flatten",
        )

        if not filled_close or filled_close.filled_qty <= 0:
            break

        closed_qty_total += filled_close.filled_qty
        close_fee_total += filled_close.fee

        # Conservative accounting: treat adverse price move as additional cost
        if trade.leg1.side == Side.BUY:
            pnl_wo_fee = (filled_close.avg_fill_price - trade.leg1.entry_price) * filled_close.filled_qty
        else:
            pnl_wo_fee = (trade.leg1.entry_price - filled_close.avg_fill_price) * filled_close.filled_qty
        if pnl_wo_fee < 0:
            extra_cost_total += -pnl_wo_fee

        current_remainder -= filled_close.filled_qty
        await asyncio.sleep(0.1)

    # Check if salvage succeeded
    if current_remainder > max(Decimal("0"), dust_qty):
        return SalvageResult(
            success=False,
            closed_qty=closed_qty_total,
            close_fee=close_fee_total,
            extra_cost=extra_cost_total,
            error=f"Could not shrink Leg1 sufficiently (remaining={current_remainder:.6f})",
        )

    return SalvageResult(
        success=True,
        closed_qty=closed_qty_total,
        close_fee=close_fee_total,
        extra_cost=extra_cost_total,
        new_open_qty=trade.leg1.filled_qty - closed_qty_total,
    )


async def _handle_salvage_failure(
    self,
    trade: Trade,
    attempt_id: str,
    depth_result: DepthCheckResult,
    salvage_data: dict[str, Any],
) -> ExecutionResult:
    """Handle salvage failure - rollback and update status."""
    logger.warning("SALVAGE FAILED: could not shrink Leg1 sufficiently. Rolling back.")
    await self._rollback(trade, reason="Hedge depth salvage failed (could not shrink Leg1)")
    trade.status = TradeStatus.FAILED
    trade.error = f"Insufficient hedge depth: {depth_result.reason}"
    await self._update_trade(trade)
    await self._kpi_update_attempt(
        attempt_id,
        {
            "status": ExecutionAttemptStatus.FAILED,
            "stage": "FAILED_HEDGE_DEPTH_SALVAGE",
            "reason": depth_result.reason,
            "data": {
                "depth_post_leg1": depth_result.metrics or {},
                "salvage": salvage_data,
            },
        },
    )
    return ExecutionResult(success=False, trade=trade, error="Insufficient hedge depth")


async def _apply_salvage_adjustments(
    self,
    trade: Trade,
    opp: Opportunity,
    attempt_id: str,
    salvage_result: SalvageResult,
    depth_result: DepthCheckResult,
    desired_full_qty: Decimal,
    cap_qty: Decimal,
    fraction: Decimal,
) -> None:
    """Apply accounting adjustments after successful salvage."""
    trade.leg1.fees += salvage_result.close_fee + salvage_result.extra_cost
    new_open_qty = salvage_result.new_open_qty
    trade.target_qty = new_open_qty
    trade.target_notional_usd = abs(opp.mid_price * new_open_qty)
    trade.leg1.qty = new_open_qty
    trade.leg1.filled_qty = new_open_qty
    trade.leg2.qty = new_open_qty
    await self._update_trade(trade)

    await self._kpi_update_attempt(
        attempt_id,
        {
            "data": {
                "hedge_depth_salvage": {
                    "reason": depth_result.reason,
                    "desired_full_qty": str(desired_full_qty),
                    "target_open_qty": str(new_open_qty),
                    "cap_qty": str(cap_qty),
                    "fraction": str(fraction),
                    "closed_qty_total": str(salvage_result.closed_qty),
                    "close_fee_total": str(salvage_result.close_fee),
                    "extra_cost_total": str(salvage_result.extra_cost),
                }
            }
        },
    )


async def _handle_depth_failure(
    self,
    trade: Trade,
    attempt_id: str,
    depth_result: DepthCheckResult,
) -> ExecutionResult:
    """Handle depth check failure - rollback and update status."""
    logger.warning(
        f"CRITICAL DEPTH: {trade.symbol} insufficient hedge depth ({depth_result.reason}). "
        "ABORTING LEG 2 AND ROLLING BACK LEG 1."
    )
    await self._rollback(trade, reason="Insufficient hedge depth")
    trade.status = TradeStatus.FAILED
    trade.error = f"Insufficient hedge depth: {depth_result.reason}"
    await self._update_trade(trade)
    await self._kpi_update_attempt(
        attempt_id,
        {
            "status": ExecutionAttemptStatus.FAILED,
            "stage": "FAILED_HEDGE_DEPTH",
            "reason": depth_result.reason,
            "data": {"depth_post_leg1": depth_result.metrics or {}},
        },
    )
    return ExecutionResult(success=False, trade=trade, error="Insufficient hedge depth")


def _calculate_realized_spread(
    trade: Trade,
    hedge_price: Decimal,
) -> Decimal:
    """Calculate realized spread between Leg1 entry and current hedge price."""
    if trade.leg1.side == Side.BUY:
        return (trade.leg1.entry_price - hedge_price) / trade.leg1.entry_price
    else:
        return (hedge_price - trade.leg1.entry_price) / trade.leg1.entry_price


async def _handle_slippage_failure(
    self,
    trade: Trade,
    attempt_id: str,
    realized_spread: Decimal,
    risk_limit: Decimal,
    hedge_price: Decimal,
) -> ExecutionResult:
    """Handle slippage check failure - rollback and update status."""
    logger.warning(
        f"CRITICAL SLIPPAGE: {trade.symbol} realized spread "
        f"{realized_spread:.4%} > limit {risk_limit:.4%}. "
        "ABORTING LEG 2 AND ROLLING BACK LEG 1."
    )
    await self._rollback(trade, reason="Leg2 slippage too high")
    trade.status = TradeStatus.FAILED
    trade.error = f"Slippage too high: {realized_spread:.4%}"
    await self._update_trade(trade)
    await self._kpi_update_attempt(
        attempt_id,
        {
            "status": ExecutionAttemptStatus.FAILED,
            "stage": "FAILED_POST_LEG1_GUARD",
            "reason": "Leg2 slippage too high",
            "realized_spread_post_leg1_pct": realized_spread,
            "data": {
                "risk_limit_pct": str(risk_limit),
                "hedge_price": str(hedge_price),
            },
        },
    )
    return ExecutionResult(success=False, trade=trade, error="Leg2 Slippage too high")


async def _execute_impl_post_leg1(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
    max_spread: Decimal,
) -> tuple[ExecutionResult | None, float]:
    """
    Execute post-leg1 checks and guards.

    This function:
    1. Executes Leg1 and records timing
    2. Checks hedge depth compliance (L1 or IMPACT mode)
    3. Attempts salvage if depth is insufficient
    4. Validates realized spread against risk limits

    Returns:
        (None, leg1_filled_t) on success to continue to Leg2
        (ExecutionResult, 0.0) on failure with error details
    """
    # Step 1: Execute Leg 1 (Lighter - Maker)
    leg1_t0 = time.monotonic()
    await self._execute_leg1(trade, opp, attempt_id=attempt_id)
    leg1_filled_t = time.monotonic()
    leg1_seconds = Decimal(str(max(0.0, time.monotonic() - leg1_t0)))

    await self._kpi_update_attempt(
        attempt_id,
        {
            "stage": "LEG1_FILLED",
            "leg1_fill_seconds": leg1_seconds,
            "data": {
                "leg1_order_id": trade.leg1.order_id,
                "leg1_filled_qty": str(trade.leg1.filled_qty),
                "leg1_entry_price": str(trade.leg1.entry_price),
                "leg1_fees": str(trade.leg1.fees),
            },
        },
    )

    # Early return if nothing filled
    if trade.leg1.filled_qty <= 0:
        return None, leg1_filled_t

    # Step 2: Fetch fresh X10 orderbook for hedging
    x10_ob = await self._get_best_effort_x10_orderbook(trade.symbol, max_age_seconds=1.0)

    # Step 3: Load configurations
    depth_cfg = _load_depth_gate_config(self.settings)
    salvage_cfg = _load_salvage_config(self.settings)

    # Step 4: Check depth compliance if enabled
    if depth_cfg.enabled:
        depth_result = await _check_depth_compliance(self, trade, x10_ob, depth_cfg)

        if not depth_result.passed:
            # Attempt salvage if conditions are met
            can_salvage = (
                salvage_cfg.enabled
                and isinstance(depth_result.reason, str)
                and "X10" in depth_result.reason
                and trade.leg1.filled_qty > 0
            )

            if can_salvage:
                # Calculate salvage quantities
                _, cap_qty, desired_open_qty, can_proceed = _calculate_salvage_quantities(
                    trade, depth_result, x10_ob, depth_cfg
                )

                if not can_proceed:
                    can_salvage = False

                if can_salvage:
                    desired_full_qty = trade.leg1.filled_qty
                    fraction = (desired_open_qty / desired_full_qty) if desired_full_qty > 0 else Decimal("0")

                    # Check if salvage is viable
                    if (
                        desired_open_qty > 0
                        and desired_open_qty < desired_full_qty
                        and fraction >= salvage_cfg.min_fraction
                    ):
                        remainder_qty = desired_full_qty - desired_open_qty
                        logger.warning(
                            f"CRITICAL DEPTH: {trade.symbol} insufficient hedge depth "
                            f"({depth_result.reason}). SALVAGE: shrinking Leg1 by "
                            f"{remainder_qty:.6f} to hedgeable qty {desired_open_qty:.6f} "
                            f"(cap={cap_qty:.6f})."
                        )

                        # Execute salvage close orders
                        salvage_result = await _execute_salvage_close(self, trade, remainder_qty, salvage_cfg)

                        if not salvage_result.success:
                            return await _handle_salvage_failure(
                                self,
                                trade,
                                attempt_id,
                                depth_result,
                                {
                                    "desired_full_qty": str(desired_full_qty),
                                    "desired_open_qty": str(desired_open_qty),
                                    "cap_qty": str(cap_qty),
                                    "error": salvage_result.error,
                                },
                            ), 0.0

                        # Apply successful salvage adjustments
                        await _apply_salvage_adjustments(
                            self,
                            trade,
                            opp,
                            attempt_id,
                            salvage_result,
                            depth_result,
                            desired_full_qty,
                            cap_qty,
                            fraction,
                        )
                    else:
                        can_salvage = False

            # If salvage not possible/viable, fail the trade
            if not can_salvage:
                return await _handle_depth_failure(self, trade, attempt_id, depth_result), 0.0

    # Step 5: Check realized spread (slippage guard)
    hedge_p = x10_ob.x10_bid if trade.leg2.side == Side.SELL else x10_ob.x10_ask
    realized_spread = _calculate_realized_spread(trade, hedge_p)

    logger.info(
        f"Post-Leg1 Guard: {trade.symbol} "
        f"Leg1={trade.leg1.entry_price:.6f} "
        f"Current_Hedge={hedge_p:.6f} "
        f"Realized_Spread={realized_spread:.4%}"
    )

    risk_limit = max_spread * Decimal("1.2")
    if realized_spread > risk_limit:
        return await _handle_slippage_failure(self, trade, attempt_id, realized_spread, risk_limit, hedge_p), 0.0

    # Step 6: Record successful guard passage
    await self._kpi_update_attempt(
        attempt_id,
        {
            "realized_spread_post_leg1_pct": realized_spread,
            "data": {
                "post_leg1_hedge_price": str(hedge_p),
                "post_leg1_risk_limit_pct": str(risk_limit),
            },
        },
    )

    return None, leg1_filled_t
