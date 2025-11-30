# src/prediction_v2.py → FINAL & BULLETPROOF VERSION (100% stabil)

import asyncio
import logging
import time
import numpy as np
from collections import deque
from typing import Dict, Tuple, Optional, List

import config
from src.adapters.base_adapter import BaseAdapter
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter

logger = logging.getLogger(__name__)

class FundingPredictorV2:
    def __init__(self):
        self.rate_history: Dict[str, deque] = {}
        self.oi_history: Dict[str, deque] = {}
        self.ob_imbalance_history: Dict[str, deque] = {}
        self.divergence_history: Dict[str, deque] = {}
        self.max_history = 200
        self.btc_price_history = deque(maxlen=200)
        
        # Orderbook imbalance cache (refreshed every 5s via WS)
        self.latest_ob_imbalance: Dict[str, float] = {}
        self.latest_oi_change: Dict[str, float] = {}  # 5-minute velocity
        self.oi_cache: Dict[str, float] = {}
        
        logger.info("FundingPredictorV2 → FINAL BULLETPROOF VERSION")

    async def predict_next_funding_rate(
        self,
        symbol: str,
        current_lighter_rate: float,
        current_x10_rate: float,
        lighter_adapter: LighterAdapter,
        x10_adapter: X10Adapter,
        btc_price: float = 60000.0
    ) -> Tuple[float, float, float]:
        now = time.time()
        self.btc_price_history.append((now, btc_price))

        # === 1. Basis-Validierung + Divergenz ===
        current_rate = (current_lighter_rate + current_x10_rate) / 2
        divergence = current_lighter_rate - current_x10_rate
        abs_div = abs(divergence)

        # === 2. Orderbook Imbalance (Live aus WS) ===
        imbalance = self.latest_ob_imbalance.get(symbol, 0.0)
        imbalance_score = 0.0
        if abs(imbalance) > 0.15:
            imbalance_score = np.tanh(abs(imbalance) * 8) * (1 if imbalance > 0 else -1)

        # === 3. Open Interest Velocity (5-min change) ===
        oi_velocity = self.latest_oi_change.get(symbol, 0.0)
        oi_score = np.tanh(oi_velocity * 5000)  # starke Reaktion ab ~5k OI/s

        # === 4. BTC Correlation Factor ===
        btc_factor = self._btc_factor()

        # === 5. Historische Rate Velocity + Acceleration ===
        hist = self.rate_history.get(symbol, deque())
        vel, acc = self._vel_acc(hist)

        # === 6. Weighting & Final Prediction ===
        # Basis: aktuelle Rate + Momentum
        base_pred = current_rate + vel * 3600 + acc * 1800

        # Divergenz zieht stark → Konvergenz erwartet
        divergence_pull = -divergence * 0.85

        # Imbalance & OI drücken Richtung
        external_pressure = (imbalance_score * 0.00003) + (oi_score * 0.00002)

        # Final Prediction
        predicted_rate = base_pred + divergence_pull + external_pressure
        predicted_rate *= btc_factor  # BTC-Dip = weniger Long-Bias

        # Delta (erwartete Änderung in nächster Stunde)
        delta = predicted_rate - current_rate

        # === 7. Confidence Engine ===
        confidence = 0.5
        confidence += min(0.30, abs_div * 15)           # starke Divergenz → höhere Sicherheit
        confidence += min(0.25, abs(imbalance) * 1.2)   # starker Imbalance → höher
        confidence += min(0.20, abs(oi_velocity) * 0.00001)  # OI-Bewegung
        confidence += min(0.15, abs(vel) * 36000)       # Momentum
        confidence += config.SYMBOL_CONFIDENCE_BOOST.get(symbol.split("-")[0], 0.0)

        confidence = min(0.99, confidence)

        # === 8. Skip Logic (nur bei klarem Signal) ===
        # Alte Logik war zu aggressiv. Wenn geringe Confidence → neutral zurückgeben,
        # damit Trades nicht unnötig blockiert werden. Wenn Delta sehr klein → neutral.
        if confidence < 0.5:
            # Wenig Confidence → Return aktuelle Rate als Prediction (neutral)
            return float(current_rate), 0.0, 0.5  # conf=0.5 = neutral

        if abs(delta) < 0.000001:
            # Delta zu klein um relevant zu sein → neutral
            return float(current_rate), float(delta), float(confidence)

        if confidence > 0.88:
            logger.info(
                f"PREDICT {symbol} Δ{delta:+.8f} → {predicted_rate:+.8f} | "
                f"conf={confidence:.1%} | imb={imbalance:+.2f} oi_vel={oi_velocity:+.0f}"
            )

        return float(predicted_rate), float(delta), float(confidence)

    # === Live Data Feeds (wird vom WebSocket Manager gefüllt) ===
    def update_orderbook_imbalance(self, symbol: str, imbalance: float):
        self.latest_ob_imbalance[symbol] = imbalance

    def update_oi_velocity(self, symbol: str, current_oi: float):
        now = time.time()
        prev_oi = self.oi_cache.get(symbol)
        if prev_oi is not None:
            velocity = (current_oi - prev_oi) / 300.0  # pro Sekunde, über 5 min
            self.latest_oi_change[symbol] = velocity
        self.oi_cache[symbol] = current_oi

    # === 100% sichere Hilfsfunktionen ===
    def _update_history(self, d: Dict[str, deque], symbol: str, value: float, ts: float):
        if symbol not in d:
            d[symbol] = deque(maxlen=self.max_history)
        d[symbol].append((ts, value))

    def _vel_acc(self, history: deque) -> Tuple[float, float]:
        if len(history) < 10:
            return 0.0, 0.0
        values = np.array([v for _, v in list(history)[-30:]])
        dt = np.array([t for t, _ in list(history)[-30:]])
        if len(values) < 10:
            return 0.0, 0.0
        vel = np.gradient(values, dt)[-1] * 3600  # hourly velocity
        acc = np.gradient(np.gradient(values, dt), dt)[-1] * 3600
        return vel, acc

    def _calc_imbalance(self, ob: dict) -> float:
        """
        Calculate orderbook imbalance from bid/ask depth
        
        Imbalance > 0 = More buy pressure (bullish)
        Imbalance < 0 = More sell pressure (bearish)
        
        Args:
            ob: {'bids': [[price, qty], ...], 'asks': [[price, qty], ...]}
        
        Returns:
            float: Imbalance ratio between -1.0 and 1.0
        """
        try:
            if not ob or 'bids' not in ob or 'asks' not in ob:
                return 0.0
            
            bids = ob.get('bids', [])[:15]  # Top 15 levels
            asks = ob.get('asks', [])[:15]
            
            if not bids or not asks:
                return 0.0
            
            # Calculate weighted volume (price * quantity)
            bid_vol = sum(float(p) * float(q) for p, q in bids)
            ask_vol = sum(float(p) * float(q) for p, q in asks)
            
            total = bid_vol + ask_vol
            if total < 1e-8:
                return 0.0
            
            # Normalized imbalance: (bid - ask) / (bid + ask)
            imbalance = (bid_vol - ask_vol) / total
            
            return float(imbalance)
            
        except Exception as e:
            logger.debug(f"Imbalance calc error: {e}")
            return 0.0

    def _btc_factor(self) -> float:
        if len(self.btc_price_history) < 30:
            return 1.0
        prices = [p for _, p in list(self.btc_price_history)[-30:]]
        if len(prices) < 2:
            return 1.0
        change = (prices[-1] - prices[0]) / prices[0]
        if change > 0.005:
            return 1.0
        if change < -0.005:
            return 0.3
        return 0.7
        return 1.0


# Singleton
_predictor_v2: Optional[FundingPredictorV2] = None

def get_predictor() -> FundingPredictorV2:
    global _predictor_v2
    if _predictor_v2 is None:
        _predictor_v2 = FundingPredictorV2()
    return _predictor_v2