"""
Shutdown Service.

Orchestrates graceful shutdown of the bot.

Responsibilities:
- Cancel open orders
- Close positions safely
- Persist final state
- Teardown components in order
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.config.settings import Settings
from funding_bot.domain.models import ExecutionState, TradeStatus
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort

logger = get_logger(__name__)


@dataclass
class ShutdownResult:
    """Result of shutdown process."""

    orders_cancelled: int = 0
    positions_closed: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    success: bool = True


class ShutdownService:
    """
    Orchestrates graceful shutdown.

    Shutdown sequence:
    1. Set shutdown flag (stop new operations)
    2. Wait for active executions to complete
    3. Cancel all open orders
    4. Close all positions
    5. Verify positions closed
    6. Persist final state
    7. Close adapters
    """

    def __init__(
        self,
        settings: Settings,
        lighter: ExchangePort,
        x10: ExchangePort,
        store: TradeStorePort,
    ):
        self.settings = settings
        self.lighter = lighter
        self.x10 = x10
        self.store = store

        self._shutdown_flag = False
        self._shutdown_complete = asyncio.Event()

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_flag

    async def shutdown(
        self,
        close_positions: bool = True,
        timeout: float = 60.0,
    ) -> ShutdownResult:
        """
        Execute graceful shutdown.

        Args:
            close_positions: Whether to close all positions.
            timeout: Maximum time for shutdown.
        """
        if self._shutdown_flag:
            logger.warning("Shutdown already in progress")
            await self._shutdown_complete.wait()
            return ShutdownResult()

        self._shutdown_flag = True
        result = ShutdownResult()
        start_time = datetime.now(UTC)

        logger.info("Starting graceful shutdown...")

        try:
            async with asyncio.timeout(timeout):
                # Step 1: Wait for active executions
                await self._wait_for_executions()

                # Step 2: Cancel all open orders
                result.orders_cancelled = await self._cancel_all_orders()

                # Step 3: Close positions if requested
                if close_positions:
                    result.positions_closed = await self._close_all_positions()

                # Step 4: Verify positions closed (only if we actually attempted to close them)
                if close_positions:
                    await self._verify_closed()

                # Step 5: Persist final state
                await self._persist_state(close_positions=close_positions)

        except TimeoutError:
            logger.error("Shutdown timed out")
            result.errors.append("Shutdown timed out")
            result.success = False

        except Exception as e:
            logger.exception(f"Shutdown error: {e}")
            result.errors.append(str(e))
            result.success = False

        finally:
            duration = (datetime.now(UTC) - start_time).total_seconds()
            result.duration_seconds = duration

            self._shutdown_complete.set()

            logger.info(
                f"Shutdown complete in {duration:.1f}s: "
                f"cancelled={result.orders_cancelled}, "
                f"closed={result.positions_closed}"
            )

        return result

    async def _wait_for_executions(self, timeout: float = 10.0) -> None:
        """Wait for active executions to complete."""
        logger.debug("Waiting for active executions...")

        # This would check ExecutionEngine.get_active_executions()
        # For now, just wait a bit
        await asyncio.sleep(1.0)

    async def _cancel_all_orders(self) -> int:
        """Cancel all open orders on both exchanges."""
        logger.info("Cancelling all open orders...")

        cancelled = 0

        try:
            lighter_cancelled = await self.lighter.cancel_all_orders()
            cancelled += lighter_cancelled
        except Exception as e:
            logger.error(f"Failed to cancel Lighter orders: {e}")

        try:
            x10_cancelled = await self.x10.cancel_all_orders()
            cancelled += x10_cancelled
        except Exception as e:
            logger.error(f"Failed to cancel X10 orders: {e}")

        logger.info(f"Cancelled {cancelled} orders")
        return cancelled

    async def _close_all_positions(self) -> int:
        """Close all open positions."""
        logger.info("Closing all positions...")

        closed = 0

        # Get all positions
        try:
            lighter_positions = await self.lighter.list_positions()
            for pos in lighter_positions:
                if pos.qty > Decimal("0.0001"):
                    await self._close_position(self.lighter, pos)
                    closed += 1
        except Exception as e:
            logger.error(f"Error closing Lighter positions: {e}")

        try:
            x10_positions = await self.x10.list_positions()
            for pos in x10_positions:
                if pos.qty > Decimal("0.0001"):
                    await self._close_position(self.x10, pos)
                    closed += 1
        except Exception as e:
            logger.error(f"Error closing X10 positions: {e}")

        logger.info(f"Closed {closed} positions")
        return closed

    async def _close_position(self, adapter: ExchangePort, pos) -> None:
        """Close a single position."""
        from funding_bot.domain.models import OrderRequest, OrderType

        request = OrderRequest(
            symbol=pos.symbol,
            exchange=adapter.exchange,
            side=pos.side.inverse(),
            qty=pos.qty,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )

        try:
            await adapter.place_order(request)
            logger.debug(f"Closed {pos.symbol} on {adapter.exchange}")
        except Exception as e:
            logger.error(f"Failed to close {pos.symbol}: {e}")

    async def _verify_closed(self) -> None:
        """Verify all positions are closed."""
        logger.debug("Verifying positions closed...")

        for adapter in [self.lighter, self.x10]:
            try:
                positions = await adapter.list_positions()
                open_positions = [p for p in positions if p.qty > Decimal("0.0001")]

                if open_positions:
                    logger.warning(
                        f"{adapter.exchange} still has {len(open_positions)} open positions"
                    )
            except Exception as e:
                logger.error(f"Failed to verify {adapter.exchange}: {e}")

    async def _persist_state(self, *, close_positions: bool) -> None:
        """Persist final state to database."""
        logger.debug("Persisting final state...")

        open_trades = await self.store.list_open_trades()
        now = datetime.now(UTC)

        for trade in open_trades:
            # If we did not close positions, we must not pretend trades are closed.
            # Only clean up "orphaned" lifecycle states that have no safe continuation after shutdown.
            if not close_positions:
                if trade.status in (TradeStatus.PENDING, TradeStatus.OPENING):
                    await self.store.update_trade(
                        trade.trade_id,
                        {
                            "status": TradeStatus.FAILED,
                            "execution_state": ExecutionState.ABORTED,
                            "close_reason": "shutdown_orphaned",
                            "closed_at": now,
                            "error": "shutdown",
                        },
                    )
                continue

            # If we closed positions, it is safe to mark remaining open trades as closed.
            await self.store.update_trade(
                trade.trade_id,
                {
                    "status": TradeStatus.CLOSED,
                    "close_reason": "shutdown",
                    "closed_at": now,
                },
            )

        logger.debug(f"Persisted shutdown state for {len(open_trades)} trades")


async def emergency_shutdown(
    lighter: ExchangePort,
    x10: ExchangePort,
) -> None:
    """
    Emergency shutdown - close everything ASAP.

    Used when normal shutdown fails or in panic situations.
    """
    logger.critical("EMERGENCY SHUTDOWN - closing all positions immediately")

    # Cancel all orders
    with contextlib.suppress(Exception):
        await lighter.cancel_all_orders()

    with contextlib.suppress(Exception):
        await x10.cancel_all_orders()

    # Close all positions with market orders
    for adapter in [lighter, x10]:
        try:
            positions = await adapter.list_positions()
            for pos in positions:
                if pos.qty > Decimal("0.0001"):
                    from funding_bot.domain.models import OrderRequest, OrderType

                    request = OrderRequest(
                        symbol=pos.symbol,
                        exchange=adapter.exchange,
                        side=pos.side.inverse(),
                        qty=pos.qty,
                        order_type=OrderType.MARKET,
                        reduce_only=True,
                    )
                    with contextlib.suppress(Exception):
                        await adapter.place_order(request)
        except Exception:
            pass

    logger.critical("Emergency shutdown complete")

