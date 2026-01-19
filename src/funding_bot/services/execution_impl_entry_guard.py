"""
Execution entry guards for execution_impl.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from funding_bot.domain.models import (
    ExecutionAttemptStatus,
    ExecutionState,
    Opportunity,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.services.execution_types import ExecutionResult
from funding_bot.services.liquidity_gates import (
    check_depth_for_entry_by_impact,
    check_l1_depth_for_entry,
)
from funding_bot.services.market_data import (
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
)
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger("funding_bot.services.execution")


async def _execute_impl_entry_guard(
    self,
    opp: Opportunity,
    trade: Trade,
    attempt_id: str,
    fresh_ob: OrderbookSnapshot,
    prefetched_depth_ob: OrderbookDepthSnapshot | None,
) -> ExecutionResult | None:
    # Final entry guard: L1 depth/liquidity (avoid APY traps / thin books)
    ts = self.settings.trading
    gate_mode = getattr(ts, "depth_gate_mode", "L1")
    use_impact = gate_mode == "IMPACT"
    depth_levels = int(getattr(ts, "depth_gate_levels", 20) or 20)
    depth_impact = _safe_decimal(getattr(ts, "depth_gate_max_price_impact_percent", None), Decimal("0.0015"))
    depth_ob = prefetched_depth_ob

    if getattr(ts, "depth_gate_enabled", False):
        if use_impact and depth_ob is not None:
            depth_result = check_depth_for_entry_by_impact(
                depth_ob,
                lighter_side=trade.leg1.side,
                x10_side=trade.leg2.side,
                target_qty=trade.target_qty,
                target_notional_usd=trade.target_notional_usd,
                min_l1_notional_usd=getattr(ts, "min_l1_notional_usd", Decimal("0")),
                min_l1_notional_multiple=getattr(ts, "min_l1_notional_multiple", Decimal("0")),
                max_l1_qty_utilization=getattr(ts, "max_l1_qty_utilization", Decimal("1.0")),
                max_price_impact_pct=depth_impact,
            )
        else:
            depth_result = check_l1_depth_for_entry(
                fresh_ob,
                lighter_side=trade.leg1.side,
                x10_side=trade.leg2.side,
                target_qty=trade.target_qty,
                target_notional_usd=trade.target_notional_usd,
                min_l1_notional_usd=getattr(ts, "min_l1_notional_usd", Decimal("0")),
                min_l1_notional_multiple=getattr(ts, "min_l1_notional_multiple", Decimal("0")),
                max_l1_qty_utilization=getattr(ts, "max_l1_qty_utilization", Decimal("1.0")),
            )
        if not depth_result.passed:
            logger.warning(f"Rejecting {opp.symbol}: insufficient depth ({depth_result.reason})")
            trade.status = TradeStatus.REJECTED
            trade.execution_state = ExecutionState.ABORTED
            trade.error = f"Insufficient depth: {depth_result.reason}"
            await self._kpi_update_attempt(
                attempt_id,
                {
                    "status": ExecutionAttemptStatus.REJECTED,
                    "stage": "REJECTED_DEPTH",
                    "reason": depth_result.reason,
                    "data": {"depth": depth_result.metrics or {}},
                },
            )
            return ExecutionResult(success=False, trade=trade, error="Insufficient depth")
        await self._kpi_update_attempt(
            attempt_id,
            {"data": {"depth": depth_result.metrics or {}}},
        )

    # Hedge depth preflight (stricter than base depth gate)
    # Goal: avoid placing Leg1 and then having to rollback because hedge-side L1 vanishes.
    es = self.settings.execution
    if getattr(ts, "depth_gate_enabled", False) and getattr(es, "hedge_depth_preflight_enabled", False):
        mult = _safe_decimal(getattr(es, "hedge_depth_preflight_multiplier", None), Decimal("1.0"))
        if mult <= 1:
            mult = Decimal("1.0")
        checks = int(getattr(es, "hedge_depth_preflight_checks", 1) or 1)
        checks = max(1, min(checks, 5))
        delay_s = float(_safe_decimal(getattr(es, "hedge_depth_preflight_delay_seconds", None), Decimal("0.0")))
        delay_s = max(0.0, min(delay_s, 5.0))

        # Scale thresholds up (more strict) and utilization down (more strict).
        base_min_usd = _safe_decimal(getattr(ts, "min_l1_notional_usd", None), Decimal("0"))
        base_min_mult = _safe_decimal(getattr(ts, "min_l1_notional_multiple", None), Decimal("0"))
        base_max_util = _safe_decimal(getattr(ts, "max_l1_qty_utilization", None), Decimal("1.0"))

        strict_min_usd = base_min_usd * mult if base_min_usd > 0 else Decimal("0")
        strict_min_mult = base_min_mult * mult if base_min_mult > 0 else Decimal("0")
        strict_max_util = base_max_util
        if strict_max_util and strict_max_util < 1 and mult > 1:
            strict_max_util = strict_max_util / mult

        strict_metrics: list[dict[str, object]] = []
        strict_ok = True
        strict_reason = ""

        for i in range(checks):
            if use_impact:
                try:
                    depth_i = (
                        depth_ob
                        if i == 0 and depth_ob is not None
                        else await self.market_data.get_fresh_orderbook_depth(
                            opp.symbol,
                            levels=depth_levels,
                        )
                    )
                    strict = check_depth_for_entry_by_impact(
                        depth_i,
                        lighter_side=trade.leg1.side,
                        x10_side=trade.leg2.side,
                        target_qty=trade.target_qty,
                        target_notional_usd=trade.target_notional_usd,
                        min_l1_notional_usd=strict_min_usd,
                        min_l1_notional_multiple=strict_min_mult,
                        max_l1_qty_utilization=strict_max_util,
                        max_price_impact_pct=depth_impact,
                    )
                except Exception as e:
                    logger.debug(f"Depth preflight fallback to L1 for {opp.symbol}: {e}")
                    ob = fresh_ob if i == 0 else await self._get_best_effort_orderbook(opp.symbol, max_age_seconds=1.0)
                    strict = check_l1_depth_for_entry(
                        ob,
                        lighter_side=trade.leg1.side,
                        x10_side=trade.leg2.side,
                        target_qty=trade.target_qty,
                        target_notional_usd=trade.target_notional_usd,
                        min_l1_notional_usd=strict_min_usd,
                        min_l1_notional_multiple=strict_min_mult,
                        max_l1_qty_utilization=strict_max_util,
                    )
            else:
                ob = fresh_ob if i == 0 else await self._get_best_effort_orderbook(opp.symbol, max_age_seconds=1.0)
                strict = check_l1_depth_for_entry(
                    ob,
                    lighter_side=trade.leg1.side,
                    x10_side=trade.leg2.side,
                    target_qty=trade.target_qty,
                    target_notional_usd=trade.target_notional_usd,
                    min_l1_notional_usd=strict_min_usd,
                    min_l1_notional_multiple=strict_min_mult,
                    max_l1_qty_utilization=strict_max_util,
                )
            strict_metrics.append(
                {
                    "check": i + 1,
                    "passed": strict.passed,
                    "reason": strict.reason,
                    "depth": strict.metrics or {},
                }
            )
            if not strict.passed:
                strict_ok = False
                strict_reason = strict.reason
                break
            if delay_s and i < checks - 1:
                await asyncio.sleep(delay_s)

        await self._kpi_update_attempt(
            attempt_id,
            {
                "data": {
                    "hedge_depth_preflight": {
                        "enabled": True,
                        "multiplier": str(mult),
                        "checks": checks,
                        "delay_seconds": str(Decimal(str(delay_s))),
                        "thresholds": {
                            "min_l1_notional_usd": str(strict_min_usd),
                            "min_l1_notional_multiple": str(strict_min_mult),
                            "max_l1_qty_utilization": str(strict_max_util),
                        },
                        "results": strict_metrics,
                    }
                }
            },
        )

        if not strict_ok:
            logger.warning(f"Rejecting {opp.symbol}: insufficient hedge depth (preflight strict) ({strict_reason})")
            trade.status = TradeStatus.REJECTED
            trade.execution_state = ExecutionState.ABORTED
            trade.error = f"Insufficient hedge depth (preflight strict): {strict_reason}"
            await self._kpi_update_attempt(
                attempt_id,
                {
                    "status": ExecutionAttemptStatus.REJECTED,
                    "stage": "REJECTED_HEDGE_DEPTH_PRE",
                    "reason": strict_reason,
                    "data": {"hedge_depth_preflight": strict_metrics},
                },
            )
            return ExecutionResult(success=False, trade=trade, error="Insufficient hedge depth")

    return None
