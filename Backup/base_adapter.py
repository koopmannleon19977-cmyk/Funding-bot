import logging
import config  # Für LIVE_TRADING
import random  # Für Sim
from typing import Optional, List

logger = logging.getLogger(__name__)

class BaseAdapter:
    """
    Eine Basisklasse für alle Börsen-Adapter.
    Sie definiert die Schnittstelle, die jeder Adapter implementieren muss.
    """
    def __init__(self, name: str):
        self.name = name
        logger.info(f"Initialisiere {self.name}Adapter...")

    async def load_market_cache(self, force: bool = False):
        """Lädt die Marktinformationen in den Cache."""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Market-Cache simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_market_cache() muss implementiert werden.")

    async def load_funding_rates_and_prices(self):
        """Lädt die aktuellen Funding-Rates und Preise."""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Funding-Rates simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_funding_rates_and_prices() muss implementiert werden.")

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Holt eine Funding-Rate aus dem Cache."""
        if not getattr(config, "LIVE_TRADING", False):
            # Simuliere variabel: 0.05–0.2% (gute Arb-Rates)
            return random.uniform(0.0005, 0.002)
        raise NotImplementedError(f"{self.name}.fetch_funding_rate() muss implementiert werden.")

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Holt einen Preis aus dem Cache."""
        if not getattr(config, "LIVE_TRADING", False):
            # Simuliere variabel: 50–100k USD (z.B. BTC/ETH-Range)
            return random.uniform(50, 100000)
        raise NotImplementedError(f"{self.name}.fetch_mark_price() muss implementiert werden.")
    
    def min_notional_usd(self, symbol: str) -> float:
        """Gibt den Mindest-Orderwert in USD für einen Markt zurück."""
        if not getattr(config, "LIVE_TRADING", False):
            return 10.0
        # Fallback in Live: Safe Default, um Crash zu vermeiden
        logger.warning(f"{self.name}: min_notional_usd Fallback – return 20.0 (implementiere in Subklasse).")
        return 20.0  # Safe Min für Perps

    async def fetch_open_positions(self) -> Optional[List[dict]]:
        """Ruft alle offenen Positionen von der Börse ab."""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Keine offenen Positionen (simuliert).")
            return []
        # Live-Fall: wenn Adapter keine Implementierung hat, liefere leere Liste statt Absturz
        logger.warning(f"{self.name}: fetch_open_positions nicht implementiert – gebe leere Liste zurück.")
        return []

    async def open_live_position(self, symbol: str, side: str, notional_usd: float) -> bool:
        """Eröffnet eine neue Position."""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Order {side} {symbol} (${notional_usd}) simuliert – ERFOLG!")
            return True
        raise NotImplementedError(f"{self.name}.open_live_position() muss implementiert werden.")

    async def close_live_position(self, symbol: str, original_side: str, notional_usd: float) -> bool:
        """Schließt eine bestehende Position."""
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run – Close {symbol} (${notional_usd}) simuliert – ERFOLG!")
            return True
        # Live-Fall: wenn nicht implementiert, warne und gib False zurück, damit Aufrufer wissen, dass nichts geschlossen wurde
        logger.warning(f"{self.name}: close_live_position nicht implementiert – Abbruch.")
        return False

    async def aclose(self):
        """Schließt die Verbindung des Adapters."""
        logger.info(f"{self.name} Adapter geschlossen.")