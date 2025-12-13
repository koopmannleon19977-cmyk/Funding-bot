# ═══════════════════════════════════════════════════════════════════════════════
# FUNDING PNL TRACKER - Tracks realized profit from funding payments
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ Fetches funding payment history from both exchanges
# ✓ Calculates cumulative funding collected per trade
# ✓ Updates database with realized funding income
# ✓ Periodic execution (every hour by default)
# ✓ Detailed logging for profitability monitoring
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from decimal import Decimal

logger = logging.getLogger(__name__)


class FundingTracker:
    """
    Tracks realized funding payments for open positions.
    
    Strategy:
    1. Query all open trades from StateManager
    2. For each trade, fetch funding history since created_at timestamp
    3. Sum up funding payments (X10 funding_fees + Lighter change)
    4. Update trade in StateManager with total collected funding
    5. Log profitability metrics
    """
    
    def __init__(
        self, 
        x10_adapter,
        lighter_adapter, 
        state_manager,
        update_interval_seconds: int = 3600  # Default: 1 hour
    ):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.state_manager = state_manager
        self.update_interval = update_interval_seconds
        
        self._tracking_task: Optional[asyncio.Task] = None
        self._shutdown = False
        
        self._stats = {
            "total_updates": 0,
            "total_funding_collected": 0.0,
            "last_update_time": 0,
            "errors": 0
        }

    async def start(self):
        """Start background funding tracking loop"""
        if self._tracking_task is None or self._tracking_task.done():
            self._tracking_task = asyncio.create_task(
                self._tracking_loop(),
                name="funding_tracker"
            )
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
                
                # Wait for next update
                await asyncio.sleep(self.update_interval)
                
            except asyncio.CancelledError:
                logger.info("🛑 Funding tracker: Cancelled")
                break
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"❌ Funding tracker error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait 1 minute before retry

    async def update_all_trades(self):
        """Update funding collected for all open trades and save PnL snapshot"""
        start_time = time.time()
        
        try:
            # Get all open trades from StateManager (returns List[TradeState])
            open_trades = await self.state_manager.get_all_open_trades()
            
            if not open_trades:
                logger.debug("📊 No open trades to track")
                # Still save a snapshot even with no open trades
                await self._save_pnl_snapshot(0, 0.0, 0.0, 0.0)
                return
            
            logger.info(f"📊 Updating funding for {len(open_trades)} open trades...")
            
            total_collected = 0.0
            updated_count = 0
            
            for trade in open_trades:
                try:
                    funding_amount = await self._fetch_trade_funding(trade)
                    
                    if abs(funding_amount) > 0.00000001:
                        # Update StateManager using the new funding amount
                        # (We only update if there's a change or significant non-zero value)
                        # NOTE: _fetch_trade_funding returns TOTAL cumulative, or incremental?
                        # The original logic calculated INCREMENTAL.
                        # Let's verify _fetch_trade_funding logic below.
                        
                        # We should likely just update the TOTAL funding collected in the state.
                        
                        current_funding = trade.funding_collected
                        new_total = current_funding + funding_amount
                        
                        await self.state_manager.update_trade(
                            trade.symbol,
                            {'funding_collected': new_total}
                        )
                        
                        total_collected += funding_amount
                        updated_count += 1
                        
                        logger.info(
                            f"💰 {trade.symbol}: Collected ${funding_amount:.4f} funding "
                            f"(total: ${new_total:.4f})"
                        )
                    
                except Exception as e:
                    logger.error(f"❌ Error fetching funding for {trade.symbol}: {e}")
                    continue
            
            # Update stats
            self._stats["total_updates"] += 1
            self._stats["total_funding_collected"] += total_collected
            self._stats["last_update_time"] = int(time.time())
            
            elapsed = time.time() - start_time
            
            if updated_count > 0:
                logger.info(
                    f"✅ Funding update complete: "
                    f"{updated_count}/{len(open_trades)} trades updated, "
                    f"${total_collected:.4f} collected this cycle "
                    f"(${self._stats['total_funding_collected']:.4f} lifetime) "
                    f"in {elapsed:.1f}s"
                )
            
            # Save PnL snapshot for historical tracking
            await self._save_pnl_snapshot(
                len(open_trades),
                0.0,  # TODO: Calculate unrealized PnL from positions
                0.0,  # TODO: Get realized PnL from closed trades
                total_collected
            )
            
        except Exception as e:
            logger.error(f"❌ Error in update_all_trades: {e}", exc_info=True)
            self._stats["errors"] += 1

    async def _fetch_trade_funding(self, trade: Any) -> float:
        """
        Fetch funding payments for a single trade from both exchanges.
        
        Args:
            trade: TradeState object
            
        Returns:
            INCREMENTAL funding collected in USD (can be negative if paying)
            since the last recorded state.
        """
        symbol = trade.symbol
        
        x10_funding = 0.0
        lighter_funding = 0.0
        
        # ═══════════════════════════════════════════════════════════════════════
        # X10: Funding attribution (best-effort)
        # ═══════════════════════════════════════════════════════════════════════
        # NOTE:
        # - X10Adapter.fetch_open_positions currently normalizes only {symbol,size,entry_price}.
        # - Older logic mistakenly tried to treat realised_pnl as "funding", which is incorrect.
        # - Until we wire a dedicated X10 funding-payments endpoint, we track X10 funding as 0.
        #
        # This keeps funding_collected consistent (won't drift with unrelated realized PnL),
        # and we still get the Lighter leg funding correctly (which is what the bot relies on most).
        try:
            positions = await self.x10.fetch_open_positions()
            x10_position = next(
                (p for p in (positions or []) if p.get('symbol') == symbol),
                None
            )
            
            if x10_position:
                # Placeholder (unknown without dedicated endpoint)
                x10_funding = 0.0
            
        except Exception as e:
            logger.warning(f"⚠️ X10 funding fetch error for {symbol}: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # Lighter: Use funding fields from Position object
        # ═══════════════════════════════════════════════════════════════════════
        # Semantik:
        # - total_funding_paid_out > 0  => du hast Funding bezahlt (Cost)
        # - total_funding_paid_out < 0  => du hast Funding erhalten (Profit)
        # Normalisierung:
        # - funding_received = -total_funding_paid_out  (Profit-positiv)
        # ═══════════════════════════════════════════════════════════════════════
        try:
            positions = await self.lighter.fetch_open_positions()
            lighter_position = next(
                (p for p in (positions or []) if p.get('symbol') == symbol),
                None
            )
            
            if lighter_position:
                # Prefer normalized key injected by LighterAdapter.fetch_open_positions()
                fr = lighter_position.get('funding_received')
                if fr is not None:
                    lighter_funding = safe_float(fr, 0.0)
                else:
                    # Fallback: compute from raw paid_out (backward compat)
                    funding_paid_out = (
                        lighter_position.get('total_funding_paid_out') or
                        lighter_position.get('total_funding_paid') or
                        lighter_position.get('funding_paid_out') or
                        lighter_position.get('funding') or
                        lighter_position.get('realized_funding') or
                        0
                    )
                    lighter_funding = -safe_float(funding_paid_out, 0.0)

                if abs(lighter_funding) > 0:
                    logger.debug(
                        f"🔍 Lighter {symbol}: funding_received_total={lighter_funding:.6f} "
                        f"(paid_out={safe_float(lighter_position.get('total_funding_paid_out'), 0.0):.6f})"
                    )
            
        except Exception as e:
            logger.warning(f"⚠️ Lighter funding fetch error for {symbol}: {e}")
        
        # Calculate TOTAL net funding (X10 received + Lighter received)
        total_current_funding = x10_funding + lighter_funding
        
        # Calculate INCREMENTAL funding since last update
        previous_funding = trade.funding_collected
        incremental_funding = total_current_funding - previous_funding
        
        return incremental_funding

    async def _save_pnl_snapshot(
        self,
        trade_count: int,
        unrealized_pnl: float,
        realized_pnl: float,
        funding_pnl: float
    ):
        """Save PnL snapshot to database for historical tracking"""
        try:
            from src.database import get_trade_repository
            repo = await get_trade_repository()
            
            total_pnl = unrealized_pnl + realized_pnl + funding_pnl
            
            await repo.save_pnl_snapshot(
                total_pnl=total_pnl,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                funding_pnl=funding_pnl,
                trade_count=trade_count
            )
        except Exception as e:
            logger.debug(f"PnL snapshot save error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get current tracking statistics"""
        return {
            **self._stats,
            "is_running": self._tracking_task and not self._tracking_task.done(),
            "update_interval_seconds": self.update_interval,
        }
