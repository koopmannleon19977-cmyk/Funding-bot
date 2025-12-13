"""
Emergency Cleanup: Close all unhedged positions on X10 and Lighter.

This script identifies positions that exist on one exchange but not the other
and closes them to restore a balanced (hedged) state.

Usage:
    python -m src.maintenance.cleanup_unhedged

IMPORTANT: This script is for EMERGENCY use only. It will close positions
that are not properly hedged between exchanges.
"""

import asyncio
import logging
import os
import sys
from typing import Dict, List, Set, Tuple, Optional

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.helpers import safe_float

logger = logging.getLogger("cleanup_unhedged")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class UnhedgedPositionCleaner:
    """
    Async-compatible cleaner for unhedged positions.
    Works with the modern async adapters (X10Adapter, LighterAdapter).
    """
    
    def __init__(self):
        self.x10 = None
        self.lighter = None
        self._initialized = False
    
    async def initialize(self):
        """Initialize adapters asynchronously."""
        if self._initialized:
            return
        
        try:
            from src.adapters.x10_adapter import X10Adapter
            from src.adapters.lighter_adapter import LighterAdapter
            
            logger.info("Initializing X10 Adapter...")
            self.x10 = X10Adapter()
            await self.x10.initialize()
            
            logger.info("Initializing Lighter Adapter...")
            self.lighter = LighterAdapter()
            await self.lighter.load_market_cache()
            
            self._initialized = True
            logger.info("✅ Adapters initialized successfully")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize adapters: {e}")
            raise
    
    async def close(self):
        """Cleanup adapters."""
        if self.x10 and hasattr(self.x10, 'aclose'):
            await self.x10.aclose()
        if self.lighter and hasattr(self.lighter, 'aclose'):
            await self.lighter.aclose()
    
    async def fetch_x10_positions(self) -> Dict[str, Dict]:
        """Fetch all X10 positions and return a mapping by symbol."""
        try:
            positions = await self.x10.fetch_open_positions()
            by_symbol = {}
            for p in positions:
                symbol = p.get("symbol") or p.get("market")
                if symbol and abs(safe_float(p.get("size", 0))) > 1e-8:
                    by_symbol[symbol] = p
            logger.info(f"📊 X10: Found {len(by_symbol)} open positions")
            return by_symbol
        except Exception as e:
            logger.error(f"❌ Failed to fetch X10 positions: {e}")
            return {}
    
    async def fetch_lighter_positions(self) -> Dict[str, Dict]:
        """Fetch all Lighter positions and return a mapping by symbol."""
        try:
            positions = await self.lighter.fetch_open_positions()
            by_symbol = {}
            for p in positions:
                symbol = p.get("symbol")
                size = safe_float(p.get("size", 0))
                is_dust = p.get("is_dust", False)
                # Skip dust positions (< $1)
                if symbol and abs(size) > 1e-8 and not is_dust:
                    by_symbol[symbol] = p
            logger.info(f"📊 Lighter: Found {len(by_symbol)} open positions")
            return by_symbol
        except Exception as e:
            logger.error(f"❌ Failed to fetch Lighter positions: {e}")
            return {}
    
    def identify_unhedged(
        self, 
        x10_positions: Dict[str, Dict], 
        lighter_positions: Dict[str, Dict]
    ) -> Tuple[List[Tuple[str, Dict]], List[Tuple[str, Dict]]]:
        """
        Identify unhedged positions on both exchanges.
        
        Returns:
            - unhedged_x10: Positions on X10 without Lighter hedge
            - unhedged_lighter: Positions on Lighter without X10 hedge
        """
        x10_symbols = set(x10_positions.keys())
        lighter_symbols = set(lighter_positions.keys())
        
        # X10 positions without Lighter hedge
        unhedged_x10 = [
            (symbol, x10_positions[symbol]) 
            for symbol in x10_symbols - lighter_symbols
        ]
        
        # Lighter positions without X10 hedge
        unhedged_lighter = [
            (symbol, lighter_positions[symbol]) 
            for symbol in lighter_symbols - x10_symbols
        ]
        
        return unhedged_x10, unhedged_lighter
    
    async def close_x10_position(self, symbol: str, position: Dict) -> bool:
        """Close X10 position via market order using reduce_only."""
        size = safe_float(position.get("size", 0))
        if abs(size) < 1e-8:
            logger.info(f"⏭️ {symbol}: Zero size, skipping")
            return True
        
        # Determine close side - opposite to position
        close_side = "SELL" if size > 0 else "BUY"
        
        try:
            logger.info(f"🔄 Closing X10 {symbol}: {close_side} {abs(size):.6f}")
            success, order_id = await self.x10.close_live_position(
                symbol=symbol,
                side=close_side,
                notional_usd=0,  # Not used for reduce_only
                reduce_only=True,
                amount=abs(size)
            )
            if success:
                logger.info(f"✅ Closed X10 {symbol} (Order: {order_id})")
            else:
                logger.warning(f"⚠️ X10 close for {symbol} returned False")
            return success
        except Exception as e:
            logger.error(f"❌ Failed to close X10 {symbol}: {e}")
            return False
    
    async def close_lighter_position(self, symbol: str, position: Dict) -> bool:
        """Close Lighter position via market order."""
        size = safe_float(position.get("size", 0))
        if abs(size) < 1e-8:
            logger.info(f"⏭️ {symbol}: Zero size, skipping")
            return True
        
        # Determine close side - opposite to position
        close_side = "SELL" if size > 0 else "BUY"
        
        try:
            logger.info(f"🔄 Closing Lighter {symbol}: {close_side} {abs(size):.6f}")
            success, order_id = await self.lighter.close_live_position(
                symbol=symbol,
                side=close_side,
                notional_usd=0,  # Not used for reduce_only
                reduce_only=True,
                amount=abs(size)
            )
            if success:
                logger.info(f"✅ Closed Lighter {symbol} (Order: {order_id})")
            else:
                logger.warning(f"⚠️ Lighter close for {symbol} returned False")
            return success
        except Exception as e:
            logger.error(f"❌ Failed to close Lighter {symbol}: {e}")
            return False
    
    async def run_cleanup(self, dry_run: bool = True) -> Dict:
        """
        Main cleanup routine.
        
        Args:
            dry_run: If True, only report unhedged positions without closing
        
        Returns:
            Summary dict with counts and details
        """
        await self.initialize()
        
        try:
            # Fetch positions from both exchanges
            x10_positions = await self.fetch_x10_positions()
            lighter_positions = await self.fetch_lighter_positions()
            
            # Identify unhedged
            unhedged_x10, unhedged_lighter = self.identify_unhedged(
                x10_positions, lighter_positions
            )
            
            summary = {
                "x10_total": len(x10_positions),
                "lighter_total": len(lighter_positions),
                "unhedged_x10": len(unhedged_x10),
                "unhedged_lighter": len(unhedged_lighter),
                "closed_x10": 0,
                "closed_lighter": 0,
                "failed": 0,
                "dry_run": dry_run,
            }
            
            if not unhedged_x10 and not unhedged_lighter:
                logger.info("✅ No unhedged positions found. All positions are properly hedged!")
                return summary
            
            # Report findings
            logger.info("=" * 60)
            logger.info("📋 UNHEDGED POSITIONS REPORT")
            logger.info("=" * 60)
            
            if unhedged_x10:
                logger.info(f"\n🔴 X10 positions WITHOUT Lighter hedge ({len(unhedged_x10)}):")
                for symbol, pos in unhedged_x10:
                    size = safe_float(pos.get("size", 0))
                    pnl = safe_float(pos.get("unrealised_pnl") or pos.get("unrealisedPnl", 0))
                    logger.info(f"   - {symbol}: size={size:.6f}, uPnL=${pnl:.4f}")
            
            if unhedged_lighter:
                logger.info(f"\n🔴 Lighter positions WITHOUT X10 hedge ({len(unhedged_lighter)}):")
                for symbol, pos in unhedged_lighter:
                    size = safe_float(pos.get("size", 0))
                    pnl = safe_float(pos.get("unrealized_pnl", 0))
                    logger.info(f"   - {symbol}: size={size:.6f}, uPnL=${pnl:.4f}")
            
            if dry_run:
                logger.info("\n⚠️ DRY RUN MODE - No positions will be closed")
                logger.info("   Run with --execute to close unhedged positions")
                return summary
            
            # Execute closures
            logger.info("\n🚨 EXECUTING CLOSURES...")
            
            for symbol, pos in unhedged_x10:
                success = await self.close_x10_position(symbol, pos)
                if success:
                    summary["closed_x10"] += 1
                else:
                    summary["failed"] += 1
                await asyncio.sleep(0.5)  # Rate limiting
            
            for symbol, pos in unhedged_lighter:
                success = await self.close_lighter_position(symbol, pos)
                if success:
                    summary["closed_lighter"] += 1
                else:
                    summary["failed"] += 1
                await asyncio.sleep(0.5)  # Rate limiting
            
            logger.info("=" * 60)
            logger.info(f"📊 CLEANUP COMPLETE")
            logger.info(f"   Closed X10: {summary['closed_x10']}")
            logger.info(f"   Closed Lighter: {summary['closed_lighter']}")
            logger.info(f"   Failed: {summary['failed']}")
            logger.info("=" * 60)
            
            return summary
            
        finally:
            await self.close()


async def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Emergency cleanup of unhedged positions"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually close positions (default is dry-run)"
    )
    args = parser.parse_args()
    
    cleaner = UnhedgedPositionCleaner()
    
    try:
        summary = await cleaner.run_cleanup(dry_run=not args.execute)
        
        if summary["unhedged_x10"] > 0 or summary["unhedged_lighter"] > 0:
            if args.execute:
                if summary["failed"] > 0:
                    sys.exit(1)
            else:
                logger.info("\n💡 To close these positions, run:")
                logger.info("   python -m src.maintenance.cleanup_unhedged --execute")
                
    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
