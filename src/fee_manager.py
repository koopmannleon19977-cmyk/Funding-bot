# src/fee_manager.py
"""
Fee Manager - Dynamic Fee Management with Caching

Features:
- Singleton pattern for global fee management
- Cached fee rates with TTL (1 hour)
- Proactive fee fetching on start and periodically
- Fallback to config values if API fails
- Per-exchange fee schedules
"""

import time
import logging
import asyncio
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import config

logger = logging.getLogger(__name__)


@dataclass
class FeeSchedule:
    """Fee schedule for an exchange"""
    maker_fee: float
    taker_fee: float
    timestamp: float
    source: str  # 'api' or 'config'
    
    def is_expired(self, ttl_seconds: float = 3600.0) -> bool:
        """Check if fee schedule is expired"""
        return (time.time() - self.timestamp) > ttl_seconds


class FeeManager:
    """
    Singleton Fee Manager for dynamic fee management.
    
    Caches fee schedules from exchanges with TTL.
    Falls back to config values if API fails.
    """
    
    _instance: Optional['FeeManager'] = None
    _lock = asyncio.Lock()
    
    def __init__(self):
        if FeeManager._instance is not None:
            raise RuntimeError("FeeManager is a singleton. Use get_fee_manager() instead.")
        
        self._x10_schedule: Optional[FeeSchedule] = None
        self._lighter_schedule: Optional[FeeSchedule] = None
        self._ttl_seconds = 3600.0  # 1 hour TTL
        self._refresh_task: Optional[asyncio.Task] = None
        self._x10_adapter = None
        self._lighter_adapter = None
        self._enabled = getattr(config, 'DYNAMIC_FEES_ENABLED', True)
        
        # Fallback values from config
        self._fallback_x10_maker = getattr(config, 'MAKER_FEE_X10', 0.0)
        self._fallback_x10_taker = getattr(config, 'TAKER_FEE_X10', 0.000225)
        self._fallback_lighter_fee = getattr(config, 'FEES_LIGHTER', 0.0)
        
        logger.info(
            f"💰 FeeManager initialized (Dynamic Fees: {'ENABLED' if self._enabled else 'DISABLED'})"
        )
    
    @classmethod
    async def get_instance(cls) -> 'FeeManager':
        """Get or create singleton instance"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls.__new__(cls)
                    cls._instance.__init__()
        return cls._instance
    
    def set_adapters(self, x10_adapter, lighter_adapter):
        """Set adapter references for fee fetching"""
        self._x10_adapter = x10_adapter
        self._lighter_adapter = lighter_adapter
    
    async def start(self):
        """Start proactive fee fetching"""
        if not self._enabled:
            logger.info("💰 Dynamic fees disabled, using config values")
            return
        
        # Initial fetch
        await self.refresh_all_fees()
        
        # Start periodic refresh (every 30 minutes)
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        logger.info("✅ FeeManager started with periodic refresh")
    
    async def stop(self):
        """Stop periodic refresh"""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
    
    async def _periodic_refresh(self):
        """Periodic fee refresh task"""
        while True:
            try:
                await asyncio.sleep(1800)  # 30 minutes
                await self.refresh_all_fees()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"FeeManager periodic refresh error: {e}")
    
    async def refresh_all_fees(self):
        """Refresh fees from both exchanges"""
        if not self._enabled:
            return
        
        tasks = []
        if self._x10_adapter:
            tasks.append(self._refresh_x10_fees())
        if self._lighter_adapter:
            tasks.append(self._refresh_lighter_fees())
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _refresh_x10_fees(self):
        """Refresh X10 fees"""
        if not self._x10_adapter:
            return
        
        try:
            schedule = await self._x10_adapter.fetch_fee_schedule()
            if schedule:
                maker, taker = schedule
                self._x10_schedule = FeeSchedule(
                    maker_fee=maker,
                    taker_fee=taker,
                    timestamp=time.time(),
                    source='api'
                )
                logger.info(f"✅ X10 Fees updated: Maker={maker:.6f}, Taker={taker:.6f}")
            else:
                # Use fallback
                self._x10_schedule = FeeSchedule(
                    maker_fee=self._fallback_x10_maker,
                    taker_fee=self._fallback_x10_taker,
                    timestamp=time.time(),
                    source='config'
                )
                logger.debug("X10 Fees: Using config fallback")
        except Exception as e:
            logger.warning(f"X10 fee refresh failed: {e}, using fallback")
            self._x10_schedule = FeeSchedule(
                maker_fee=self._fallback_x10_maker,
                taker_fee=self._fallback_x10_taker,
                timestamp=time.time(),
                source='config'
            )
    
    async def _refresh_lighter_fees(self):
        """Refresh Lighter fees"""
        if not self._lighter_adapter:
            return
        
        try:
            schedule = await self._lighter_adapter.fetch_fee_schedule()
            if schedule:
                maker, taker = schedule
                self._lighter_schedule = FeeSchedule(
                    maker_fee=maker,
                    taker_fee=taker,
                    timestamp=time.time(),
                    source='api'
                )
                logger.info(f"✅ Lighter Fees updated: Maker={maker:.6f}, Taker={taker:.6f}")
            else:
                # Use fallback
                self._lighter_schedule = FeeSchedule(
                    maker_fee=self._fallback_lighter_fee,
                    taker_fee=self._fallback_lighter_fee,
                    timestamp=time.time(),
                    source='config'
                )
                logger.debug("Lighter Fees: Using config fallback")
        except Exception as e:
            logger.warning(f"Lighter fee refresh failed: {e}, using fallback")
            self._lighter_schedule = FeeSchedule(
                maker_fee=self._fallback_lighter_fee,
                taker_fee=self._fallback_lighter_fee,
                timestamp=time.time(),
                source='config'
            )
    
    def get_x10_fees(self, is_maker: bool = False) -> float:
        """Get X10 fee rate"""
        if not self._enabled:
            return self._fallback_x10_maker if is_maker else self._fallback_x10_taker
        
        if self._x10_schedule is None or self._x10_schedule.is_expired(self._ttl_seconds):
            # Return fallback if expired or not loaded
            return self._fallback_x10_maker if is_maker else self._fallback_x10_taker
        
        return self._x10_schedule.maker_fee if is_maker else self._x10_schedule.taker_fee
    
    def get_lighter_fees(self, is_maker: bool = False) -> float:
        """Get Lighter fee rate"""
        if not self._enabled:
            return self._fallback_lighter_fee
        
        if self._lighter_schedule is None or self._lighter_schedule.is_expired(self._ttl_seconds):
            # Return fallback if expired or not loaded
            return self._fallback_lighter_fee
        
        return self._lighter_schedule.maker_fee if is_maker else self._lighter_schedule.taker_fee
    
    def get_fees_for_exchange(self, exchange: str, is_maker: bool = False) -> float:
        """Get fees for exchange by name"""
        if exchange.upper() == 'X10':
            return self.get_x10_fees(is_maker)
        elif exchange.upper() == 'LIGHTER':
            return self.get_lighter_fees(is_maker)
        else:
            logger.warning(f"Unknown exchange: {exchange}, using X10 taker fee")
            return self._fallback_x10_taker
    
    def calculate_trade_fees(
        self, 
        notional_usd: float, 
        exchange1: str, 
        exchange2: str,
        is_maker1: bool = False,
        is_maker2: bool = False
    ) -> float:
        """Calculate total fees for a trade across two exchanges"""
        fee1 = self.get_fees_for_exchange(exchange1, is_maker1)
        fee2 = self.get_fees_for_exchange(exchange2, is_maker2)
        return notional_usd * (fee1 + fee2)
    
    def get_stats(self) -> Dict:
        """Get fee manager statistics"""
        stats = {
            'enabled': self._enabled,
            'x10': {
                'maker': self.get_x10_fees(is_maker=True),
                'taker': self.get_x10_fees(is_maker=False),
                'source': self._x10_schedule.source if self._x10_schedule else 'config',
                'age_seconds': time.time() - self._x10_schedule.timestamp if self._x10_schedule else 0
            },
            'lighter': {
                'maker': self.get_lighter_fees(is_maker=True),
                'taker': self.get_lighter_fees(is_maker=False),
                'source': self._lighter_schedule.source if self._lighter_schedule else 'config',
                'age_seconds': time.time() - self._lighter_schedule.timestamp if self._lighter_schedule else 0
            }
        }
        return stats


# Singleton getter
_fee_manager: Optional[FeeManager] = None

async def get_fee_manager() -> FeeManager:
    """Get singleton FeeManager instance"""
    global _fee_manager
    if _fee_manager is None:
        _fee_manager = await FeeManager.get_instance()
    return _fee_manager

