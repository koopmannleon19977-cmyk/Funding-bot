# src/prediction_v2.py → FINAL & BULLETPROOF VERSION (Updated for BTC Correlation)

import asyncio
import logging
import time
import numpy as np
from collections import deque
from typing import Dict, Tuple, Optional, List

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.btc_correlation import get_btc_monitor  # <--- NEW IMPORT

logger = logging.getLogger(__name__)


class FundingPredictorV2:
    def __init__(self):
        self.rate_history: Dict[str, deque] = {}
        self.ob_imbalance_history: Dict[str, deque] = {}
        self.max_history = 200
        
        # Orderbook imbalance cache (refreshed via WS)
        self.latest_ob_imbalance: Dict[str, float] = {}
        self.latest_oi_change: Dict[str, float] = {}
        self.oi_cache: Dict[str, float] = {}
        
        # BTC Monitor Reference
        self.btc_monitor = get_btc_monitor()
        
        logger.info("FundingPredictorV2 initialized with BTC Correlation")

    async def predict_next_funding_rate(
        self,
        symbol: str,
        current_lighter_rate: float,
        current_x10_rate: float,
        lighter_adapter: LighterAdapter,
        x10_adapter: X10Adapter,
        btc_price: float = 0.0 # Passed from caller
    ) -> Tuple[float, float, float]:
        
        # 1. Feed BTC Price to Monitor
        if btc_price > 0:
            self.btc_monitor.update_price(btc_price)

        # 2. Get BTC Safety Factor
        btc_regime, btc_factor = self.btc_monitor.get_market_regime()

        # === Basis-Berechnung ===
        current_rate = (current_lighter_rate + current_x10_rate) / 2
        divergence = current_lighter_rate - current_x10_rate
        abs_div = abs(divergence)

        # === Orderbook Imbalance ===
        imbalance = self.latest_ob_imbalance.get(symbol, 0.0)
        imbalance_score = 0.0
        if abs(imbalance) > 0.15:
            imbalance_score = np.tanh(abs(imbalance) * 8) * (1 if imbalance > 0 else -1)

        # === OI Velocity ===
        oi_velocity = self.latest_oi_change.get(symbol, 0.0)
        oi_score = np.tanh(oi_velocity * 5000)

        # === Historische Trends ===
        hist = self.rate_history.get(symbol, deque())
        vel, acc = self._vel_acc(hist)

        # === Prediction Calculation ===
        base_pred = current_rate + vel * 3600 + acc * 1800
        divergence_pull = -divergence * 0.85
        external_pressure = (imbalance_score * 0.00003) + (oi_score * 0.00002)

        predicted_rate = base_pred + divergence_pull + external_pressure
        
        # === Apply BTC Correlation ===
        # If BTC is crashing, Funding Rates drop. 
        # If we predict positive funding (Long bias), punish it with the btc_factor.
        if predicted_rate > 0 and btc_factor < 1.0:
            predicted_rate *= btc_factor
            
        delta = predicted_rate - current_rate

        # === Confidence ===
        confidence = 0.5
        confidence += min(0.30, abs_div * 15)
        confidence += min(0.25, abs(imbalance) * 1.2)
        confidence += min(0.20, abs(oi_velocity) * 0.00001)
        confidence += min(0.15, abs(vel) * 36000)
        
        # Penalize confidence if BTC market is chaotic
        if btc_regime == "CRASH":
            confidence *= 0.5
        elif btc_regime == "DUMP":
            confidence *= 0.8

        confidence = min(0.99, confidence)

        # Log critical events only (to avoid spam)
        if btc_factor < 0.8:
             logger.debug(f"⚠️ BTC {btc_regime}: Reduced prediction for {symbol} by factor {btc_factor}")

        return float(predicted_rate), float(delta), float(confidence)

    # === Live Data Feeds ===
    def update_orderbook_imbalance(self, symbol: str, imbalance: float):
        self.latest_ob_imbalance[symbol] = imbalance

    def update_oi_velocity(self, symbol: str, current_oi: float):
        now = time.time()
        prev_oi = self.oi_cache.get(symbol)
        if prev_oi is not None:
            velocity = (current_oi - prev_oi) / 300.0
            self.latest_oi_change[symbol] = velocity
        self.oi_cache[symbol] = current_oi
    
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
        try:
            vel = np.gradient(values, dt)[-1] * 3600
            acc = np.gradient(np.gradient(values, dt), dt)[-1] * 3600
            return vel, acc
        except Exception:
            return 0.0, 0.0

# Singleton
_predictor_v2: Optional[FundingPredictorV2] = None

def get_predictor() -> FundingPredictorV2:
    global _predictor_v2
    if _predictor_v2 is None:
        _predictor_v2 = FundingPredictorV2()
    return _predictor_v2