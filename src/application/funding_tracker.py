# ═══════════════════════════════════════════════════════════════════════════════
# FUNDING PNL TRACKER - Tracks realized profit from funding payments using Decimal
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

from src.infrastructure.database import get_funding_repository
from src.utils import safe_decimal

logger = logging.getLogger(__name__)


class FundingTracker:
    """
    Tracks realized funding payments for open positions using Decimal.
    """

    def __init__(self, x10_adapter, lighter_adapter, state_manager, update_interval_seconds: int = 300):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.state_manager = state_manager
        self.update_interval = update_interval_seconds

        self._tracking_task: asyncio.Task | None = None
        self._shutdown = False

        self._stats = {
            "total_updates": 0,
            "total_funding_collected": Decimal("0.0"),
            "last_update_time": 0,
            "errors": 0,
        }
        self.funding_repo = None

    async def start(self):
        """Start background funding tracking loop"""
        if self._tracking_task is None or self._tracking_task.done():
            self._tracking_task = asyncio.create_task(self._tracking_loop(), name="funding_tracker")
            self.funding_repo = await get_funding_repository()
            logger.info(f"✅ Funding Tracker started (updates every {self.update_interval}s)")

    async def stop(self):
        """Stop background tracking"""
        self._shutdown = True
        if self._tracking_task and not self._tracking_task.done():
            self._tracking_task.cancel()
            try:
                await self._tracking_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ Funding Tracker stopped")

    async def _tracking_loop(self):
        """Background loop that periodically updates funding data"""
        logger.info("🔄 Funding tracking loop started...")

        while not self._shutdown:
            try:
                await self.update_all_trades()
                await asyncio.sleep(self.update_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"❌ Funding tracker error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def update_all_trades(self):
        """Update funding collected for all open trades using Decimal"""
        start_time = time.time()

        try:
            open_trades = await self.state_manager.get_all_open_trades()

            if not open_trades:
                logger.debug("📊 No open trades to track")
                await self._save_pnl_snapshot(0, Decimal("0"), Decimal("0"), Decimal("0"))
                return

            # Batch fetch X10 funding if supported
            x10_batch_funding: dict[str, Decimal] = {}
            try:
                if hasattr(self.x10, "fetch_funding_payments_batch"):
                    symbols = [t.symbol for t in open_trades]
                    oldest_created_at = min((t.created_at for t in open_trades), default=None)

                    from_time_ms = int(oldest_created_at) if oldest_created_at else int((time.time() - 86400) * 1000)

                    x10_batch_funding_raw = await self.x10.fetch_funding_payments_batch(
                        symbols=symbols, from_time=from_time_ms
                    )
                    x10_batch_funding = {k: safe_decimal(v) for k, v in x10_batch_funding_raw.items()}
            except Exception:
                pass

            total_collected = Decimal("0")
            updated_count = 0

            for trade in open_trades:
                try:
                    incremental_funding = await self._fetch_trade_funding(trade, x10_batch_funding=x10_batch_funding)

                    if abs(incremental_funding) > Decimal("1e-8"):
                        current_funding = safe_decimal(trade.funding_collected)
                        new_total = current_funding + incremental_funding

                        await self.state_manager.update_trade(trade.symbol, {"funding_collected": float(new_total)})

                        total_collected += incremental_funding
                        updated_count += 1

                        if self.funding_repo:
                            try:
                                await self.funding_repo.add_funding_record(
                                    symbol=trade.symbol,
                                    exchange="AGGREGATE",
                                    rate=0.0,
                                    timestamp=int(time.time() * 1000),
                                    collected=float(incremental_funding),
                                )
                            except Exception:
                                pass

                except Exception as e:
                    logger.error(f"❌ Error fetching funding for {trade.symbol}: {e}")
                    continue

            self._stats["total_updates"] += 1
            self._stats["total_funding_collected"] += total_collected
            self._stats["last_update_time"] = int(time.time())

            if updated_count > 0:
                logger.info(
                    f"✅ Funding update: {updated_count}/{len(open_trades)} trades updated, ${float(total_collected):.4f} collected"
                )

            # Save PnL snapshot
            await self._save_pnl_snapshot(
                len(open_trades),
                Decimal("0"),  # Unrealized will be updated in _save_pnl_snapshot
                Decimal("0"),  # Realized will be updated in _save_pnl_snapshot
                total_collected,
            )

        except Exception as e:
            logger.error(f"❌ Error in update_all_trades: {e}", exc_info=True)
            self._stats["errors"] += 1

    async def _fetch_trade_funding(self, trade: Any, x10_batch_funding: dict[str, Decimal] | None = None) -> Decimal:
        """
        Fetch funding payments using Decimal.
        """
        symbol = trade.symbol
        x10_funding = Decimal("0")
        lighter_funding = Decimal("0")

        created_at = getattr(trade, "created_at", None)
        from_time_ms = int(created_at) if created_at else int((time.time() - 86400) * 1000)

        # X10
        try:
            if x10_batch_funding and symbol in x10_batch_funding:
                x10_funding = x10_batch_funding[symbol]
            elif hasattr(self.x10, "fetch_funding_payments"):
                payments = await self.x10.fetch_funding_payments(symbol=symbol, from_time=from_time_ms)
                if payments:
                    x10_funding = sum(safe_decimal(p.get("funding_fee", 0)) for p in payments)
        except Exception:
            pass

        # Lighter
        try:
            if hasattr(self.lighter, "get_funding_for_symbol"):
                lighter_funding = safe_decimal(await self.lighter.get_funding_for_symbol(symbol, from_time_ms))
            else:
                positions = await self.lighter.fetch_open_positions()
                pos = next((p for p in (positions or []) if p.symbol == symbol), None)
                if pos:
                    # Fallback to Unrealized PnL from position if specific funding data not available
                    lighter_funding = pos.unrealized_pnl
        except Exception:
            pass

        total_current_funding = x10_funding + lighter_funding
        previous_funding = safe_decimal(trade.funding_collected)
        incremental_funding = total_current_funding - previous_funding

        return incremental_funding

    async def _save_pnl_snapshot(
        self, trade_count: int, unrealized_pnl: Decimal, realized_pnl: Decimal, funding_pnl: Decimal
    ):
        """Save PnL snapshot using Decimal."""
        try:
            from src.infrastructure.database import get_trade_repository

            repo = await get_trade_repository()

            # Realized PnL from DB
            try:
                db_realized_pnl = safe_decimal(await repo.get_total_pnl())
                if db_realized_pnl != 0:
                    realized_pnl = db_realized_pnl
            except Exception:
                pass

            # Unrealized PnL from live positions
            try:
                px_pos, lit_pos = await asyncio.gather(
                    self.x10.fetch_open_positions(), self.lighter.fetch_open_positions(), return_exceptions=True
                )
                px_pos = [] if isinstance(px_pos, Exception) else (px_pos or [])
                lit_pos = [] if isinstance(lit_pos, Exception) else (lit_pos or [])

                u_pnl = sum(p.unrealized_pnl for p in px_pos) + sum(p.unrealized_pnl for p in lit_pos)
                if u_pnl != 0:
                    unrealized_pnl = u_pnl
            except Exception:
                pass

            total_pnl = unrealized_pnl + realized_pnl + funding_pnl

            await repo.save_pnl_snapshot(
                total_pnl=float(total_pnl),
                unrealized_pnl=float(unrealized_pnl),
                realized_pnl=float(realized_pnl),
                funding_pnl=float(funding_pnl),
                trade_count=trade_count,
            )
        except Exception:
            pass

    def get_stats(self) -> dict[str, Any]:
        """Get current tracking statistics"""
        return {
            **self._stats,
            "is_running": self._tracking_task and not self._tracking_task.done(),
            "update_interval_seconds": self.update_interval,
        }
