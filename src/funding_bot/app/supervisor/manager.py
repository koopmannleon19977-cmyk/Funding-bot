"""
Supervisor facade with orchestration state and method wiring.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from funding_bot.app.supervisor.guards import (
    _check_account_guards,
    _compact_error,
    _fetch_balances_with_retry,
    _is_trading_paused,
    _maybe_resume_trading,
    _on_broken_hedge,
    _pause_trading,
)
from funding_bot.app.supervisor.lifecycle import (
    _close_adapters,
    _close_infrastructure,
    _init_adapters,
    _init_infrastructure,
    _start_services,
    _stop_services,
    start,
    stop,
)
from funding_bot.app.supervisor.loops import (
    _adapter_health_loop,
    _check_ws_readiness,
    _execute_opportunity,
    _handle_failed_trade,
    _handle_successful_trade,
    _heartbeat_loop,
    _historical_data_loop,
    _opportunity_loop,
    _opportunity_loop_iteration,
    _position_management_loop,
    _start_loops,
)
from funding_bot.app.supervisor.tasks import (
    _cancel_all_tasks,
    _create_task,
    _handle_task_done,
    _restart_task_after_delay,
)
from funding_bot.config.settings import Settings


class Supervisor:
    """
    Central orchestrator for all bot services.

    Manages lifecycle of:
    - MarketDataService
    - OpportunityEngine
    - ExecutionEngine
    - PositionManager
    - FundingTracker
    - Reconciler
    - HistoricalIngestionService (optional, for APY analysis)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Core components
        self.lighter: Any | None = None
        self.x10: Any | None = None
        self.store: Any | None = None
        self.event_bus: Any | None = None
        self.notifier: Any | None = None

        # Services
        self.market_data: Any | None = None
        self.opportunity_engine: Any | None = None
        self.execution_engine: Any | None = None
        self.position_manager: Any | None = None
        self.funding_tracker: Any | None = None
        self.reconciler: Any | None = None
        self.shutdown_service: Any | None = None
        self.notification_service: Any | None = None
        self.historical_ingestion: Any | None = None

        # Background tasks (supervised)
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_factories: dict[str, Any] = {}
        self._task_restart_attempts: dict[str, int] = {}
        self._task_restart_jobs: dict[str, asyncio.Task] = {}

        # Shutdown flag for coordinated stop
        self._stopping = False

        # Risk guards / circuit breaker (stops NEW trades only)
        self._risk_lock = asyncio.Lock()
        self._trading_paused_until: float | None = None  # epoch seconds; float("inf") = manual intervention
        self._trading_pause_reason: str | None = None
        self._consecutive_trade_failures = 0
        self._start_equity: Decimal | None = None
        self._peak_equity: Decimal | None = None
        self._last_total_equity: Decimal | None = None
        self._last_free_margin_pct: Decimal | None = None
        self._broken_hedge_symbol: str | None = None  # Symbol that caused broken hedge (for self-healing)

        # Stats
        self._stats = {
            "trades_opened": 0,
            "trades_closed": 0,
            "opportunities_scanned": 0,
            "errors": 0,
            "rejections": 0,
        }

    @property
    def is_running(self) -> bool:
        """Check if supervisor is running."""
        return self._running and not self._stopping

    _pause_trading = _pause_trading
    _compact_error = _compact_error
    _on_broken_hedge = _on_broken_hedge
    _fetch_balances_with_retry = _fetch_balances_with_retry
    _maybe_resume_trading = _maybe_resume_trading
    _is_trading_paused = _is_trading_paused
    _check_account_guards = _check_account_guards

    start = start
    stop = stop
    _init_infrastructure = _init_infrastructure
    _init_adapters = _init_adapters
    _start_services = _start_services
    _start_loops = _start_loops
    _stop_services = _stop_services
    _close_adapters = _close_adapters
    _close_infrastructure = _close_infrastructure

    _opportunity_loop = _opportunity_loop
    _opportunity_loop_iteration = _opportunity_loop_iteration
    _execute_opportunity = _execute_opportunity
    _handle_successful_trade = _handle_successful_trade
    _handle_failed_trade = _handle_failed_trade
    _check_ws_readiness = _check_ws_readiness
    _position_management_loop = _position_management_loop
    _heartbeat_loop = _heartbeat_loop
    _historical_data_loop = _historical_data_loop
    _adapter_health_loop = _adapter_health_loop

    _create_task = _create_task
    _handle_task_done = _handle_task_done
    _restart_task_after_delay = _restart_task_after_delay
    _cancel_all_tasks = _cancel_all_tasks


def trigger_position_check(self: "Supervisor") -> None:
    """
    Trigger immediate position check (event-driven wakeup).

    🚀 PERFORMANCE: This method replaces excessive polling by allowing
    external components to signal when position checks are needed.

    Call this when:
    - Significant price changes occur
    - Funding rates update
    - Trade events happen
    - Manual intervention needed

    Example:
        await supervisor.trigger_position_check()
    """
    if hasattr(self, '_position_check_event'):
        self._position_check_event.set()


# Assign as method to class
Supervisor.trigger_position_check = trigger_position_check  # type: ignore[method-assign]
