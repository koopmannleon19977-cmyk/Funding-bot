"""
Reconciler.

Synchronizes database state with actual exchange positions.

Handles:
- Zombie positions (in DB, not on exchange)
- Ghost positions (on exchange, not in DB)
- Late fills (positions appearing after abort)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.config.settings import Settings
from funding_bot.domain.events import PositionReconciled
from funding_bot.domain.models import (
    Exchange,
    ExecutionState,
    FundingRate,
    OrderRequest,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
    TradeStatus,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort
from funding_bot.services.positions.utils import _round_price_to_tick

logger = get_logger(__name__)


@dataclass
class ReconciliationResult:
    """Result of reconciliation."""

    zombies_closed: int = 0
    ghosts_adopted: int = 0
    ghosts_closed: int = 0
    errors: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class Reconciler:
    """
    Reconciles database state with exchange positions.

    Called at startup and periodically as a safety net.
    """

    def __init__(
        self,
        settings: Settings,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: TradeStorePort,
        event_bus: EventBusPort,
    ):
        self.settings = settings
        self.lighter = lighter
        self.x10 = x10
        self.store = store
        self.event_bus = event_bus

        self._running = False
        self._task: asyncio.Task | None = None
        self._reconcile_interval = 300.0  # 5 minutes

    async def start(self) -> None:
        """Start periodic reconciliation."""
        if self._running:
            return

        logger.info("Starting Reconciler...")
        self._running = True

        # Run startup checks
        await self._perform_startup_checks()

        # Run initial reconciliation
        await self.reconcile(startup=True)

        # Start periodic task
        self._task = asyncio.create_task(
            self._reconcile_loop(),
            name="reconciler"
        )

    async def stop(self) -> None:
        """Stop the reconciler."""
        self._running = False

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        logger.info("Reconciler stopped")

    async def _reconcile_loop(self) -> None:
        """Background reconciliation loop."""
        while self._running:
            try:
                await asyncio.sleep(self._reconcile_interval)
                await self.reconcile()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Reconciliation error: {e}")

    async def reconcile(self, *, startup: bool = False) -> ReconciliationResult:
        """
        Perform full reconciliation.

        Compares database trades with exchange positions and handles:
        - Zombies: trades in DB without exchange positions
        - Ghosts: positions on exchange without DB trades
        - Conflicts: side/quantity mismatches between DB and exchange
        """
        logger.info("Starting reconciliation...")
        result = ReconciliationResult()

        try:
            # Phase 1: Fetch data from DB and exchanges
            db_trades = await self.store.list_open_trades()
            db_symbols: set[str] = {t.symbol for t in db_trades}

            lighter_positions = await self._safe_list_positions(self.lighter)
            x10_positions = await self._safe_list_positions(self.x10)

            if lighter_positions is None or x10_positions is None:
                logger.error("Aborting reconciliation: failed to fetch positions")
                result.errors.append("Failed to fetch positions")
                return result

            logger.info(f"Reconciler: DB has {len(db_trades)} active trades: {sorted(list(db_symbols))}")

            # Phase 2: Identify zombie candidates and aggregate exchange positions
            zombie_candidates = self._identify_zombie_candidates(db_trades, startup=startup)
            zombie_symbols = {t.symbol for t in zombie_candidates}

            l_log = [f"{p.symbol}={p.qty}" for p in lighter_positions]
            x_log = [f"{p.symbol}={p.qty}" for p in x10_positions]
            logger.info(f"Reconciler: Lighter positions ({len(lighter_positions)}): {l_log}")
            logger.info(f"Reconciler: X10 positions ({len(x10_positions)}): {x_log}")

            exchange_positions = self._aggregate_exchange_positions(lighter_positions, x10_positions)
            exchange_symbols = set(exchange_positions.keys())

            # Phase 3: Find discrepancies
            zombies = zombie_symbols - exchange_symbols
            ghosts = exchange_symbols - db_symbols
            conflicts = await self._detect_conflicts(db_trades, exchange_positions)

            # Phase 4: Handle conflicts
            await self._handle_conflicts(
                conflicts, lighter_positions, x10_positions, db_trades, result, startup=startup
            )

            # Phase 5: Handle zombies
            await self._handle_zombies(zombies, db_trades, result, startup=startup)

            # Phase 6: Handle ghosts
            await self._handle_ghosts(
                ghosts, lighter_positions, x10_positions, result
            )

            # Phase 7: Log results
            self._log_reconciliation_result(result)

        except Exception as e:
            logger.exception(f"Reconciliation failed: {e}")
            result.errors.append(str(e))

        return result

    async def _handle_conflicts(
        self,
        conflicts: dict[str, str],
        lighter_positions: list[Position],
        x10_positions: list[Position],
        db_trades: list[Trade],
        result: ReconciliationResult,
        *,
        startup: bool,
    ) -> None:
        """Handle conflict positions (side/quantity mismatches)."""
        for symbol, reason in conflicts.items():
            try:
                logger.warning(f"Closing conflict position: {symbol} reason={reason}")
                closed = await self._close_symbol_positions(
                    symbol, lighter_positions, x10_positions
                )
                if closed:
                    await self._mark_db_trades_resolved(
                        symbol, db_trades, reason=reason, startup=startup
                    )
                    result.ghosts_closed += 1
                else:
                    result.errors.append(
                        f"Conflict {symbol}: no matching on-exchange positions found to close"
                    )
            except Exception as e:
                result.errors.append(f"Conflict {symbol}: {e}")

    async def _handle_zombies(
        self,
        zombies: set[str],
        db_trades: list[Trade],
        result: ReconciliationResult,
        *,
        startup: bool,
    ) -> None:
        """Handle zombie positions (in DB but not on exchange)."""
        for symbol in zombies:
            try:
                await self._handle_zombie(symbol, db_trades, startup=startup)
                result.zombies_closed += 1
            except Exception as e:
                result.errors.append(f"Zombie {symbol}: {e}")

    async def _handle_ghosts(
        self,
        ghosts: set[str],
        lighter_positions: list[Position],
        x10_positions: list[Position],
        result: ReconciliationResult,
    ) -> None:
        """Handle ghost positions (on exchange but not in DB)."""
        auto_close_ghosts = bool(
            getattr(self.settings, "reconciliation", None)
            and getattr(self.settings.reconciliation, "auto_close_ghosts", False)
        )
        auto_import_ghosts = bool(
            getattr(self.settings, "reconciliation", None)
            and getattr(self.settings.reconciliation, "auto_import_ghosts", False)
        )

        if not auto_close_ghosts and not auto_import_ghosts and ghosts:
            logger.warning(
                f"Found {len(ghosts)} ghost position(s) but auto_close_ghosts and "
                f"auto_import_ghosts are disabled. Symbols: {sorted(list(ghosts))}. "
                f"Set reconciliation.auto_close_ghosts=true or auto_import_ghosts=true"
            )

        for symbol in ghosts:
            try:
                if not auto_close_ghosts and not auto_import_ghosts:
                    logger.warning(
                        f"Ghost position {symbol} detected but auto-close and "
                        f"auto-import are disabled - SKIPPING"
                    )
                    continue

                closed = await self._handle_ghost(
                    symbol, lighter_positions, x10_positions, action="closed_ghost"
                )
                if closed:
                    result.ghosts_closed += 1
                else:
                    result.ghosts_adopted += 1
            except Exception as e:
                result.errors.append(f"Ghost {symbol}: {e}")

    def _log_reconciliation_result(self, result: ReconciliationResult) -> None:
        """Log the reconciliation result summary."""
        if result.zombies_closed or result.ghosts_closed or result.ghosts_adopted:
            logger.info(
                f"Reconciliation complete: "
                f"zombies={result.zombies_closed}, "
                f"ghosts_closed={result.ghosts_closed}, "
                f"ghosts_adopted={result.ghosts_adopted}"
            )
        elif result.errors:
            logger.warning(f"Reconciliation completed with {len(result.errors)} errors")
        else:
            logger.debug("Reconciliation complete: no discrepancies")

        for err in result.errors:
            logger.warning(f"Reconciliation error: {err}")

    async def _safe_list_positions(self, adapter: ExchangePort) -> list[Position] | None:
        """Safely list positions, returning None on error."""
        try:
            return await adapter.list_positions()
        except Exception as e:
            logger.warning(f"Failed to list positions from {adapter.exchange}: {e}")
            return None

    def _identify_zombie_candidates(
        self,
        db_trades: list[Trade],
        *,
        startup: bool,
    ) -> list[Trade]:
        """
        Identify trades that are zombie candidates.

        Zombie candidates are trades in DB that may not have exchange positions:
        - OPEN/CLOSING trades: Always candidates (must have positions)
        - PENDING trades: Only if stale (> 120s)
        - OPENING trades: Only if stale (> maker_timeout * retries + buffer)

        Args:
            db_trades: List of open trades from DB
            startup: Whether this is a startup reconciliation

        Returns:
            List of trades that are zombie candidates
        """
        now = datetime.now(UTC)
        pending_stale_seconds = 120.0

        maker_timeout = float(
            getattr(
                self.settings.execution,
                "maker_order_timeout_seconds",
                Decimal("60.0"),
            )
        )
        maker_retries = int(
            getattr(self.settings.execution, "maker_order_max_retries", 3)
        )
        opening_stale_seconds = max(
            600.0,  # hard safety floor
            (maker_timeout * max(1, maker_retries)) + 120.0,
        )

        candidates: list[Trade] = []

        for t in db_trades:
            if t.status in (TradeStatus.OPEN, TradeStatus.CLOSING):
                candidates.append(t)
            elif t.status == TradeStatus.PENDING:
                age = (now - t.created_at).total_seconds()
                if age > pending_stale_seconds:
                    candidates.append(t)
            elif t.status == TradeStatus.OPENING:
                if startup:
                    # At startup: any OPENING trade without position is orphaned
                    candidates.append(t)
                    continue

                age = (now - t.created_at).total_seconds()
                if (
                    t.execution_state
                    in (
                        ExecutionState.PENDING,
                        ExecutionState.LEG1_SUBMITTED,
                        ExecutionState.LEG1_FILLED,
                        ExecutionState.LEG2_SUBMITTED,
                    )
                    and age > opening_stale_seconds
                ):
                    candidates.append(t)

        return candidates

    def _aggregate_exchange_positions(
        self,
        lighter_positions: list[Position],
        x10_positions: list[Position],
    ) -> dict[str, dict[Exchange, Position]]:
        """
        Aggregate exchange positions by symbol.

        Filters out dust positions (qty <= 0.0001) and normalizes
        symbol names (strips -USD suffix).

        Args:
            lighter_positions: Positions from Lighter exchange
            x10_positions: Positions from X10 exchange

        Returns:
            Dict mapping symbol -> {Exchange -> Position}
        """
        exchange_positions: dict[str, dict[Exchange, Position]] = {}

        for pos in lighter_positions + x10_positions:
            if pos.qty > Decimal("0.0001"):
                # Normalize: strip -USD to match DB canonical format
                symbol = pos.symbol.replace("-USD", "")
                if symbol not in exchange_positions:
                    exchange_positions[symbol] = {}
                exchange_positions[symbol][pos.exchange] = pos
            else:
                logger.debug(f"Ignoring dust position: {pos.symbol} qty={pos.qty}")

        return exchange_positions

    async def _detect_conflicts(
        self,
        db_trades: list[Trade],
        exchange_positions: dict[str, dict[Exchange, Position]],
    ) -> dict[str, str]:
        """
        Detect conflicts between DB trades and exchange positions.

        Checks for:
        - Side mismatches (DB side != exchange side)
        - Quantity mismatches (Lighter qty != X10 qty beyond tolerance)

        Args:
            db_trades: List of trades from DB
            exchange_positions: Aggregated exchange positions by symbol

        Returns:
            Dict mapping symbol -> conflict reason
        """
        conflicts: dict[str, str] = {}

        for t in db_trades:
            if t.symbol not in exchange_positions:
                continue
            if t.status not in (TradeStatus.OPEN, TradeStatus.CLOSING):
                continue

            l_pos = exchange_positions[t.symbol].get(Exchange.LIGHTER)
            x_pos = exchange_positions[t.symbol].get(Exchange.X10)

            # Check side mismatch
            side_mismatch = False
            if l_pos and l_pos.side != t.leg1.side:
                logger.warning(
                    f"Side mismatch for {t.symbol} on LIGHTER: "
                    f"Exchange={l_pos.side}, DB={t.leg1.side}"
                )
                side_mismatch = True
            if x_pos and x_pos.side != t.leg2.side:
                logger.warning(
                    f"Side mismatch for {t.symbol} on X10: "
                    f"Exchange={x_pos.side}, DB={t.leg2.side}"
                )
                side_mismatch = True

            if side_mismatch:
                conflicts.setdefault(t.symbol, "reconciliation_side_mismatch")
                continue

            # Check quantity mismatch
            l_qty = abs(l_pos.qty) if l_pos else Decimal("0")
            x_qty = abs(x_pos.qty) if x_pos else Decimal("0")

            qty_tolerance_pct = Decimal("0.05")  # 5% tolerance
            max_qty = max(l_qty, x_qty)
            delta = abs(l_qty - x_qty)

            # Only trigger if significant mismatch (> 5% of max side)
            # And at least one side has significant size (> $5 dust)
            if max_qty > 0 and delta > qty_tolerance_pct * max_qty and max_qty > Decimal("5"):
                logger.warning(
                    f"QUANTITY MISMATCH for {t.symbol}: "
                    f"Lighter={l_qty}, X10={x_qty} "
                    f"(delta={delta:.4f}, {delta/max_qty*100:.1f}%)"
                )
                conflicts.setdefault(t.symbol, "reconciliation_quantity_mismatch")

                # Publish alert event
                await self.event_bus.publish(
                    PositionReconciled(
                        symbol=t.symbol,
                        exchange=Exchange.LIGHTER,
                        action="quantity_mismatch",
                        details={
                            "lighter_qty": str(l_qty),
                            "x10_qty": str(x_qty),
                            "delta": str(delta),
                            "delta_pct": f"{delta/max_qty*100:.1f}%",
                        },
                    )
                )

        return conflicts

    async def _handle_zombie(
        self, symbol: str, db_trades: list[Trade], *, startup: bool = False
    ) -> None:
        """
        Handle zombie position (in DB but not on exchange).

        The position was closed externally - mark as closed.
        """
        logger.warning(f"Zombie detected: {symbol} (in DB but not on exchange)")

        # Find all trades for this symbol
        matching_trades = [t for t in db_trades if t.symbol == symbol]

        if not matching_trades:
            return

        for trade in matching_trades:
            if trade.status == TradeStatus.OPENING:
                # Orphaned OPENING trade: cancel any leftover orders and mark FAILED/ABORTED.
                reason = "startup_orphaned_opening" if startup else "reconciliation_stale_opening"
                logger.info(f"Aborting orphaned OPENING trade: {trade.trade_id} ({symbol}) reason={reason}")

                # Best-effort: cancel orders for this symbol on both exchanges to avoid late fills.
                with contextlib.suppress(Exception):
                    await self.lighter.cancel_all_orders(symbol)

                x10_market = symbol if symbol.endswith("-USD") else f"{symbol}-USD"
                with contextlib.suppress(Exception):
                    await self.x10.cancel_all_orders(x10_market)

                trade.status = TradeStatus.FAILED
                trade.execution_state = ExecutionState.ABORTED
                trade.close_reason = reason
                trade.closed_at = datetime.now(UTC)
                trade.error = "orphaned_opening_trade"
                trade._log_event("ABORTED", {"reason": reason})

                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.FAILED,
                        "execution_state": ExecutionState.ABORTED,
                        "close_reason": reason,
                        "closed_at": trade.closed_at,
                        "error": trade.error,
                        "events": trade.events,
                    },
                )
            elif trade.status == TradeStatus.PENDING:
                # A stale pending trade has no on-exchange state; mark as rejected.
                reason = "startup_orphaned_pending" if startup else "reconciliation_stale_pending"
                logger.info(f"Rejecting orphaned PENDING trade: {trade.trade_id} ({symbol}) reason={reason}")

                trade.status = TradeStatus.REJECTED
                trade.execution_state = ExecutionState.ABORTED
                trade.close_reason = reason
                trade.closed_at = datetime.now(UTC)
                trade.error = "orphaned_pending_trade"
                trade._log_event("REJECTED", {"reason": reason})

                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.REJECTED,
                        "execution_state": ExecutionState.ABORTED,
                        "close_reason": reason,
                        "closed_at": trade.closed_at,
                        "error": trade.error,
                        "events": trade.events,
                    },
                )
            else:
                logger.info(f"Closing zombie trade: {trade.trade_id} ({symbol})")

                # Mark as closed
                trade.mark_closed("reconciliation_zombie", Decimal("0"))
                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.CLOSED,
                        "close_reason": "reconciliation_zombie",
                        "closed_at": datetime.now(UTC),
                    },
                )

            await self.event_bus.publish(
                PositionReconciled(
                    symbol=symbol,
                    exchange=Exchange.LIGHTER,
                    action="closed_zombie",
                    details={"trade_id": trade.trade_id},
                )
            )

    async def _get_current_funding_rate(self, symbol: str) -> Decimal:
        """
        Get current funding rate for ghost import.

        Returns average of both exchanges' funding rates (as decimal, not percent).
        """
        try:
            lighter_rate = await self.lighter.get_funding_rate(symbol)
            x10_rate = await self.x10.get_funding_rate(f"{symbol}-USD")

            if lighter_rate and x10_rate:
                # Average of both exchanges (normalized to hourly rate)
                return (lighter_rate.rate + x10_rate.rate) / 2
            elif lighter_rate:
                return lighter_rate.rate
            elif x10_rate:
                return x10_rate.rate
            else:
                return Decimal("0")
        except Exception as e:
            logger.warning(f"Failed to get funding rate for {symbol}: {e}")
            return Decimal("0")

    async def _get_current_spread(self, symbol: str) -> Decimal:
        """
        Get current price spread for ghost import.

        Returns spread as decimal (e.g., 0.001 = 0.1%).
        """
        try:
            lighter_price = await self.lighter.get_price(symbol)
            x10_price = await self.x10.get_price(f"{symbol}-USD")

            if lighter_price and x10_price and lighter_price > 0:
                return abs(lighter_price - x10_price) / lighter_price
            else:
                return Decimal("0")
        except Exception as e:
            logger.warning(f"Failed to get spread for {symbol}: {e}")
            return Decimal("0")

    async def _import_ghost_position(
        self,
        symbol: str,
        lighter_positions: list[Position],
        x10_positions: list[Position],
    ) -> bool:
        """
        Import ghost position into database.

        Returns True if successfully imported, False otherwise.
        """
        try:
            # Find positions on both exchanges
            target_clean = symbol.replace("-USD", "")
            lighter_pos = next(
                (p for p in lighter_positions if p.symbol.replace("-USD", "") == target_clean),
                None
            )
            x10_pos = next(
                (p for p in x10_positions if p.symbol.replace("-USD", "") == target_clean),
                None
            )

            # Validation: Both positions must exist
            if not lighter_pos or not x10_pos:
                logger.warning(
                    f"Ghost {symbol}: Import failed - "
                    f"Lighter={bool(lighter_pos)}, X10={bool(x10_pos)}"
                )
                return False

            # Validation: Quantities must be approximately equal (within 5%)
            lighter_qty = abs(lighter_pos.qty)
            x10_qty = abs(x10_pos.qty)
            max_qty = max(lighter_qty, x10_qty)

            if max_qty > 0:
                qty_diff_pct = abs(lighter_qty - x10_qty) / max_qty
                if qty_diff_pct > Decimal("0.05"):  # 5% tolerance
                    logger.warning(
                        f"Ghost {symbol}: Import failed - "
                        f"Qty mismatch: Lighter={lighter_qty}, X10={x10_qty}, "
                        f"diff={qty_diff_pct:.2%}"
                    )
                    return False
            else:
                logger.warning(f"Ghost {symbol}: Import failed - both quantities are zero")
                return False

            # Validation: Positions must be opposite sides (hedged)
            if lighter_pos.side == x10_pos.side:
                logger.warning(
                    f"Ghost {symbol}: Import failed - both positions on same side "
                    f"({lighter_pos.side})"
                )
                return False

            # Get current market data for import
            current_funding = await self._get_current_funding_rate(symbol)
            current_spread = await self._get_current_spread(symbol)

            # Calculate notional from position size
            avg_price = (lighter_pos.entry_price + x10_pos.entry_price) / 2
            target_notional_usd = lighter_qty * avg_price

            # Create trade with current market data
            from funding_bot.domain.models import Exchange, Trade

            trade = Trade.create(
                symbol=symbol,
                leg1_exchange=Exchange.LIGHTER,
                leg1_side=lighter_pos.side,
                leg2_exchange=Exchange.X10,
                leg2_side=x10_pos.side,
                target_qty=lighter_qty,  # Use average quantity
                target_notional_usd=target_notional_usd,
                entry_apy=current_funding * 24 * 365,  # Convert hourly rate to annual
                entry_spread=current_spread,
            )

            # Add metadata
            trade.metadata = {
                "imported_as_ghost": True,
                "imported_at": datetime.now(UTC).isoformat(),
                "lighter_entry_price": str(lighter_pos.entry_price),
                "x10_entry_price": str(x10_pos.entry_price),
                "lighter_qty": str(lighter_qty),
                "x10_qty": str(x10_qty),
            }

            # Save to database
            await self.store.create_trade(trade)

            logger.info(
                f"Ghost {symbol}: Imported successfully - "
                f"Qty={lighter_qty:.2f}, Notional=${target_notional_usd:.2f}, "
                f"Funding={current_funding*100:.4f}%, Spread={current_spread:.4f}"
            )

            return True

        except Exception as e:
            logger.error(f"Ghost {symbol}: Import failed with exception: {e}")
            logger.exception(f"Ghost {symbol}: Traceback:")
            return False

    async def _handle_ghost(
        self,
        symbol: str,
        lighter_positions: list[Position],
        x10_positions: list[Position],
        *,
        allow_soft_close: bool = True,
        action: str = "closed_ghost",
    ) -> bool:
        """
        Handle ghost position (on exchange but not in DB).

        Options:
        1. Import into DB (if auto_import_ghosts=true)
        2. Close the position (safest fallback)

        Returns True if closed, False if adopted/imported.
        """
        logger.warning(f"Ghost detected: {symbol} (on exchange but not in DB)")

        # Symbol normalization: DB uses canonical symbols (e.g. "SEI"), while X10 positions
        # may include "-USD" suffix ("SEI-USD"). Match loosely to ensure we can close.
        target_clean = symbol.replace("-USD", "")
        lighter_pos = next((p for p in lighter_positions if p.symbol.replace("-USD", "") == target_clean), None)
        x10_pos = next((p for p in x10_positions if p.symbol.replace("-USD", "") == target_clean), None)

        # Check auto-import setting
        auto_import = bool(
            getattr(self.settings, "reconciliation", None) and
            getattr(self.settings.reconciliation, "auto_import_ghosts", False)
        )

        if auto_import:
            # Try to import the ghost position
            imported = await self._import_ghost_position(
                symbol, lighter_positions, x10_positions
            )

            if imported:
                # Successfully imported - publish event
                await self.event_bus.publish(
                    PositionReconciled(
                        symbol=symbol,
                        exchange=Exchange.LIGHTER,
                        action="imported_ghost",
                        details={
                            "reason": "auto_imported",
                            "lighter_qty": str(lighter_pos.qty) if lighter_pos else "0",
                            "x10_qty": str(x10_pos.qty) if x10_pos else "0",
                        },
                    )
                )
                return False  # Not closed, but imported

        # Fallback: close ghost positions for safety
        did_close = False
        if lighter_pos and lighter_pos.qty > Decimal("0.0001"):
            await self._close_position(self.lighter, lighter_pos, allow_soft_close=allow_soft_close)
            did_close = True

        if x10_pos and x10_pos.qty > Decimal("0.0001"):
            await self._close_position(self.x10, x10_pos, allow_soft_close=allow_soft_close)
            did_close = True

        await self.event_bus.publish(
            PositionReconciled(
                symbol=symbol,
                exchange=Exchange.LIGHTER,
                action=action,
                details={
                    "lighter_qty": str(lighter_pos.qty) if lighter_pos else "0",
                    "x10_qty": str(x10_pos.qty) if x10_pos else "0",
                },
            )
        )

        return did_close

    async def _close_symbol_positions(
        self,
        symbol: str,
        lighter_positions: list[Position],
        x10_positions: list[Position],
    ) -> bool:
        """
        Close any on-exchange positions for `symbol` (across both exchanges).

        This is used for conflict resolution where the trade exists in DB but the hedge is
        broken/mismatched. Returns True if at least one close was attempted.
        """
        target_clean = symbol.replace("-USD", "")
        lighter_pos = next((p for p in lighter_positions if p.symbol.replace("-USD", "") == target_clean), None)
        x10_pos = next((p for p in x10_positions if p.symbol.replace("-USD", "") == target_clean), None)

        did_close = False
        if lighter_pos and lighter_pos.qty > Decimal("0.0001"):
            await self._close_position(self.lighter, lighter_pos, allow_soft_close=True)
            did_close = True

        if x10_pos and x10_pos.qty > Decimal("0.0001"):
            await self._close_position(self.x10, x10_pos, allow_soft_close=True)
            did_close = True

        await self.event_bus.publish(
            PositionReconciled(
                symbol=symbol,
                exchange=Exchange.LIGHTER,
                action="closed_conflict",
                details={
                    "lighter_qty": str(lighter_pos.qty) if lighter_pos else "0",
                    "x10_qty": str(x10_pos.qty) if x10_pos else "0",
                },
            )
        )

        return did_close

    async def _mark_db_trades_resolved(
        self,
        symbol: str,
        db_trades: list[Trade],
        *,
        reason: str,
        startup: bool,
    ) -> None:
        """
        If we flattened a conflict on the exchanges, we must also resolve the DB trades;
        otherwise the bot keeps reporting exposure from stale OPEN/CLOSING trades.
        """
        matching_trades = [t for t in db_trades if t.symbol == symbol]
        if not matching_trades:
            return

        for trade in matching_trades:
            if trade.status in (TradeStatus.OPEN, TradeStatus.CLOSING):
                trade.mark_closed(reason, Decimal("0"))
                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.CLOSED,
                        "realized_pnl": trade.realized_pnl,
                        "close_reason": trade.close_reason,
                        "closed_at": trade.closed_at,
                    },
                )
                continue

            if trade.status == TradeStatus.OPENING:
                abort_reason = f"{reason}_opening" if startup else reason
                trade.status = TradeStatus.FAILED
                trade.execution_state = ExecutionState.ABORTED
                trade.close_reason = abort_reason
                trade.closed_at = datetime.now(UTC)
                trade.error = "reconciliation_conflict_opening"
                trade._log_event("ABORTED", {"reason": abort_reason})
                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.FAILED,
                        "execution_state": ExecutionState.ABORTED,
                        "close_reason": abort_reason,
                        "closed_at": trade.closed_at,
                        "error": trade.error,
                        "events": trade.events,
                    },
                )
                continue

            if trade.status == TradeStatus.PENDING:
                reject_reason = f"{reason}_pending" if startup else reason
                trade.status = TradeStatus.REJECTED
                trade.execution_state = ExecutionState.ABORTED
                trade.close_reason = reject_reason
                trade.closed_at = datetime.now(UTC)
                trade.error = "reconciliation_conflict_pending"
                trade._log_event("REJECTED", {"reason": reject_reason})
                await self.store.update_trade(
                    trade.trade_id,
                    {
                        "status": TradeStatus.REJECTED,
                        "execution_state": ExecutionState.ABORTED,
                        "close_reason": reject_reason,
                        "closed_at": trade.closed_at,
                        "error": trade.error,
                        "events": trade.events,
                    },
                )

    async def _close_position(
        self, adapter: ExchangePort, pos: Position, *, allow_soft_close: bool = True
    ) -> None:
        """Close a single position."""
        if allow_soft_close:
            soft_closed = await self._attempt_soft_close(adapter, pos)
            if soft_closed:
                logger.info(f"Soft-closed ghost position: {pos.symbol} on {adapter.exchange}")
                return

        await self._execute_market_close(adapter, pos)

    async def _attempt_soft_close(self, adapter: ExchangePort, pos: Position) -> bool:
        """Try to close a position using passive limit orders."""
        rs = getattr(self.settings, "reconciliation", None)
        if not rs or not getattr(rs, "soft_close_enabled", True):
            return False

        max_attempts = int(getattr(rs, "soft_close_max_attempts", 2) or 0)
        if max_attempts <= 0:
            return False

        timeout = float(getattr(rs, "soft_close_timeout_seconds", 15.0))
        timeout = max(1.0, min(timeout, 60.0))

        qty_threshold = Decimal("0.0001")

        for attempt in range(max_attempts):
            target_pos = pos
            try:
                current_pos = await adapter.get_position(pos.symbol)
            except Exception as e:
                logger.warning(f"Soft close failed to fetch position ({pos.symbol}): {e}")
                current_pos = None

            if current_pos:
                if current_pos.qty <= qty_threshold:
                    logger.info(f"Position already closed for {pos.symbol} on {adapter.exchange}")
                    return True
                target_pos = current_pos

            price = await self._get_soft_close_price(adapter, target_pos)
            if price <= 0:
                logger.warning(
                    f"Soft close skipped: missing price for {pos.symbol} on {adapter.exchange}"
                )
                return False

            logger.info(
                f"Attempting Soft Close via Limit Order @ {price} "
                f"({pos.symbol} on {adapter.exchange}, attempt {attempt + 1}/{max_attempts})"
            )

            request = OrderRequest(
                symbol=target_pos.symbol,
                exchange=adapter.exchange,
                side=target_pos.side.inverse(),
                qty=target_pos.qty,
                order_type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.POST_ONLY,
                reduce_only=True,
            )

            try:
                order = await adapter.place_order(request)
            except Exception as e:
                logger.warning(f"Soft close order rejected for {pos.symbol} on {adapter.exchange}: {e}")
                continue

            filled = await self._wait_for_soft_close_fill(
                adapter, pos.symbol, order.order_id, timeout
            )
            if filled:
                return True

        return False

    async def _get_soft_close_price(self, adapter: ExchangePort, pos: Position) -> Decimal:
        """Derive a maker-safe limit price around mid."""
        try:
            ob = await adapter.get_orderbook_l1(pos.symbol)
        except Exception as e:
            logger.warning(f"Soft close failed to fetch L1 for {pos.symbol} on {adapter.exchange}: {e}")
            return Decimal("0")

        best_bid = ob.get("best_bid", Decimal("0"))
        best_ask = ob.get("best_ask", Decimal("0"))
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return Decimal("0")

        mid = (best_bid + best_ask) / Decimal("2")
        market_info = await adapter.get_market_info(pos.symbol)
        tick = market_info.tick_size if market_info else Decimal("0.01")

        close_side = pos.side.inverse()
        rounding = "ceil" if close_side == Side.SELL else "floor"
        price = _round_price_to_tick(mid, tick, rounding=rounding)

        if close_side == Side.BUY and price >= best_ask:
            price = _round_price_to_tick(best_bid, tick, rounding="floor")
        elif close_side == Side.SELL and price <= best_bid:
            price = _round_price_to_tick(best_ask, tick, rounding="ceil")

        return price

    async def _wait_for_soft_close_fill(
        self, adapter: ExchangePort, symbol: str, order_id: str, timeout: float
    ) -> bool:
        start = time.monotonic()
        last_seen = None

        while (time.monotonic() - start) < timeout:
            try:
                order = await asyncio.wait_for(adapter.get_order(symbol, order_id), timeout=2.0)
            except TimeoutError:
                order = None

            last_seen = order
            if order and order.is_filled:
                return True
            if order and order.status.is_terminal():
                return False

            await asyncio.sleep(0.5)

        if last_seen is None or last_seen.is_active:
            with contextlib.suppress(Exception):
                await adapter.cancel_order(symbol, order_id)
        return False

    async def _execute_market_close(self, adapter: ExchangePort, pos: Position) -> None:
        """Fallback to market close for a single position."""
        qty_threshold = Decimal("0.0001")
        current_pos = None
        try:
            current_pos = await adapter.get_position(pos.symbol)
        except Exception as e:
            logger.warning(f"Failed to fetch position for close ({pos.symbol}): {e}")

        if current_pos and current_pos.qty <= qty_threshold:
            logger.info(f"Position already closed for {pos.symbol} on {adapter.exchange}")
            return

        target = current_pos or pos
        if target.qty <= qty_threshold:
            logger.info(f"Skipping market close: empty position {pos.symbol} on {adapter.exchange}")
            return

        request = OrderRequest(
            symbol=target.symbol,
            exchange=adapter.exchange,
            side=target.side.inverse(),
            qty=target.qty,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )

        try:
            await adapter.place_order(request)

            # Best-effort verification (especially important for Lighter MARKET emulation via IOC).
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                with contextlib.suppress(Exception):
                    updated = await adapter.get_position(pos.symbol)
                    if not updated or updated.qty <= qty_threshold:
                        logger.info(f"Closed ghost position: {pos.symbol} on {adapter.exchange}")
                        return
                await asyncio.sleep(0.5)

            # If we're here: order submitted but position still open.
            with contextlib.suppress(Exception):
                updated = await adapter.get_position(pos.symbol)
                if updated and updated.qty > qty_threshold:
                    logger.warning(
                        f"Market close submitted but position still open: {pos.symbol} on {adapter.exchange} "
                        f"side={updated.side} qty={updated.qty}"
                    )
        except Exception as e:
            logger.error(f"Failed to close ghost {pos.symbol}: {e}")
            raise

    async def check_late_fills(self) -> int:
        """
        Check for late fills (positions appearing after abort).

        Called periodically after failed executions.
        Returns number of late fills handled.
        """
        count = 0

        # Get all trades in rollback/failed state
        all_trades = await self.store.list_trades(status=TradeStatus.FAILED, limit=50)
        all_trades += await self.store.list_trades(status=TradeStatus.ROLLBACK, limit=50)

        for trade in all_trades:
            # Skip old trades
            age = (datetime.now(UTC) - trade.created_at).total_seconds()
            if age > 3600:  # 1 hour
                continue

            # Check if position appeared (parallel fetch for speed)
            results = await asyncio.gather(
                self.lighter.get_position(trade.symbol),
                self.x10.get_position(trade.symbol),
                return_exceptions=True
            )

            # Handle exceptions from gather with proper typing
            lighter_pos: Position | None = results[0] if not isinstance(results[0], BaseException) else None
            x10_pos: Position | None = results[1] if not isinstance(results[1], BaseException) else None

            has_position = (
                (lighter_pos and lighter_pos.qty > Decimal("0.0001")) or
                (x10_pos and x10_pos.qty > Decimal("0.0001"))
            )

            if has_position:
                logger.warning(f"Late fill detected for {trade.symbol}")

                # Close any positions
                if lighter_pos and lighter_pos.qty > Decimal("0.0001"):
                    await self._close_position(self.lighter, lighter_pos)
                if x10_pos and x10_pos.qty > Decimal("0.0001"):
                    await self._close_position(self.x10, x10_pos)

                count += 1

        return count

    async def _perform_startup_checks(self) -> None:
        """
        Perform startup sanitary checks on data feeds.

        Validates:
        - Funding rate consistency across exchanges (both return hourly rates)
        - Rate normalization (interval_hours configurable, default: 1 = hourly)
        - Reasonable rate bounds to detect data anomalies
        """
        logger.info("Running startup data consistency checks...")

        try:
            # Check Funding Rate scaling and consistency across exchanges
            # Use common symbols as benchmark (BTC, ETH)
            test_symbols = ["BTC", "ETH"]

            for symbol in test_symbols:
                lighter_rate: FundingRate | None = None
                x10_rate: FundingRate | None = None

                # Fetch funding rates from both exchanges
                with contextlib.suppress(Exception):
                    lighter_rate = await self.lighter.get_funding_rate(symbol)

                with contextlib.suppress(Exception):
                    x10_rate = await self.x10.get_funding_rate(symbol)

                # Only compare if we have both rates
                if lighter_rate and x10_rate:
                    # Both rates should be normalized to hourly, so they should be similar
                    # Allow for reasonable spread due to market inefficiencies
                    rate_diff = abs(lighter_rate.rate - x10_rate.rate)
                    max_diff = Decimal("0.0001")  # 0.01% hourly difference threshold

                    if rate_diff > max_diff:
                        logger.warning(
                            f"Funding rate discrepancy for {symbol}: "
                            f"Lighter={lighter_rate.rate:.6f}, X10={x10_rate.rate:.6f}, "
                            f"diff={rate_diff:.6f}"
                        )
                    else:
                        logger.info(
                            f"Funding rate check passed for {symbol}: "
                            f"Lighter={lighter_rate.rate:.6f}, X10={x10_rate.rate:.6f}"
                        )

                    # Sanity check: rates should be within reasonable bounds
                    # Annualized rate should be between -100% and +1000% for sanity
                    annual_lighter = lighter_rate.rate_annual
                    annual_x10 = x10_rate.rate_annual

                    for exchange_name, annual_rate in [
                        ("Lighter", annual_lighter),
                        ("X10", annual_x10),
                    ]:
                        if annual_rate < Decimal("-1.0") or annual_rate > Decimal("10.0"):
                            logger.error(
                                f"Anomalous funding rate detected for {symbol} on {exchange_name}: "
                                f"annualized={annual_rate*100:.1f}%"
                            )

                elif lighter_rate or x10_rate:
                    # Log if only one exchange has data
                    available = "Lighter" if lighter_rate else "X10"
                    logger.warning(
                        f"Partial funding rate data for {symbol}: only {available} available"
                    )

        except Exception as e:
            logger.warning(f"Startup check failed (non-critical): {e}")

