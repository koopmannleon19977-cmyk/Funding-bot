"""
Opportunity engine facade.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from funding_bot.config.settings import Settings
from funding_bot.services.market_data import MarketDataService
from funding_bot.services.opportunities.diagnostics import (
    _log_zero_scan_diagnostics,
    _reserve_candidate_log,
    _reserve_scan_log,
    _scan_signature,
)
from funding_bot.services.opportunities.evaluator import (
    _evaluate_symbol,
    _evaluate_symbol_with_reject_info,
)
from funding_bot.services.opportunities.filters import _apply_filters, _is_tradfi_or_fx
from funding_bot.services.opportunities.scanner import (
    calculate_switching_cost,
    get_best_opportunity,
    mark_cooldown,
    mark_rotation_cooldown,
    scan,
    should_rotate_to,
)
from funding_bot.services.opportunities.scoring import (
    _calculate_breakeven_hours,
    _calculate_expected_value,
    _calculate_liquidity_score,
)
from funding_bot.services.opportunities.sizing import _get_available_equity_for_sizing


class OpportunityEngine:
    """
    Scans for and evaluates funding arbitrage opportunities.

    Filter Pipeline:
    1. Blacklist check
    2. Spread limit
    3. Minimum APY
    4. Liquidity score
    5. Expected value (including fees)
    6. Breakeven time
    """

    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataService,
        store,
    ):
        self.settings = settings
        self.market_data = market_data
        self.store = store
        self.cooldowns: dict[str, datetime] = {}
        self.rotation_cooldowns: dict[str, datetime] = {}  # Phase 4: Separate rotation cooldown
        self._last_zero_scan_log_at: datetime | None = None
        self._last_scan_log_at: datetime | None = None
        self._last_scan_signature: tuple[str, str, str] | None = None
        self._last_candidate_log_at: dict[str, datetime] = {}
        self._scan_log_lock = asyncio.Lock()
        self._candidate_log_lock = asyncio.Lock()
        # Phase 4 Orderbook Optimization: Symbol failure tracking
        self._symbol_failures: dict[str, int] = {}  # Consecutive failures per symbol
        self._symbol_cooldown: dict[str, datetime] = {}  # Temporary skip cooldown
        self._max_symbol_failures: int = 5  # Max failures before cooldown
        self._symbol_cooldown_minutes: int = 10  # Cooldown duration

    mark_cooldown = mark_cooldown
    mark_rotation_cooldown = mark_rotation_cooldown  # Phase 4: Rotation cooldown
    scan = scan
    get_best_opportunity = get_best_opportunity
    calculate_switching_cost = calculate_switching_cost  # Phase 4: Rotation cost calculation
    should_rotate_to = should_rotate_to  # Phase 4: Rotation decision logic
    _scan_signature = _scan_signature
    _reserve_scan_log = _reserve_scan_log
    _reserve_candidate_log = _reserve_candidate_log
    _log_zero_scan_diagnostics = _log_zero_scan_diagnostics
    _get_available_equity_for_sizing = _get_available_equity_for_sizing
    _evaluate_symbol = _evaluate_symbol
    _evaluate_symbol_with_reject_info = _evaluate_symbol_with_reject_info
    _apply_filters = _apply_filters
    _is_tradfi_or_fx = _is_tradfi_or_fx
    _calculate_expected_value = _calculate_expected_value
    _calculate_breakeven_hours = _calculate_breakeven_hours
    _calculate_liquidity_score = _calculate_liquidity_score

    # =========================================================================
    # PHASE 4: SYMBOL FAILURE TRACKING (Orderbook Optimization)
    # =========================================================================

    def should_skip_symbol(self, symbol: str) -> bool:
        """
        Check if a symbol should be skipped due to too many consecutive failures.

        Symbols enter a temporary cooldown after max_symbol_failures consecutive
        failures (e.g., empty orderbooks, API timeouts). This prevents wasting
        API calls on low-volume or problematic symbols.

        Returns:
            True if symbol should be skipped, False otherwise.
        """
        # Check if in cooldown
        if symbol in self._symbol_cooldown:
            if datetime.now(UTC) < self._symbol_cooldown[symbol]:
                return True
            # Cooldown expired - remove and reset failures
            del self._symbol_cooldown[symbol]
            self._symbol_failures.pop(symbol, None)

        # Check failure count
        return self._symbol_failures.get(symbol, 0) >= self._max_symbol_failures

    def record_symbol_failure(self, symbol: str) -> None:
        """
        Record a failure for a symbol (e.g., empty orderbook, fetch timeout).

        After max_symbol_failures consecutive failures, the symbol enters
        a temporary cooldown to avoid wasting resources.
        """
        from funding_bot.observability.logging import get_logger

        logger = get_logger(__name__)

        self._symbol_failures[symbol] = self._symbol_failures.get(symbol, 0) + 1
        failures = self._symbol_failures[symbol]

        if failures >= self._max_symbol_failures:
            cooldown_until = datetime.now(UTC) + timedelta(minutes=self._symbol_cooldown_minutes)
            self._symbol_cooldown[symbol] = cooldown_until
            logger.warning(
                f"[{symbol}] Entering {self._symbol_cooldown_minutes}min cooldown "
                f"after {failures} consecutive failures (until {cooldown_until.isoformat()})"
            )

    def record_symbol_success(self, symbol: str) -> None:
        """
        Record a successful evaluation for a symbol.

        Resets the failure counter so symbol can continue being scanned.
        """
        if symbol in self._symbol_failures:
            del self._symbol_failures[symbol]
        # Don't clear cooldown - let it expire naturally
