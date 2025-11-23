import logging
import config
import random
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

class BaseAdapter:
    """Basisklasse für alle Börsen-Adapter"""
    
    def __init__(self, name: str):
        self.name = name
        logger.info(f"✅ Initialisiere {self.name}Adapter...")

    async def load_market_cache(self, force: bool = False):
        """Lädt Marktinformationen"""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Market-Cache simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_market_cache() muss implementiert werden.")

    async def load_funding_rates_and_prices(self):
        """Lädt Funding-Rates und Preise"""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Funding-Rates simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_funding_rates_and_prices() muss implementiert werden.")

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Holt Funding-Rate (STÜNDLICH normalisiert)"""
        if not getattr(config, "LIVE_TRADING", False):
            return random.uniform(0.0005, 0.002)
        raise NotImplementedError(f"{self.name}.fetch_funding_rate() muss implementiert werden.")

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Holt Mark-Price"""
        if not getattr(config, "LIVE_TRADING", False):
            return random.uniform(50, 100000)
        raise NotImplementedError(f"{self.name}.fetch_mark_price() muss implementiert werden.")
    
    def min_notional_usd(self, symbol: str) -> float:
        """Mindest-Orderwert in USD"""
        if not getattr(config, "LIVE_TRADING", False):
            return 10.0
        logger.warning(f"{self.name}: min_notional_usd Fallback – return 20.0")
        return 20.0

    async def fetch_open_positions(self) -> Optional[List[dict]]:
        """Holt offene Positionen"""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Keine Positionen.")
            return []
        logger.warning(f"{self.name}: fetch_open_positions nicht implementiert.")
        return []

    async def open_live_position(
        self, 
        symbol: str, 
        side: str, 
        notional_usd: float,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Eröffnet Position. Returns: (success, order_id)"""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Order {side} {symbol} (${notional_usd}) simuliert.")
            return True, "DRY_RUN_ORDER_123"
        raise NotImplementedError(f"{self.name}.open_live_position() muss implementiert werden.")

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        """Schließt Position. Returns: (success, order_id)"""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Close {symbol} simuliert.")
            return True, "DRY_RUN_CLOSE_456"
        logger.warning(f"{self.name}: close_live_position nicht implementiert.")
        return False, None

    async def get_order_fee(self, order_id: str) -> float:
        """Holt echte Fee für Order-ID. MUSS von Subklassen implementiert werden."""
        if not getattr(config, "LIVE_TRADING", False):
            return 0.0005  # Simulierte Fee
        raise NotImplementedError(f"{self.name}.get_order_fee() muss implementiert werden.")

    async def get_real_available_balance(self) -> float:
        """Holt verfügbare Balance"""
        if not getattr(config, "LIVE_TRADING", False):
            return 10000.0  # Sim-Balance
        raise NotImplementedError(f"{self.name}.get_real_available_balance() muss implementiert werden.")

    async def aclose(self):
        """Schließt Verbindung"""
        logger.info(f"🔌 {self.name} Adapter geschlossen.")