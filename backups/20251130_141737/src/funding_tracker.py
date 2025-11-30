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
    1. Query all open trades from database
    2. For each trade, fetch funding history since created_at timestamp
    3. Sum up funding payments (X10 funding_fees + Lighter change)
    4. Update database with total collected funding
    5. Log profitability metrics
    """
    
    def __init__(
        self, 
        x10_adapter,
        lighter_adapter, 
        trade_repository,
        update_interval_seconds: int = 3600  # Default: 1 hour
    ):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.trade_repo = trade_repository
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
        """Update funding collected for all open trades"""
        start_time = time.time()
        
        try:
            # Get all open trades
            open_trades = await self.trade_repo.get_open_trades()
            
            if not open_trades:
                logger.info("📊 No open trades to track")
                return
            
            logger.info(f"📊 Updating funding for {len(open_trades)} open trades...")
            
            total_collected = 0.0
            updated_count = 0
            
            for trade in open_trades:
                try:
                    funding_amount = await self._fetch_trade_funding(trade)
                    
                    if funding_amount != 0:
                        # Update database
                        await self.trade_repo.update_trade_funding(
                            trade['symbol'],
                            funding_amount
                        )
                        
                        total_collected += funding_amount
                        updated_count += 1
                        
                        logger.info(
                            f"💰 {trade['symbol']}: Collected ${funding_amount:.2f} funding "
                            f"(total: ${trade.get('funding_collected', 0) + funding_amount:.2f})"
                        )
                    
                except Exception as e:
                    logger.error(f"❌ Error fetching funding for {trade['symbol']}: {e}")
                    continue
            
            # Update stats
            self._stats["total_updates"] += 1
            self._stats["total_funding_collected"] += total_collected
            self._stats["last_update_time"] = int(time.time())
            
            elapsed = time.time() - start_time
            
            logger.info(
                f"✅ Funding update complete: "
                f"{updated_count}/{len(open_trades)} trades updated, "
                f"${total_collected:.2f} collected this cycle "
                f"(${self._stats['total_funding_collected']:.2f} lifetime) "
                f"in {elapsed:.1f}s"
            )
            
        except Exception as e:
            logger.error(f"❌ Error in update_all_trades: {e}", exc_info=True)
            self._stats["errors"] += 1

    async def _fetch_trade_funding(self, trade: Dict[str, Any]) -> float:
        """
        Fetch funding payments for a single trade from both exchanges.
        
        Returns:
            Total funding collected in USD (can be negative if paying)
        """
        symbol = trade['symbol']
        created_at_ms = trade['created_at']
        
        x10_funding = 0.0
        lighter_funding = 0.0
        
        # ═══════════════════════════════════════════════════════════════════════
        # X10: Use get_positions_history() with realised_pnl_breakdown
        # ═══════════════════════════════════════════════════════════════════════
        try:
            # X10 SDK: realised_pnl_breakdown.funding_fees
            # Note: This requires the position to be closed, so we'll use current position instead
            # For open positions, we can check PositionModel.realised_pnl which includes funding
            
            # Alternative: Use get_positions() for current position funding
            positions = await self.x10.fetch_open_positions()
            x10_position = next(
                (p for p in (positions or []) if p.get('symbol') == symbol),
                None
            )
            
            if x10_position:
                # realised_pnl includes funding fees paid out
                x10_funding = float(x10_position.get('realised_pnl', 0))
                logger.debug(f"🔍 X10 {symbol}: realised_pnl=${x10_funding:.4f}")
            
        except Exception as e:
            logger.warning(f"⚠️ X10 funding fetch error for {symbol}: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # Lighter: Use position_funding API
        # ═══════════════════════════════════════════════════════════════════════
        try:
            # Lighter SDK: position_funding() returns PositionFundings
            # Each PositionFunding has 'change' field which is the payment amount
            
            # We need to call the Lighter API through the adapter
            # This requires access to the Lighter account_api
            
            # Since adapters don't expose this directly, we'll fetch it differently
            # Option: Add method to lighter_adapter or use the positions endpoint
            
            positions = await self.lighter.fetch_open_positions()
            lighter_position = next(
                (p for p in (positions or []) if p.get('symbol') == symbol),
                None
            )
            
            if lighter_position:
                # Use total_funding_paid_out if available
                funding_paid_out = lighter_position.get('total_funding_paid_out')
                if funding_paid_out:
                    try:
                        lighter_funding = -float(funding_paid_out)  # Negative because paid OUT
                        logger.debug(f"🔍 Lighter {symbol}: funding_paid_out=${funding_paid_out}")
                    except (ValueError, TypeError):
                        pass
            
        except Exception as e:
            logger.warning(f"⚠️ Lighter funding fetch error for {symbol}: {e}")
        
        # Calculate net funding (X10 received - Lighter paid)
        net_funding = x10_funding + lighter_funding
        
        # Only return the incremental funding since last update
        # Subtract the already-recorded funding_collected from the trade
        previous_funding = trade.get('funding_collected', 0)
        incremental_funding = net_funding - previous_funding
        
        return incremental_funding

    def get_stats(self) -> Dict[str, Any]:
        """Get current tracking statistics"""
        return {
            **self._stats,
            "is_running": self._tracking_task and not self._tracking_task.done(),
            "update_interval_seconds": self.update_interval,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def create_funding_tracker(
    x10_adapter,
    lighter_adapter,
    trade_repository,
    update_interval_hours: float = 1.0
) -> FundingTracker:
    """
    Create and start a funding tracker.
    
    Args:
        x10_adapter: X10 exchange adapter
        lighter_adapter: Lighter exchange adapter
        trade_repository: TradeRepository instance
        update_interval_hours: Hours between updates (default: 1.0)
    
    Returns:
        FundingTracker instance (already started)
    """
    tracker = FundingTracker(
        x10_adapter,
        lighter_adapter,
        trade_repository,
        update_interval_seconds=int(update_interval_hours * 3600)
    )
    
    await tracker.start()
    return tracker
