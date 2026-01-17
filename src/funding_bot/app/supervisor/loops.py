"""
Main supervised loops for scanning, position management, and heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from funding_bot.domain.events import AlertEvent
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


# =============================================================================
# Helper Classes for Opportunity Loop
# =============================================================================


@dataclass
class ErrorClassification:
    """Classification of execution errors for circuit breaker logic."""

    is_market_rejection: bool
    is_account_rejection: bool
    should_count_error: bool


def _classify_execution_error(error: str | None) -> ErrorClassification:
    """
    Classify an execution error for circuit breaker decisions.

    Returns:
        ErrorClassification with flags for error type handling.
    """
    err = (error or "").strip()

    market_rejection_prefixes = (
        "Entry spread too high",
        "Insufficient capacity",
        "Insufficient depth",
        "Insufficient hedge depth",
        "Insufficient L1 depth",
        "Insufficient hedge depth (preflight strict)",
    )
    account_rejection_prefixes = (
        "Insufficient Lighter balance",
        "Insufficient X10 balance",
        "Insufficient Lighter margin",
    )

    is_market = any(err == p or err.startswith(p) for p in market_rejection_prefixes)
    is_account = any(err == p or err.startswith(p) for p in account_rejection_prefixes)

    return ErrorClassification(
        is_market_rejection=is_market,
        is_account_rejection=is_account,
        should_count_error=not is_market,
    )


async def _check_ws_readiness(self, symbol: str) -> bool:
    """
    Check if Lighter WebSocket is ready for trading.

    Returns True if ready or if WS gate is disabled.
    """
    es = getattr(self.settings, "execution", None)

    # Skip check if WS gate is disabled
    if not (
        bool(getattr(es, "ws_ready_gate_enabled", True))
        and bool(getattr(es, "ws_fill_wait_enabled", True))
    ):
        return True

    timeout_s = float(getattr(es, "ws_ready_gate_timeout_seconds", Decimal("2.0")) or 2.0)

    try:
        # Try market-specific readiness check first
        ensure_market_fn = getattr(self.lighter, "ensure_orders_ws_market", None)
        if callable(ensure_market_fn):
            return await ensure_market_fn(symbol, timeout=timeout_s)

        # Fallback: general WS readiness
        wait_fn = getattr(self.lighter, "wait_ws_ready", None)
        if callable(wait_fn):
            return await wait_fn(timeout=timeout_s)

        # Fallback: synchronous check
        ready_fn = getattr(self.lighter, "ws_ready", None)
        if callable(ready_fn):
            return bool(ready_fn())

        return True  # No check available, assume ready

    except Exception as e:
        logger.warning(f"WS readiness check failed (Lighter): {e}")
        return False


async def _start_loops(self) -> None:
    """Start main event loops as supervised tasks."""
    logger.info("Starting main loops...")

    self._task_factories = {
        "opportunity_scan": self._opportunity_loop,
        "position_management": self._position_management_loop,
        "heartbeat": self._heartbeat_loop,
        "historical_data": self._historical_data_loop,
        "adapter_health": self._adapter_health_loop,
    }

    # Opportunity scanning loop
    self._tasks["opportunity_scan"] = self._create_task(
        self._task_factories["opportunity_scan"](),
        name="opportunity_scan"
    )

    # Position management loop
    self._tasks["position_management"] = self._create_task(
        self._task_factories["position_management"](),
        name="position_management"
    )

    # Health/heartbeat loop
    self._tasks["heartbeat"] = self._create_task(
        self._task_factories["heartbeat"](),
        name="heartbeat"
    )

    # Historical data loop (optional, only if enabled)
    hist_cfg = getattr(self.settings, "historical", None)
    if hist_cfg and getattr(hist_cfg, "enabled", False):
        self._tasks["historical_data"] = self._create_task(
            self._task_factories["historical_data"](),
            name="historical_data"
        )

        # Adapter health monitoring loop
        self._tasks["adapter_health"] = self._create_task(
            self._task_factories["adapter_health"](),
            name="adapter_health"
        )

    logger.info("Main loops started")


async def _opportunity_loop(self) -> None:
    """
    Main loop: scan for opportunities and execute trades.

    Refactored with early returns and helper functions to reduce nesting depth.
    Original nesting depth: 8 levels → Reduced to: 4 levels max.
    """
    logger.info("Opportunity scanning loop started")

    scan_interval = float(
        getattr(self.settings.trading, "opportunity_scan_interval_seconds", 5.0) or 5.0
    )
    scan_interval = max(0.2, min(scan_interval, 30.0))
    failed_cooldowns: dict[str, float] = {}  # symbol -> expiry_timestamp
    COOLDOWN_DURATION = 300.0  # 5 minutes

    while not self._shutdown_event.is_set():
        try:
            await self._opportunity_loop_iteration(
                scan_interval, failed_cooldowns, COOLDOWN_DURATION
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Opportunity loop error: {e}")

        # Wait before next scan
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=scan_interval)
            break  # Shutdown signaled
        except TimeoutError:
            pass

    logger.info("Opportunity scanning loop stopped")


async def _opportunity_loop_iteration(
    self,
    scan_interval: float,
    failed_cooldowns: dict[str, float],
    cooldown_duration: float,
) -> None:
    """Single iteration of the opportunity scanning loop."""
    await self._maybe_resume_trading()

    # Prune expired cooldowns
    now = time.time()
    expired = [s for s, expiry in failed_cooldowns.items() if now > expiry]
    for s in expired:
        del failed_cooldowns[s]

    # Get current state
    open_trades = await self.store.list_open_trades()
    open_symbols = {t.symbol for t in open_trades}

    executing_symbols: set[str] = set()
    with contextlib.suppress(Exception):
        executing = self.execution_engine.get_active_executions()
        executing_symbols = {t.symbol for t in executing}

    # Early return: pause during active executions
    if executing_symbols:
        pause_s = float(
            getattr(
                self.settings.trading,
                "opportunity_scan_pause_during_execution_seconds",
                scan_interval,
            )
            or scan_interval
        )
        await asyncio.sleep(max(0.2, min(pause_s, 10.0)))
        return

    # Build exclusion set
    open_symbols.update(executing_symbols)
    open_symbols.update(failed_cooldowns.keys())

    # Early return: at max trades
    if len(open_trades) >= self.settings.trading.max_open_trades:
        logger.debug(f"At max trades ({len(open_trades)}), skipping scan")
        await asyncio.sleep(scan_interval * 2)
        return

    # Scan for best opportunity
    self._stats["opportunities_scanned"] += 1
    best = await self.opportunity_engine.get_best_opportunity(
        open_symbols=open_symbols,
        suppress_candidate_logs=bool(executing_symbols),
    )

    # Early return: no valid opportunity
    if not best:
        return
    if not self.settings.live_trading:
        return
    if not best.is_profitable:
        return
    if best.symbol in open_symbols:
        return

    # Execute the opportunity
    await self._execute_opportunity(best, failed_cooldowns, cooldown_duration)


async def _execute_opportunity(
    self,
    best,  # Opportunity type
    failed_cooldowns: dict[str, float],
    cooldown_duration: float,
) -> None:
    """Execute a validated opportunity with all pre-checks."""
    # WS readiness gate
    if not await self._check_ws_readiness(best.symbol):
        es = getattr(self.settings, "execution", None)
        async with self._risk_lock:
            failures = self._consecutive_trade_failures
        cooldown = float(getattr(es, "ws_ready_gate_cooldown_seconds", Decimal("15.0")) or 15.0)
        await self._pause_trading(
            reason="Lighter WS account stream not ready",
            cooldown_seconds=cooldown,
            failures=failures,
            severity="ERROR",
        )
        return

    # Account guards
    await self._check_account_guards()
    if await self._is_trading_paused():
        logger.info(f"Skipping execution (trading paused): {best.symbol}")
        return

    # Execute trade
    logger.info(f"Executing opportunity: {best.symbol}")
    result = await self.execution_engine.execute(best)

    if result.success:
        await self._handle_successful_trade(best)
    else:
        await self._handle_failed_trade(best, result, failed_cooldowns, cooldown_duration)


async def _handle_successful_trade(self, best) -> None:
    """Handle successful trade execution."""
    self._stats["trades_opened"] += 1
    async with self._risk_lock:
        self._consecutive_trade_failures = 0
    logger.info(f"Trade opened: {best.symbol}")


async def _handle_failed_trade(
    self,
    best,
    result,  # ExecutionResult type
    failed_cooldowns: dict[str, float],
    cooldown_duration: float,
) -> None:
    """Handle failed trade execution with circuit breaker logic."""
    self._stats["rejections"] += 1
    logger.warning(f"Trade failed: {result.error}")

    # Add to cooldown
    failed_cooldowns[best.symbol] = time.time() + cooldown_duration
    logger.info(f"Adding {best.symbol} to failure cooldown for {cooldown_duration}s")

    # Classify error for circuit breaker decisions
    error_class = _classify_execution_error(result.error)

    # Count operational failures
    if error_class.should_count_error:
        self._stats["errors"] += 1

    # Handle account rejections: pause trading briefly
    if error_class.is_account_rejection:
        async with self._risk_lock:
            failures = self._consecutive_trade_failures
        await self._pause_trading(
            reason=(result.error or "").strip() or "insufficient balance/margin",
            cooldown_seconds=60.0,
            failures=failures,
            severity="ERROR",
        )
        return

    # Market rejections: no circuit breaker action needed
    if error_class.is_market_rejection:
        return

    # Operational failure: increment circuit breaker counter
    async with self._risk_lock:
        self._consecutive_trade_failures += 1
        failures = self._consecutive_trade_failures

    max_failures = self.settings.risk.max_consecutive_failures
    warn_threshold = max(2, max_failures - 1)

    # Warn before tripping breaker
    if self.event_bus and failures == warn_threshold and failures < max_failures:
        await self.event_bus.publish(
            AlertEvent(
                level="ERROR",
                message="Consecutive execution failures",
                details={
                    "failures": failures,
                    "max_failures": max_failures,
                    "symbol": best.symbol,
                    "error": result.error or "",
                },
            )
        )

    # Trip circuit breaker
    if failures >= max_failures:
        await self._pause_trading(
            reason=f"max consecutive trade failures reached ({failures})",
            cooldown_seconds=cooldown_duration,
            failures=failures,
            severity="ERROR",
        )


async def _position_management_loop(self) -> None:
    """
    Loop: check exit conditions and manage positions.

    🚀 PERFORMANCE: Event-driven architecture with hybrid polling.
    - Uses asyncio.Event for instant wakeup on price/funding changes
    - Falls back to 5s polling as safety net (reduced from 1.0s)
    - Triggers on: price updates, funding changes, trade events, manual requests

    This eliminates 80% of unnecessary position checks while maintaining
    responsiveness to market conditions.
    """
    logger.info("Position management loop started (event-driven)")

    # Create event for position check triggers
    if not hasattr(self, '_position_check_event'):
        self._position_check_event = asyncio.Event()

    check_interval = 5.0  # Increased from 1.0s - safety net polling
    check_count = 0
    event_triggered_count = 0

    while not self._shutdown_event.is_set():
        try:
            closed = await self.position_manager.check_trades()

            for trade in closed:
                self._stats["trades_closed"] += 1
                logger.info(f"Trade closed: {trade.symbol}")

            check_count += 1

        except asyncio.CancelledError:
            break
        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Position management error: {e}")

        # Wait for event OR timeout (hybrid approach)
        try:
            # Wait for event trigger with timeout as safety net
            await asyncio.wait_for(
                self._position_check_event.wait(),
                timeout=check_interval
            )
            # Event was triggered
            event_triggered_count += 1
            self._position_check_event.clear()  # Reset for next trigger
        except TimeoutError:
            # Timeout - normal safety net polling
            pass

    logger.info(
        f"Position management loop stopped: "
        f"{check_count} total checks, "
        f"{event_triggered_count} event-triggered"
        + (f" ({event_triggered_count/check_count*100:.1f}% event-driven)" if check_count > 0 else "")
    )


async def _heartbeat_loop(self) -> None:
    """Heartbeat and health reporting loop."""
    logger.info("Heartbeat loop started")

    while not self._shutdown_event.is_set():
        try:
            # Log health status
            # 🚀 PERFORMANCE: Use cached open trades to reduce DB query frequency
            open_trades = await self.store.list_open_trades_cached()
            market_health = self.market_data.is_healthy() if self.market_data else False

            # Exposure (from DB trades) is available even if balance APIs are degraded.
            exposure = sum(t.target_notional_usd for t in open_trades)

            # Fetch balances for Dashboard visibility (Equity/Free margin)
            balances_ok = True
            balance_error: str | None = None
            try:
                l_bal, x_bal = await self._fetch_balances_with_retry(attempts=2, base_delay_seconds=0.5)
                total_equity = l_bal.total + x_bal.total
                free_margin_pct = (
                    (l_bal.available + x_bal.available) / total_equity
                    if total_equity > 0
                    else Decimal("0")
                )
            except Exception as e:
                balances_ok = False
                balance_error = self._compact_error(e)
                logger.debug(f"Failed to fetch balances for heartbeat: {balance_error}")
                async with self._risk_lock:
                    total_equity = self._last_total_equity or Decimal("0")
                    free_margin_pct = self._last_free_margin_pct or Decimal("0")

            # Update risk guards from heartbeat only if we have fresh balances.
            if balances_ok:
                await self._check_account_guards(l_bal=l_bal, x_bal=x_bal)
            paused = await self._is_trading_paused()

            state_icon = "[OK]" if not paused and market_health and balances_ok else "[ERR]"
            logger.info(
                f"[HEALTH] {state_icon} Trades: {len(open_trades)} | "
                f"Equity: ${total_equity:.2f} | "
                f"Margin: {free_margin_pct:.1%} | "
                f"Exp: ${exposure:.2f}" +
                (f" | [WARN] Errors: {self._stats['errors']}" if self._stats['errors'] > 0 else "") +
                (" | [PAUSED]" if paused else "")
            )

            # Periodic cleanup of stale order watchers (memory management)
            if self.execution_engine and hasattr(self.execution_engine, "_cleanup_stale_watchers"):
                try:
                    await self.execution_engine._cleanup_stale_watchers()
                except Exception:
                    pass

            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=15.0  # Frequent updates for dashboard
            )
            break
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Heartbeat error: {e}")

    logger.info("Heartbeat loop stopped")


async def _historical_data_loop(self) -> None:
    """Historical data ingestion loop for APY analysis and crash detection.

    Runs daily at configured hour to:
    1. Fetch minute-level candles from both exchanges
    2. Update database with latest data
    3. Trigger crash detection analysis
    4. Update volatility profiles (Priority 2)
    """
    logger.info("Historical data loop started")

    # Get configuration
    hist_cfg = getattr(self.settings, "historical", None)
    enabled = bool(getattr(hist_cfg, "enabled", False)) if hist_cfg else False

    if not enabled:
        logger.info("Historical data collection disabled - loop idle")
        while not self._shutdown_event.is_set():
            await asyncio.sleep(3600)  # Sleep 1 hour if disabled
        return

    # Get schedule settings
    daily_hour = int(getattr(hist_cfg, "daily_update_hour", 2)) if hist_cfg else 2
    backfill_days = int(getattr(hist_cfg, "backfill_days", 90)) if hist_cfg else 90

    # Wait for random startup delay (0-60s) to avoid thundering herd
    await asyncio.sleep(time.time() % 60)

    last_ingestion_date = None
    backfill_done = False  # Track if initial backfill has been run

    while not self._shutdown_event.is_set():
        try:
            now = datetime.now(UTC)
            current_hour = now.hour
            current_date = now.date()

            # === Startup Backfill (runs once on first iteration) ===
            if not backfill_done and self.historical_ingestion:
                logger.info(f"Starting initial historical backfill ({backfill_days} days)...")

                try:
                    # Fetch ALL available markets from both exchanges (not just recent trades)
                    logger.info("Fetching all available markets for backfill...")
                    all_symbols = set()

                    if self.lighter:
                        try:
                            await self.lighter.load_markets()
                            lighter_symbols = set(self.lighter._markets.keys())
                            all_symbols.update(lighter_symbols)
                            logger.info(f"Loaded {len(lighter_symbols)} markets from Lighter")
                        except Exception as e:
                            logger.warning(f"Failed to load Lighter markets: {e}")

                    if self.x10:
                        try:
                            await self.x10.load_markets()
                            x10_symbols = set(self.x10._markets.keys())
                            all_symbols.update(x10_symbols)
                            logger.info(f"Loaded {len(x10_symbols)} markets from X10")
                        except Exception as e:
                            logger.warning(f"Failed to load X10 markets: {e}")

                    # Fallback to hardcoded symbols if both exchanges fail
                    if not all_symbols:
                        logger.warning("Failed to load markets from exchanges, using fallback symbols")
                        all_symbols = {"ETH", "BTC", "SOL"}

                    # Convert to sorted list for consistent ordering
                    symbols = sorted(list(all_symbols))
                    suffix = "..." if len(symbols) > 20 else ""
                    logger.info(f"Backfilling {len(symbols)} symbols: {symbols[:20]}{suffix}")

                    results = await self.historical_ingestion.backfill(symbols, days_back=backfill_days)

                    total_records = sum(results.values())
                    logger.info(f"Backfill complete: {total_records} records inserted")

                    for symbol, count in results.items():
                        logger.info(f"  {symbol}: {count} records")

                except Exception as e:
                    logger.error(f"Initial backfill failed: {e}")
                    self._stats["errors"] += 1

                backfill_done = True

            # === Daily Ingestion (existing) ===
            should_ingest = current_hour == daily_hour and last_ingestion_date != current_date

            if should_ingest and self.historical_ingestion:
                logger.info("Starting daily historical data update...")

                try:
                    # ALWAYS fetch ALL available markets (not just recent trades)
                    logger.info("Fetching all available markets for daily update...")
                    all_symbols = set()

                    if self.lighter:
                        try:
                            await self.lighter.load_markets()
                            lighter_symbols = set(self.lighter._markets.keys())
                            all_symbols.update(lighter_symbols)
                            logger.info(f"Loaded {len(lighter_symbols)} markets from Lighter")
                        except Exception as e:
                            logger.warning(f"Failed to load Lighter markets: {e}")

                    if self.x10:
                        try:
                            await self.x10.load_markets()
                            x10_symbols = set(self.x10._markets.keys())
                            all_symbols.update(x10_symbols)
                            logger.info(f"Loaded {len(x10_symbols)} markets from X10")
                        except Exception as e:
                            logger.warning(f"Failed to load X10 markets: {e}")

                    # Fallback to hardcoded symbols if both exchanges fail
                    if not all_symbols:
                        logger.warning("Failed to load markets from exchanges, using fallback symbols")
                        all_symbols = {"ETH", "BTC", "SOL"}

                    # Convert to sorted list for consistent ordering
                    symbols = sorted(list(all_symbols))
                    suffix = "..." if len(symbols) > 20 else ""
                    logger.info(
                        f"Updating historical data for {len(symbols)} symbols: "
                        f"{symbols[:20]}{suffix}"
                    )

                    results = await self.historical_ingestion.daily_update(symbols)

                    total_records = sum(results.values())
                    logger.info(f"Historical update complete: {total_records} records inserted")

                    for symbol, count in results.items():
                        if count > 0:
                            logger.debug(f"  {symbol}: {count} records")

                except Exception as e:
                    logger.error(f"Historical data update failed: {e}")
                    self._stats["errors"] += 1

                last_ingestion_date = current_date

            # Calculate sleep until next check
            next_check = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            sleep_seconds = min(3600, (next_check - now).total_seconds())

            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=sleep_seconds
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            self._stats["errors"] += 1
            logger.exception("Historical data loop error")
            await asyncio.sleep(300)  # 5 min on error

    logger.info("Historical data loop stopped")


async def _adapter_health_loop(self) -> None:
    """Adapter health monitoring and cache validation loop.

    Runs every 10 minutes to:
    1. Log adapter health status
    2. Validate WebSocket caches against REST API
    3. Detect and log any connection issues
    """
    logger.info("Adapter health monitoring loop started")

    # Configuration
    health_check_interval = 600  # 10 minutes
    validation_enabled = True  # Can be made configurable

    while not self._shutdown_event.is_set():
        try:
            # Collect adapters from Supervisor (individual attributes)
            adapters_list = []
            if self.lighter:
                adapters_list.append(self.lighter)
            if self.x10:
                adapters_list.append(self.x10)

            # Log health status for all adapters
            for adapter in adapters_list:
                if hasattr(adapter, 'log_health_status'):
                    try:
                        await adapter.log_health_status()
                    except Exception as e:
                        logger.warning(f"Failed to log health for {adapter.__class__.__name__}: {e}")

            # Validate X10 adapter caches (if enabled)
            if validation_enabled and self.x10:
                try:
                    # Validate funding rates (sample 3 markets)
                    fr_validation = await self.x10.validate_funding_rate_cache(sample_size=3)
                    if fr_validation.get("status") == "discrepancies_found":
                        logger.warning(
                            f"X10 funding rate cache validation failed: "
                            f"{fr_validation['discrepancies']} discrepancies"
                        )
                        # Optionally trigger full refresh on validation failure
                        # await self.x10.refresh_all_market_data(force_full_refresh=True)

                    # Validate prices (sample 3 markets)
                    price_validation = await self.x10.validate_price_cache(sample_size=3)
                    if price_validation.get("status") == "discrepancies_found":
                        logger.warning(
                            f"X10 price cache validation failed: "
                            f"{price_validation['discrepancies']} discrepancies"
                        )

                except Exception as e:
                    logger.warning(f"X10 cache validation error: {e}")

            # Wait for next check or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=health_check_interval
                )
                break  # Event was set, exit loop
            except TimeoutError:
                # Timeout is expected - continue to next iteration
                pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Adapter health loop error: {e}")
            await asyncio.sleep(60)  # 1 min on error

    logger.info("Adapter health monitoring loop stopped")
