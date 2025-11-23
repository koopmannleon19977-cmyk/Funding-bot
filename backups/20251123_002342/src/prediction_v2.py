# src/prediction_v2.py
import logging
import time
from typing import Tuple, Dict, Optional
from collections import deque
import config

logger = logging.getLogger(__name__)

class FundingPredictor:
    """
    Advanced Funding Rate Prediction Engine
    
    Signals:
    1. Orderbook Imbalance (bid pressure vs ask pressure)
    2. OI Velocity (Open Interest momentum)
    3. Rate Velocity (Funding rate momentum)
    4. BTC Correlation (market regime)
    """
    
    def __init__(self):
        self.rate_history: Dict[str, deque] = {}
        self.oi_history: Dict[str, deque] = {}
        self.max_history = 100
        
    def _get_orderbook_imbalance(self, orderbook: dict, depth: int = 10) -> float:
        """
        Calculate orderbook imbalance
        
        Formula: (bid_volume - ask_volume) / (bid_volume + ask_volume)
        
        Returns:
            -1.0 to 1.0 (positive = bullish, negative = bearish)
        """
        if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
            return 0.0
        
        try:
            bids = orderbook['bids'][:depth]
            asks = orderbook['asks'][:depth]
            
            bid_volume = sum(price * size for price, size in bids)
            ask_volume = sum(price * size for price, size in asks)
            
            total = bid_volume + ask_volume
            if total == 0:
                return 0.0
            
            imbalance = (bid_volume - ask_volume) / total
            return imbalance
            
        except Exception as e:
            logger.debug(f"Orderbook imbalance calc error: {e}")
            return 0.0
    
    def _get_oi_velocity(self, symbol: str, current_oi: float) -> float:
        """
        Calculate OI velocity (% change per minute)
        
        Returns:
            Float (e.g. 0.05 = +5%/min)
        """
        if current_oi == 0:
            return 0.0
        
        now = time.time()
        
        if symbol not in self.oi_history:
            self.oi_history[symbol] = deque(maxlen=self.max_history)
        
        history = self.oi_history[symbol]
        history.append((now, current_oi))
        
        if len(history) < 2:
            return 0.0
        
        old_timestamp, old_oi = history[0]
        time_diff = (now - old_timestamp) / 60.0
        
        if time_diff == 0 or old_oi == 0:
            return 0.0
        
        pct_change = ((current_oi - old_oi) / old_oi) / time_diff
        return pct_change
    
    def _get_rate_velocity(self, symbol: str, current_rate: float) -> float:
        """
        Calculate funding rate velocity (momentum)
        
        Returns:
            Float (rate change per hour)
        """
        now = time.time()
        
        if symbol not in self.rate_history:
            self.rate_history[symbol] = deque(maxlen=self.max_history)
        
        history = self.rate_history[symbol]
        history.append((now, current_rate))
        
        if len(history) < 2:
            return 0.0
        
        samples_back = min(10, len(history) - 1)
        old_timestamp, old_rate = history[-samples_back]
        
        time_diff = (now - old_timestamp) / 3600.0
        
        if time_diff == 0:
            return 0.0
        
        rate_change = (current_rate - old_rate) / time_diff
        return rate_change
    
    def _calculate_confidence(
        self,
        imbalance: float,
        oi_velocity: float,
        rate_velocity: float,
        btc_momentum: float
    ) -> float:
        """
        Calculate prediction confidence based on all signals
        
        Returns:
            0.0 - 1.0
        """
        base_confidence = 0.5
        
        imbalance_abs = abs(imbalance)
        if imbalance_abs > 0.3:
            base_confidence += 0.20
        elif imbalance_abs > 0.15:
            base_confidence += 0.10
        
        oi_velocity_abs = abs(oi_velocity)
        if oi_velocity_abs > 0.10:
            base_confidence += 0.15
        elif oi_velocity_abs > 0.05:
            base_confidence += 0.08
        
        rate_velocity_abs = abs(rate_velocity)
        if rate_velocity_abs > 0.001:
            base_confidence += 0.10
        elif rate_velocity_abs > 0.0005:
            base_confidence += 0.05
        
        btc_abs = abs(btc_momentum)
        if btc_abs > config.BTC_STRONG_MOMENTUM_PCT:
            base_confidence += 0.15
        elif btc_abs > config.BTC_MEDIUM_MOMENTUM_PCT:
            base_confidence += 0.10
        elif btc_abs > config.BTC_WEAK_MOMENTUM_PCT:
            base_confidence += 0.05
        
        return min(base_confidence, 0.95)
    
    async def predict_next_funding_rate(
        self,
        symbol: str,
        current_rate: float,
        lighter_adapter,
        x10_adapter,
        btc_trend_pct: float = 0.0
    ) -> Tuple[float, float, float]:
        """
        Predict next funding rate with confidence
        
        Args:
            symbol: Trading pair
            current_rate: Current funding rate (hourly)
            lighter_adapter: Lighter adapter instance
            x10_adapter: X10 adapter instance
            btc_trend_pct: BTC 24h change %
        
        Returns:
            (predicted_rate, rate_delta, confidence)
        """
        try:
            ob_lighter = await lighter_adapter.fetch_orderbook(symbol, limit=20)
            ob_x10 = await x10_adapter.fetch_orderbook(symbol, limit=20)
            orderbook = ob_lighter if ob_lighter.get('bids') else ob_x10
            
            imbalance = self._get_orderbook_imbalance(orderbook, depth=10)
            
            oi_lighter = await lighter_adapter.fetch_open_interest(symbol)
            oi_x10 = await x10_adapter.fetch_open_interest(symbol)
            current_oi = max(oi_lighter, oi_x10)
            
            oi_velocity = self._get_oi_velocity(symbol, current_oi)
            rate_velocity = self._get_rate_velocity(symbol, current_rate)
            
            confidence = self._calculate_confidence(
                imbalance=imbalance,
                oi_velocity=oi_velocity,
                rate_velocity=rate_velocity,
                btc_momentum=btc_trend_pct
            )
            
            predicted_delta = 0.0
            
            if abs(imbalance) > 0.1:
                predicted_delta += imbalance * 0.0001
            
            if abs(rate_velocity) > 0.0001:
                predicted_delta += rate_velocity * 0.3
            
            if abs(oi_velocity) > 0.05:
                predicted_delta += oi_velocity * 0.0002
            
            predicted_rate = current_rate + predicted_delta
            
            symbol_base = symbol.replace("-USD", "")
            for prefix, boost in config.SYMBOL_CONFIDENCE_BOOST.items():
                if symbol_base.startswith(prefix):
                    confidence = min(confidence + boost, 0.95)
                    break
            
            if confidence > 0.75:
                logger.debug(
                    f"PREDICT {symbol}: conf={confidence:.2f} "
                    f"imb={imbalance:.3f} oi_vel={oi_velocity:.3f} "
                    f"rate_vel={rate_velocity:.6f} btc={btc_trend_pct:.1f}%"
                )
            
            return predicted_rate, predicted_delta, confidence
            
        except Exception as e:
            logger.error(f" Prediction error {symbol}: {e}")
            return current_rate, 0.0, 0.5

_predictor = None

def get_predictor() -> FundingPredictor:
    global _predictor
    if _predictor is None:
        _predictor = FundingPredictor()
    return _predictor