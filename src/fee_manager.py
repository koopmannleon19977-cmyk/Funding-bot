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
    _initialized: bool = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        # Prevent re-initialization
        if FeeManager._initialized:
            return
        FeeManager._initialized = True
        
        self._x10_schedule: Optional[FeeSchedule] = None
        self._lighter_schedule: Optional[FeeSchedule] = None
        self._ttl_seconds = 3600.0  # 1 hour TTL
        self._refresh_task: Optional[asyncio.Task] = None
        self._x10_adapter = None
        self._lighter_adapter = None
        self._enabled = getattr(config, 'DYNAMIC_FEES_ENABLED', True)
        self._started = False
        
        # Real-time fee values (initialized via init_fees)
        self.x10_maker_fee: float = 0.0
        self.x10_taker_fee: float = 0.000225
        self.lighter_maker_fee: float = 0.0
        self.lighter_taker_fee: float = 0.0
        
        # Fallback values from config
        self._fallback_x10_maker = float(getattr(config, 'MAKER_FEE_X10', 0.0))
        self._fallback_x10_taker = float(getattr(config, 'TAKER_FEE_X10', 0.000225))
        self._fallback_lighter_fee = float(getattr(config, 'FEES_LIGHTER', 0.0))
        
        # Validate fallback values
        if self._fallback_x10_taker <= 0:
            logger.warning(f"⚠️ FeeManager: TAKER_FEE_X10 is {self._fallback_x10_taker}, using default 0.000225")
            self._fallback_x10_taker = 0.000225
        if self._fallback_x10_maker < 0:
            logger.warning(f"⚠️ FeeManager: MAKER_FEE_X10 is {self._fallback_x10_maker}, using default 0.0")
            self._fallback_x10_maker = 0.0
        
        # Initialize schedules with fallback values to prevent None
        # They will be updated when refresh_all_fees() is called
        self._x10_schedule = FeeSchedule(
            maker_fee=self._fallback_x10_maker,
            taker_fee=self._fallback_x10_taker,
            timestamp=time.time(),
            source='config'
        )
        self._lighter_schedule = FeeSchedule(
            maker_fee=self._fallback_lighter_fee,
            taker_fee=self._fallback_lighter_fee,
            timestamp=time.time(),
            source='config'
        )
        
        logger.info(
            f"💰 FeeManager initialized (Dynamic Fees: {'ENABLED' if self._enabled else 'DISABLED'})"
        )
    
    def set_adapters(self, x10_adapter, lighter_adapter):
        """Set adapter references for fee fetching"""
        self._x10_adapter = x10_adapter
        self._lighter_adapter = lighter_adapter
        logger.debug(f"FeeManager: Adapters set (X10={x10_adapter is not None}, Lighter={lighter_adapter is not None})")
    
    async def start(self):
        """Start proactive fee fetching"""
        if self._started:
            logger.debug("FeeManager already started")
            return
            
        if not self._enabled:
            logger.info("💰 Dynamic fees disabled, using config values")
            self._started = True
            return
        
        # Initial fetch
        await self.refresh_all_fees()
        
        # Start periodic refresh (every 30 minutes)
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        self._started = True
        logger.info("✅ FeeManager started with periodic refresh")
    
    async def stop(self):
        """Stop periodic refresh"""
        if hasattr(self, '_refresh_task') and self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        logger.info("🛑 FeeManager stopped")
    
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
        if not getattr(self, '_enabled', True):
            return
        
        tasks = []
        if getattr(self, '_x10_adapter', None):
            tasks.append(self._refresh_x10_fees())
        if getattr(self, '_lighter_adapter', None):
            tasks.append(self._refresh_lighter_fees())
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"Fee refresh task {i} failed: {result}")
    
    async def _refresh_x10_fees(self):
        """Refresh X10 fees"""
        if not self._x10_adapter:
            return
        
        try:
            # fetch_fee_schedule() returns (maker, taker) or None
            result = await self._x10_adapter.fetch_fee_schedule()
            if result is None:
                raise ValueError("API returned no fee data")
            
            maker, taker = result  # Only 2 values
            self._x10_schedule = FeeSchedule(
                maker_fee=maker,
                taker_fee=taker,
                timestamp=time.time(),
                source='api'
            )
            logger.info(f"✅ X10 Fees updated: Maker={maker:.6f}, Taker={taker:.6f} (source=api)")
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
            # fetch_fee_schedule() returns (maker, taker) or None
            result = await self._lighter_adapter.fetch_fee_schedule()
            if result is None:
                raise ValueError("API returned no fee data")
            
            maker, taker = result  # Only 2 values
            self._lighter_schedule = FeeSchedule(
                maker_fee=maker,
                taker_fee=taker,
                timestamp=time.time(),
                source='api'
            )
            logger.info(f"✅ Lighter Fees updated: Maker={maker:.6f}, Taker={taker:.6f} (source=api)")
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
        if not getattr(self, '_enabled', True):
            fallback = self._fallback_x10_maker if is_maker else self._fallback_x10_taker
            logger.debug(f"FeeManager: Dynamic fees disabled, using fallback: {fallback}")
            return fallback
        
        # Check if schedule is None or expired
        if self._x10_schedule is None:
            fallback = self._fallback_x10_maker if is_maker else self._fallback_x10_taker
            logger.debug(f"FeeManager: X10 schedule is None, using fallback: {fallback}")
            return fallback
        
        if self._x10_schedule.is_expired(self._ttl_seconds):
            fallback = self._fallback_x10_maker if is_maker else self._fallback_x10_taker
            logger.debug(f"FeeManager: X10 schedule expired, using fallback: {fallback}")
            return fallback
        
        # Use cached schedule
        fee = self._x10_schedule.maker_fee if is_maker else self._x10_schedule.taker_fee
        logger.debug(f"FeeManager: Using X10 schedule fee: {fee} (maker={is_maker}, source={self._x10_schedule.source})")
        return fee
    
    def get_lighter_fees(self, is_maker: bool = False) -> float:
        """Get Lighter fee rate"""
        if not getattr(self, '_enabled', True):
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
            return getattr(self, '_fallback_x10_taker', 0.000225)
    
    def calculate_trade_fees(
        self, 
        notional_usd: float, 
        exchange1: str, 
        exchange2: str,
        is_maker1: bool = False,
        is_maker2: bool = False,
        actual_fee1: Optional[float] = None,
        actual_fee2: Optional[float] = None
    ) -> float:
        """
        Calculate total fees for a trade across two exchanges.
        
        Args:
            notional_usd: Trade notional value
            exchange1: First exchange name
            exchange2: Second exchange name
            is_maker1: Whether exchange1 order is maker
            is_maker2: Whether exchange2 order is maker
            actual_fee1: Actual fee rate from exchange1 (if available)
            actual_fee2: Actual fee rate from exchange2 (if available)
            
        Returns:
            Total fees in USD
        """
        # Use actual fees if provided, otherwise use schedule
        if actual_fee1 is not None:
            fee1 = actual_fee1
        else:
            fee1 = self.get_fees_for_exchange(exchange1, is_maker1)
        
        if actual_fee2 is not None:
            fee2 = actual_fee2
        else:
            fee2 = self.get_fees_for_exchange(exchange2, is_maker2)
        
        return notional_usd * (fee1 + fee2)
    
    async def fetch_x10_fees_from_api(self, x10_adapter) -> dict:
        """
        Fetch real X10 fees from account endpoint. 
        X10 API: GET /api/v1/user/account returns fee_schedule
        """
        try:
            # X10 account info endpoint
            client = await x10_adapter._get_trading_client()
            
            # Option 1: Wenn SDK Methode existiert
            if hasattr(client, 'account') and hasattr(client.account, 'get_account_info'):
                account_info = await client.account.get_account_info()
                fee_schedule = account_info.get('fee_schedule', {})
                
                return {
                    'maker': float(fee_schedule.get('maker', 0.0)),
                    'taker': float(fee_schedule.get('taker', 0.000225))
                }
            
            # Option 2: Direkter REST-Call
            if not x10_adapter.stark_account or not x10_adapter.stark_account.api_key:
                logger.warning("X10 stark_account or api_key missing, using fallback fees")
                return {'maker': 0.0, 'taker': 0.000225}
            
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {
                    'X-Api-Key': x10_adapter.stark_account.api_key,
                    'User-Agent': 'X10PythonTradingClient/0.4.5'
                }
                async with session.get(
                    'https://api.starknet.extended.exchange/api/v1/user/account',
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fee_schedule = data.get('fee_schedule', {})
                        return {
                            'maker': float(fee_schedule.get('maker', 0.0)),
                            'taker': float(fee_schedule.get('taker', 0.000225))
                        }
                        
        except Exception as e:
            logger.error(f"Failed to fetch X10 fees: {e}")
            
        # Fallback
        return {'maker': 0.0, 'taker': 0.000225}
    
    async def init_fees(self, x10_adapter, lighter_adapter):
        """Initialize with real fees from APIs"""
        
        # X10: Fetch from API
        x10_fees = await self.fetch_x10_fees_from_api(x10_adapter)
        self.x10_maker_fee = x10_fees['maker']
        self.x10_taker_fee = x10_fees['taker']
        
        # Lighter: Always 0 (confirmed)
        self.lighter_maker_fee = 0.0
        self.lighter_taker_fee = 0.0
        
        logger.info(
            f"💰 FeeManager: X10 Maker={self.x10_maker_fee:.4%}, Taker={self.x10_taker_fee:.4%} | "
            f"Lighter: 0.00% (both)"
        )
    
    def get_stats(self) -> Dict:
        """Get fee manager statistics"""
        stats = {
            'enabled': getattr(self, '_enabled', True),
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

def get_fee_manager() -> FeeManager:
    """Get singleton FeeManager instance (synchronous)"""
    global _fee_manager
    if _fee_manager is None:
        _fee_manager = FeeManager()
    return _fee_manager


async def init_fee_manager(x10_adapter=None, lighter_adapter=None) -> FeeManager:
    """Initialize and start the fee manager"""
    manager = get_fee_manager()
    if x10_adapter or lighter_adapter:
        manager.set_adapters(x10_adapter, lighter_adapter)
    await manager.start()
    return manager


async def stop_fee_manager():
    """Stop the fee manager"""
    global _fee_manager
    if _fee_manager:
        await _fee_manager.stop()
