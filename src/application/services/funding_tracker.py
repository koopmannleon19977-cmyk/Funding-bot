# ═══════════════════════════════════════════════════════════════════════════════
# FUNDING PNL TRACKER - Tracks realized profit from funding payments
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ Fetches funding payment history from both exchanges
# ✓ Calculates cumulative funding collected per trade
# ✓ Updates database with realized funding income
# ✓ Periodic execution (every hour by default)
# ✓ Detailed logging for profitability monitoring
# ✓ Uses Lighter PositionFunding API for accurate funding data
# ✓ Populates unrealized_pnl and realized_pnl in PnL snapshots
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from decimal import Decimal

import config

from src.pnl_utils import normalize_funding_sign
from src.database import get_funding_repository
# Note: This file has been moved to application/services/ for better organization

logger = logging.getLogger(__name__)


def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        return float(str(val).strip() or default)
    except (ValueError, TypeError):
        return default


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
        update_interval_seconds: int = 300  # Default: 5 minutes (was 1h, but missed funding payments)
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
        self.funding_repo = None

    async def start(self):
        """Start background funding tracking loop"""
        if self._tracking_task is None or self._tracking_task.done():
            self._tracking_task = asyncio.create_task(
                self._tracking_loop(),
                name="funding_tracker"
            )
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

                if getattr(config, "FUNDING_TRACKER_DEBUG", False):
                    next_run = time.time() + self.update_interval
                    logger.debug(
                        f"⏱️ FundingTracker sleeping {self.update_interval}s "
                        f"(next_run_utc={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(next_run))})"
                    )
                
                logger.debug(f"⏱️ [FUNDING_CYCLE] Sleeping {self.update_interval}s until next run...")
                
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

            if getattr(config, "FUNDING_TRACKER_DEBUG", False):
                symbols = [getattr(t, 'symbol', '?') for t in open_trades]
                logger.debug(f"🔎 FundingTracker cycle: open_trades={symbols}")
            
            logger.debug(f"🔄 [FUNDING_CYCLE] Starting update for {len(open_trades)} trades. Time: {time.time()}")
            
            # FIX (2025-12-19): BATCH FETCH X10 FUNDING FIRST
            # This prevents API-latch where individual calls miss funding payments
            x10_batch_funding: Dict[str, float] = {}
            try:
                if hasattr(self.x10, 'fetch_funding_payments_batch'):
                    # Batch fetch all symbols at once
                    symbols = [getattr(t, 'symbol', '') for t in open_trades]
                    oldest_created_at = min(
                        (getattr(t, 'created_at', None) for t in open_trades),
                        default=None
                    )
                    
                    def _normalize_to_ms(ts):
                        if ts is None: return int((time.time() - 86400) * 1000)
                        if hasattr(ts, 'timestamp'):
                            try: return int(ts.timestamp() * 1000)
                            except: return int((time.time() - 86400) * 1000)
                        v = safe_float(ts, 0.0)
                        return int(v) if v >= 1e12 else int(v * 1000)
                    
                    from_time_ms = _normalize_to_ms(oldest_created_at)
                    logger.info(f"🔄 [FUNDING BATCH] Fetching funding for {len(symbols)} symbols from X10...")
                    x10_batch_funding = await self.x10.fetch_funding_payments_batch(
                        symbols=symbols, 
                        from_time=from_time_ms
                    )
                    logger.info(f"✅ [FUNDING BATCH] X10 batch fetch complete: {len(x10_batch_funding)} symbols")
            except Exception as e:
                logger.debug(f"ℹ️ X10 batch fetch not available, falling back to individual calls: {e}")
            
            total_collected = 0.0
            updated_count = 0
            
            for trade in open_trades:
                try:
                    logger.info(f"🔍 [FUNDING_CYCLE] Trade {trade.symbol}: Current Collected={trade.funding_collected}")
                    # FIX (2025-12-19): Pass batch funding to avoid API-latch
                    funding_amount = await self._fetch_trade_funding(trade, x10_batch_funding=x10_batch_funding)
                    logger.info(f"🔍 [FUNDING_CYCLE] Trade {trade.symbol}: New Incremental={funding_amount}")
                    
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
                        
                        # Persist to funding history table
                        if self.funding_repo:
                            try:
                                await self.funding_repo.add_funding_record(
                                    symbol=trade.symbol,
                                    exchange="AGGREGATE",
                                    rate=0.0,
                                    timestamp=int(time.time() * 1000),
                                    collected=funding_amount
                                )
                            except Exception as e:
                                logger.error(f"❌ Failed to record funding history for {trade.symbol}: {e}")
                    
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

    async def _fetch_trade_funding(
        self, 
        trade: Any,
        x10_batch_funding: Optional[Dict[str, float]] = None
    ) -> float:
        """
        Fetch funding payments for a single trade from both exchanges.
        
        Args:
            trade: TradeState object
            x10_batch_funding: Optional pre-fetched batch funding from X10 (FIX 2025-12-19)
            
        Returns:
            INCREMENTAL funding collected in USD (can be negative if paying)
            since the last recorded state.
        """
        symbol = trade.symbol
        
        x10_funding = 0.0
        lighter_funding = 0.0

        def _normalize_to_ms(ts: Any) -> int:
            """Normalize timestamps to milliseconds.

            Accepts datetime-like (has .timestamp()) or numeric seconds/ms.
            """
            if ts is None:
                return 0
            if hasattr(ts, 'timestamp'):
                try:
                    return int(ts.timestamp() * 1000)
                except Exception:
                    return 0
            v = safe_float(ts, 0.0)
            if v <= 0:
                return 0
            # Heuristic: >= 1e12 is already ms (2025 in ms ~ 1.7e12)
            if v >= 1e12:
                return int(v)
            return int(v * 1000)

        # Trade open timestamp (ms). Used for both exchanges.
        created_at = getattr(trade, 'created_at', None)
        from_time_ms = _normalize_to_ms(created_at)
        if not from_time_ms:
            from_time_ms = int((time.time() - 86400) * 1000)
            
        logger.info(f"🔍 [DEBUG_FUNDING] Checking {symbol} (created_at={created_at}, from_ms={from_time_ms})")
        
        # ═══════════════════════════════════════════════════════════════════════
        # X10: Fetch REAL Funding Payments from API (with Batch Caching)
        # ═══════════════════════════════════════════════════════════════════════
        # FIX (2025-12-19): Use pre-fetched batch data if available
        # This prevents API-latch where individual calls miss updates
        # ═══════════════════════════════════════════════════════════════════════
        try:
            # FIX (2025-12-19): Try batch cache first
            if x10_batch_funding and symbol in x10_batch_funding:
                x10_funding = x10_batch_funding[symbol]
                logger.info(f"💵 X10 {symbol}: Using batch-cached funding=${x10_funding:.6f}")
            elif hasattr(self.x10, 'fetch_funding_payments'):
                # Fallback to individual fetch if batch not available
                payments = await self.x10.fetch_funding_payments(symbol=symbol, from_time=from_time_ms)

                if payments:
                    # Sum all funding fees for this symbol
                    # NOTE: x10_adapter.fetch_funding_payments returns "funding_fee" as PnL directly
                    # (Negative = Cost, Positive = Profit)
                    x10_funding = sum(p.get('funding_fee', 0.0) for p in payments)

                    if abs(x10_funding) > 0.00001:
                        logger.debug(
                            f"💵 X10 {symbol}: API funding=${x10_funding:.6f} "
                            f"from {len(payments)} payments"
                        )
                else:
                    logger.debug(f"ℹ️ X10 {symbol}: No funding payments found via API")
            else:
                # Fallback: Use get_funding_for_symbol helper if available
                if hasattr(self.x10, 'get_funding_for_symbol'):
                    x10_funding = await self.x10.get_funding_for_symbol(symbol, from_time_ms)
                else:
                    logger.debug(f"ℹ️ X10 {symbol}: fetch_funding_payments not available")
            
        except Exception as e:
            logger.debug(f"ℹ️ X10 funding fetch failed for {symbol}: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # Lighter: Use NEW PositionFunding API (priority) or Position object (fallback)
        # ═══════════════════════════════════════════════════════════════════════
        # Priority:
        # 1. Try Lighter get_funding_for_symbol() (uses /api/v1/positionFunding)
        # 2. Fall back to funding fields from Position object
        # 3. Log at INFO level if API fails with clear fallback notice
        # ═══════════════════════════════════════════════════════════════════════
        try:
            # Priority 1: NEW API - get_funding_for_symbol() uses /api/v1/positionFunding
            if hasattr(self.lighter, 'get_funding_for_symbol'):
                logger.debug(f"Calling get_funding_for_symbol for {symbol} with from_time_ms={from_time_ms}")
                lighter_funding = await self.lighter.get_funding_for_symbol(symbol, from_time_ms)
                
                if abs(lighter_funding) > 0.00001:
                    logger.debug(
                        f"💵 Lighter {symbol}: API funding=${lighter_funding:.6f} "
                        f"(from positionFunding API)"
                    )
            else:
                # Priority 2 (Fallback): Position object funding fields
                positions = await self.lighter.fetch_open_positions()
                lighter_position = next(
                    (p for p in (positions or []) if p.get('symbol') == symbol),
                    None
                )
                
                if lighter_position:
                    # Try funding_received (profit-positive, normalized by adapter)
                    fr = lighter_position.get('funding_received')
                    if fr is not None:
                        lighter_funding = safe_float(fr, 0.0)
                    else:
                        # total_funding_paid_out (needs sign inversion)
                        funding_paid_out = (
                            lighter_position.get('total_funding_paid_out') or
                            lighter_position.get('total_funding_paid') or
                            0
                        )
                        # Invert for profit-positive
                        lighter_funding = -safe_float(funding_paid_out, 0.0)

                    if abs(lighter_funding) > 0:
                        logger.debug(
                            f"🔍 Lighter {symbol}: funding_received_total={lighter_funding:.6f} "
                            f"(from position object)"
                        )
            
        except Exception as e:
            logger.info(f"ℹ️ Lighter funding fetch failed for {symbol}: {e} (using fallback: 0)")
        
        # Calculate TOTAL net funding (X10 received + Lighter received)
        total_current_funding = x10_funding + lighter_funding
        
        # Calculate INCREMENTAL funding since last update
        previous_funding = trade.funding_collected
        incremental_funding = total_current_funding - previous_funding
        
        # Log NET funding for arbitrage monitoring (only if there's data)
        if abs(x10_funding) > 0.00001 or abs(lighter_funding) > 0.00001:
            logger.info(
                f"📊 {symbol} NET Funding: X10=${x10_funding:.4f}, Lighter=${lighter_funding:.4f}, "
                f"NET=${total_current_funding:.4f} (incr=${incremental_funding:.4f})"
            )
            
            # B5: JSON structured log for funding payment
            try:
                from src.utils.json_logger import get_json_logger
                json_log = get_json_logger()
                if json_log and json_log.enabled:
                    json_log.funding_payment(
                        symbol=symbol,
                        x10_funding=x10_funding,
                        lighter_funding=lighter_funding,
                        net_funding=total_current_funding,
                        incremental=incremental_funding,
                        previous_total=previous_funding
                    )
            except ImportError:
                pass
        
        return incremental_funding

    async def _save_pnl_snapshot(
        self,
        trade_count: int,
        unrealized_pnl: float,
        realized_pnl: float,
        funding_pnl: float
    ):
        """
        Save PnL snapshot to database for historical tracking.
        
        Now fetches actual unrealized PnL from live positions and 
        realized PnL from closed trades in the database.
        """
        try:
            from src.database import get_trade_repository
            repo = await get_trade_repository()
            
            # ═══════════════════════════════════════════════════════════════
            # Populate realized_pnl from closed trades in DB
            # ═══════════════════════════════════════════════════════════════
            try:
                db_realized_pnl = await repo.get_total_pnl()
                if db_realized_pnl != 0:
                    realized_pnl = db_realized_pnl
            except Exception as e:
                logger.debug(f"Could not fetch realized PnL from DB: {e}")
            
            # ═══════════════════════════════════════════════════════════════
            # Populate unrealized_pnl from live positions (mark-to-market)
            # ═══════════════════════════════════════════════════════════════
            try:
                # Fetch unrealized PnL from Lighter positions
                lighter_positions = await self.lighter.fetch_open_positions()
                lighter_unrealized = 0.0
                for pos in (lighter_positions or []):
                    upnl = safe_float(pos.get('unrealized_pnl', 0), 0.0)
                    lighter_unrealized += upnl
                
                # Fetch unrealized PnL from X10 positions (if available)
                x10_positions = await self.x10.fetch_open_positions()
                x10_unrealized = 0.0
                for pos in (x10_positions or []):
                    upnl = safe_float(pos.get('unrealized_pnl', 0), 0.0)
                    x10_unrealized += upnl
                
                live_unrealized = lighter_unrealized + x10_unrealized
                if live_unrealized != 0:
                    unrealized_pnl = live_unrealized
                    logger.debug(
                        f"📊 Live uPnL: Lighter=${lighter_unrealized:.4f}, "
                        f"X10=${x10_unrealized:.4f}, Total=${unrealized_pnl:.4f}"
                    )
            except Exception as e:
                logger.debug(f"Could not fetch unrealized PnL from positions: {e}")
            
            total_pnl = unrealized_pnl + realized_pnl + funding_pnl
            
            await repo.save_pnl_snapshot(
                total_pnl=total_pnl,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                funding_pnl=funding_pnl,
                trade_count=trade_count
            )
            
            logger.debug(
                f"📸 PnL Snapshot: total=${total_pnl:.4f} "
                f"(uPnL=${unrealized_pnl:.4f}, rPnL=${realized_pnl:.4f}, funding=${funding_pnl:.4f})"
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
