"""
Liquidity/Depth gates.

We intentionally keep this in the services layer:
- It operates on OrderbookSnapshot (a service DTO)
- It is used by OpportunitiesService validation and ExecutionEngine

Goal: avoid illiquid markets where the "funding edge" is eaten by spread/fees/slippage,
and reduce unhedged/rollback scenarios due to thin books.
"""

from __future__ import annotations

from .liquidity_gates_config import get_depth_gate_config
from .liquidity_gates_impact_core import (
    estimate_entry_spread_pct_by_impact,
    estimate_taker_vwap_price_by_impact,
)
from .liquidity_gates_impact_gates import (
    calculate_depth_cap_by_impact,
    check_depth_for_entry_by_impact,
    check_x10_depth_compliance_by_impact,
)
from .liquidity_gates_l1 import (
    calculate_l1_depth_cap,
    check_l1_depth_for_entry,
    check_x10_l1_compliance,
)
from .liquidity_gates_preflight import (
    PreflightLiquidityConfig,
    PreflightLiquidityResult,
    check_preflight_liquidity,
)
from .liquidity_gates_types import DepthVWAPResult, L1DepthCapResult, L1DepthGateResult

__all__ = [
    "DepthVWAPResult",
    "L1DepthCapResult",
    "L1DepthGateResult",
    "PreflightLiquidityConfig",
    "PreflightLiquidityResult",
    "calculate_depth_cap_by_impact",
    "calculate_l1_depth_cap",
    "check_depth_for_entry_by_impact",
    "check_l1_depth_for_entry",
    "check_preflight_liquidity",
    "check_x10_depth_compliance_by_impact",
    "check_x10_l1_compliance",
    "estimate_entry_spread_pct_by_impact",
    "estimate_taker_vwap_price_by_impact",
    "get_depth_gate_config",
]
