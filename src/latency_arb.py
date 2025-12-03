"""
Latency Arbitrage Detector

⚠️ DEAKTIVIERT (Stand: 2025-12-03)

Grund: X10 Funding WebSocket sendet NUR stündliche Funding Payment Events. 
Laut offizieller API-Dokumentation:

"While the funding rate is calculated every minute, it is applied only once per hour.
The records include only those funding rates that were used for funding fee payments."

Das bedeutet:
- Funding Rates ändern sich nur alle 1-8 Stunden
- WebSocket sendet Batch-Updates, keine Real-Time Ticks
- Ein Lag von 2-5s ist bei diesem Update-Pattern nicht messbar/sinnvoll

Latency Arb ist sinnvoll für:
✅ Orderbook Updates (kontinuierlich, tick-by-tick)
✅ Trade Streams (jeder Trade = Update)
✅ Price Feeds (kontinuierlich)

Latency Arb ist NICHT sinnvoll für:
❌ Funding Rates (stündliche Batch-Updates)
❌ OI/Liquidation Daten (periodische Snapshots)

Falls X10 in Zukunft Real-Time Funding Updates einführt,
kann dieser Code reaktiviert werden.
"""

import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Feature Flag - auf True setzen um zu reaktivieren
LATENCY_ARB_ENABLED = False


class LatencyArbDetector:
    """
    Latency Arbitrage Detector - DEAKTIVIERT
    
    Erkennt Lag zwischen X10 und Lighter Funding Rate Updates. 
    Aktuell deaktiviert da X10 nur stündliche Updates sendet. 
    """
    
    def __init__(self, lag_threshold_seconds: float = 2.0):
        self.lag_threshold = lag_threshold_seconds
        self.min_rate_change = 0.00005  # 0.5 bps minimum movement
        self.opportunity_cooldown = 30.0
        
        self.last_update_times: Dict[str, Dict[str, float]] = {}
        self.last_rates: Dict[str, Dict[str, float]] = {}
        self.last_opportunity_time: Dict[str, float] = {}
        
        # Log einmalig beim Init
        if LATENCY_ARB_ENABLED:
            logger.info(f"⚡ Latency Arb Detector initialized (threshold: {lag_threshold_seconds}s)")
        else:
            logger.info("⚡ Latency Arb Detector DISABLED (X10 only sends hourly funding updates)")
    
    async def detect_lag_opportunity(
        self, 
        symbol: str, 
        x10_rate: float, 
        lighter_rate: float,
        x10_adapter,
        lighter_adapter
    ) -> Optional[Dict]:
        """
        Detect latency arbitrage opportunity. 
        
        Returns None if:
        - Feature is disabled
        - No significant lag detected
        - Rate change too small
        - On cooldown
        """
        # Feature deaktiviert
        if not LATENCY_ARB_ENABLED:
            return None
        
        # Original-Logik hier (für spätere Reaktivierung)
        # ...  (Code bleibt erhalten aber wird nie ausgeführt)
        
        return None
    
    def _update_timestamps_from_adapters(self, symbol: str, x10_adapter, lighter_adapter):
        """Update timestamps from adapter caches - DISABLED"""
        pass
    
    def _get_lag(self, symbol: str) -> Optional[tuple]:
        """Calculate lag - DISABLED"""
        return None
    
    def get_stats(self) -> Dict:
        """Return detector statistics"""
        return {
            "enabled": LATENCY_ARB_ENABLED,
            "status": "DISABLED - X10 only sends hourly funding updates",
            "reason": "Latency Arb requires real-time tick data, not hourly batches",
            "tracked_symbols": len(self.last_update_times),
        }


# Singleton
_detector: Optional[LatencyArbDetector] = None


def get_detector() -> LatencyArbDetector:
    """Get or create the singleton detector instance"""
    global _detector
    if _detector is None:
        _detector = LatencyArbDetector(lag_threshold_seconds=2.0)
    return _detector


def is_latency_arb_enabled() -> bool:
    """Check if Latency Arb is enabled"""
    return LATENCY_ARB_ENABLED
