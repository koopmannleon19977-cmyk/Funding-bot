"""
Execution flow helpers (moved from ExecutionEngine).
"""

from __future__ import annotations

from funding_bot.domain.models import Opportunity, Trade
from funding_bot.services.execution_leg1_attempts import _execute_leg1_attempts
from funding_bot.services.execution_leg1_finalize import _finalize_leg1_execution
from funding_bot.services.execution_leg1_prep import _prepare_leg1_execution


async def _execute_leg1(
    self,
    trade: Trade,
    opp: Opportunity,
    *,
    attempt_id: str | None = None,
) -> None:
    """Execute Leg 1 (Lighter - Maker) with Dynamic Linear Repricing."""
    config, state = await _prepare_leg1_execution(
        self,
        trade,
        opp,
        attempt_id=attempt_id,
    )
    await _execute_leg1_attempts(
        self,
        trade,
        opp,
        config,
        state,
        attempt_id=attempt_id,
    )
    await _finalize_leg1_execution(self, trade, state)
