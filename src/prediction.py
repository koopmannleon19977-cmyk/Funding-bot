# ═══════════════════════════════════════════════════════════════════════════════
# PUNKT 6: PREDICTION V2 - ECHTE FUNDING RATE PREDICTION
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ Multi-Signal Prediction (Momentum, Mean Reversion, OI, Spread)
# ✓ Historical Funding Rate Analysis
# ✓ Confidence Scoring (0. 0 - 1.0)
# ✓ Adaptive Thresholds basierend auf Volatilität
# ✓ Caching für Performance
# ✓ Regime Detection (Trending vs Ranging)
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import logging
import time
import math
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
import statistics

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market regime classification"""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNKNOWN = "unknown"


class PredictionSignal(Enum):
    """Individual prediction signals"""
    STRONG_LONG = 2      # Strong signal to go long on this exchange
    WEAK_LONG = 1
    NEUTRAL = 0
    WEAK_SHORT = -1
    STRONG_SHORT = -2    # Strong signal to go short on this exchange


@dataclass
class FundingSnapshot:
    """Single funding rate observation"""
    timestamp: int
    rate_x10: float
    rate_lighter: float
    spread: float  # rate_lighter - rate_x10
    mark_price: float = 0.0
    open_interest: float = 0.0


@dataclass
class PredictionResult:
    """Result of funding rate prediction"""
    symbol: str
    timestamp: int
    
    # Predicted values
    predicted_spread: float           # Expected spread in next period
    predicted_apy: float              # Annualized APY
    predicted_direction: str          # "long_x10" or "long_lighter"
    
    # Confidence & signals
    confidence: float                 # 0.0 to 1.0
    signal_strength: int              # -2 to +2
    signals: Dict[str, float]         # Individual signal contributions
    
    # Context
    current_spread: float
    historical_avg_spread: float
    regime: MarketRegime
    
    # Recommendation
    should_trade: bool
    recommended_size_multiplier: float  # 0.5x to 2. 0x based on confidence
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp,
            'predicted_spread': self.predicted_spread,
            'predicted_apy': self.predicted_apy,
            'predicted_direction': self.predicted_direction,
            'confidence': self.confidence,
            'signal_strength': self.signal_strength,
            'signals': self. signals,
            'current_spread': self.current_spread,
            'historical_avg_spread': self.historical_avg_spread,
            'regime': self.regime.value,
            'should_trade': self.should_trade,
            'recommended_size_multiplier': self.recommended_size_multiplier,
        }


class FundingPredictor:
    """
    Predicts funding rate spreads using multiple signals. 
    
    ═══════════════════════════════════════════════════════════════════════════
    SIGNAL COMPONENTS:
    ═══════════════════════════════════════════════════════════════════════════
    
    1. MOMENTUM (40% weight)
       - Trend der letzten N Funding-Perioden
       - Beschleunigung/Verzögerung
       
    2. MEAN REVERSION (30% weight)
       - Abweichung vom historischen Durchschnitt
       - Je weiter weg, desto stärker die Reversion-Erwartung
       
    3. SPREAD PERSISTENCE (20% weight)
       - Wie stabil war der Spread in der Vergangenheit? 
       - Autocorrelation der Spreads
       
    4. OPEN INTEREST (10% weight)
       - Hoher OI = mehr "sticky" Positionen
       - Veränderung des OI als Signal
       
    ═══════════════════════════════════════════════════════════════════════════
    """
    
    # Signal weights
    WEIGHT_MOMENTUM = 0.40
    WEIGHT_MEAN_REVERSION = 0.30
    WEIGHT_PERSISTENCE = 0.20
    WEIGHT_OI = 0.10
    
    # Configuration
    HISTORY_SIZE = 168  # 7 days of hourly data
    MIN_HISTORY = 24    # Minimum 24 hours for prediction
    CACHE_TTL = 60      # Cache predictions for 60 seconds
    
    # Thresholds
    MIN_SPREAD_BPS = 0.5    # Minimum spread in basis points
    HIGH_CONFIDENCE = 0.7
    MEDIUM_CONFIDENCE = 0.5
    
    def __init__(self):
        # Historical data per symbol
        self._history: Dict[str, deque] = {}
        
        # Prediction cache
        self._cache: Dict[str, Tuple[PredictionResult, float]] = {}
        
        # Statistics
        self._stats = {
            "predictions_made": 0,
            "cache_hits": 0,
            "avg_confidence": 0.0,
        }
        
        self._lock = asyncio.Lock()

    async def add_observation(
        self,
        symbol: str,
        rate_x10: float,
        rate_lighter: float,
        mark_price: float = 0.0,
        open_interest: float = 0.0,
        timestamp: Optional[int] = None
    ):
        """Add a new funding rate observation"""
        async with self._lock:
            if symbol not in self._history:
                self._history[symbol] = deque(maxlen=self. HISTORY_SIZE)
            
            snapshot = FundingSnapshot(
                timestamp=timestamp or int(time. time() * 1000),
                rate_x10=rate_x10,
                rate_lighter=rate_lighter,
                spread=rate_lighter - rate_x10,
                mark_price=mark_price,
                open_interest=open_interest,
            )
            
            self._history[symbol].append(snapshot)
            
            # Invalidate cache
            self._cache.pop(symbol, None)

    async def predict(
        self,
        symbol: str,
        current_rate_x10: Optional[float] = None,
        current_rate_lighter: Optional[float] = None,
        min_apy: float = 0.0
    ) -> Optional[PredictionResult]:
        """
        Predict funding rate spread for a symbol. 
        
        Returns:
            PredictionResult or None if insufficient data
        """
        # Check cache
        if symbol in self._cache:
            result, cached_at = self._cache[symbol]
            if time.time() - cached_at < self. CACHE_TTL:
                self._stats["cache_hits"] += 1
                return result
        
        async with self._lock:
            history = self._history. get(symbol)
            
            if not history or len(history) < self.MIN_HISTORY:
                logger.debug(f"[PREDICT] {symbol}: Insufficient history ({len(history) if history else 0}/{self.MIN_HISTORY})")
                return None
            
            # Use provided rates or latest from history
            if current_rate_x10 is None or current_rate_lighter is None:
                latest = history[-1]
                current_rate_x10 = latest.rate_x10
                current_rate_lighter = latest. rate_lighter
            
            current_spread = current_rate_lighter - current_rate_x10
            
            # Calculate signals
            signals = {}
            
            # 1. Momentum Signal
            momentum_signal, momentum_value = self._calculate_momentum(history)
            signals['momentum'] = momentum_value
            
            # 2. Mean Reversion Signal
            mean_rev_signal, mean_rev_value, hist_avg = self._calculate_mean_reversion(history, current_spread)
            signals['mean_reversion'] = mean_rev_value
            
            # 3. Persistence Signal
            persistence_signal, persistence_value = self._calculate_persistence(history)
            signals['persistence'] = persistence_value
            
            # 4. Open Interest Signal
            oi_signal, oi_value = self._calculate_oi_signal(history)
            signals['open_interest'] = oi_value
            
            # Combine signals
            weighted_signal = (
                momentum_signal * self. WEIGHT_MOMENTUM +
                mean_rev_signal * self.WEIGHT_MEAN_REVERSION +
                persistence_signal * self. WEIGHT_PERSISTENCE +
                oi_signal * self. WEIGHT_OI
            )
            
            # Predict spread
            # Base prediction: current spread + momentum adjustment
            momentum_adjustment = momentum_value * 0.1  # Scale momentum impact
            
            # Mean reversion adjustment
            mean_rev_adjustment = (hist_avg - current_spread) * 0.2  # 20% pull to mean
            
            predicted_spread = current_spread + momentum_adjustment + mean_rev_adjustment
            
            # Calculate confidence
            confidence = self._calculate_confidence(history, signals, current_spread)
            
            # Detect regime
            regime = self._detect_regime(history)
            
            # Calculate APY (annualized)
            # Funding paid every 8 hours = 3x per day = 1095x per year
            predicted_apy = abs(predicted_spread) * 1095 * 100  # in percent
            
            # Determine direction
            if predicted_spread > 0:
                # Lighter rate > X10 rate → Long on X10, Short on Lighter
                predicted_direction = "long_x10"
            else:
                # X10 rate > Lighter rate → Long on Lighter, Short on X10
                predicted_direction = "long_lighter"
            
            # Determine signal strength (-2 to +2)
            if abs(weighted_signal) > 1.5:
                signal_strength = 2 if weighted_signal > 0 else -2
            elif abs(weighted_signal) > 0.5:
                signal_strength = 1 if weighted_signal > 0 else -1
            else:
                signal_strength = 0
            
            # Should trade?
            should_trade = (
                predicted_apy >= min_apy and
                confidence >= self. MEDIUM_CONFIDENCE and
                abs(current_spread) >= self.MIN_SPREAD_BPS / 10000
            )
            
            # Size multiplier based on confidence
            if confidence >= self.HIGH_CONFIDENCE:
                size_multiplier = 1.5
            elif confidence >= self. MEDIUM_CONFIDENCE:
                size_multiplier = 1.0
            else:
                size_multiplier = 0.5
            
            result = PredictionResult(
                symbol=symbol,
                timestamp=int(time. time() * 1000),
                predicted_spread=predicted_spread,
                predicted_apy=predicted_apy,
                predicted_direction=predicted_direction,
                confidence=confidence,
                signal_strength=signal_strength,
                signals=signals,
                current_spread=current_spread,
                historical_avg_spread=hist_avg,
                regime=regime,
                should_trade=should_trade,
                recommended_size_multiplier=size_multiplier,
            )
            
            # Cache result
            self._cache[symbol] = (result, time.time())
            
            # Update stats
            self._stats["predictions_made"] += 1
            alpha = 0.1
            self._stats["avg_confidence"] = (
                alpha * confidence + (1 - alpha) * self._stats["avg_confidence"]
            )
            
            return result

    def _calculate_momentum(
        self, 
        history: deque
    ) -> Tuple[float, float]:
        """
        Calculate momentum signal from recent spread changes.
        
        Returns:
            (signal: -1 to 1, raw_momentum: float)
        """
        if len(history) < 3:
            return 0.0, 0.0
        
        # Use last 12 periods (12 hours)
        recent = list(history)[-12:]
        spreads = [s.spread for s in recent]
        
        if len(spreads) < 3:
            return 0.0, 0.0
        
        # Calculate rate of change
        changes = [spreads[i] - spreads[i-1] for i in range(1, len(spreads))]
        avg_change = sum(changes) / len(changes)
        
        # Normalize to -1 to 1
        # Assume max reasonable change is 0. 001 (10 bps)
        signal = max(-1.0, min(1.0, avg_change / 0.001))
        
        return signal, avg_change

    def _calculate_mean_reversion(
        self, 
        history: deque,
        current_spread: float
    ) -> Tuple[float, float, float]:
        """
        Calculate mean reversion signal. 
        
        Returns:
            (signal: -1 to 1, deviation: float, historical_avg: float)
        """
        spreads = [s. spread for s in history]
        
        if not spreads:
            return 0.0, 0.0, 0.0
        
        historical_avg = sum(spreads) / len(spreads)
        deviation = current_spread - historical_avg
        
        # Calculate standard deviation
        if len(spreads) >= 2:
            std_dev = statistics.stdev(spreads)
        else:
            std_dev = 0.0001  # Default
        
        # Z-score
        if std_dev > 0:
            z_score = deviation / std_dev
        else:
            z_score = 0.0
        
        # Mean reversion signal: high deviation = expect reversion
        # Negative signal means expect spread to decrease
        # Positive signal means expect spread to increase
        signal = max(-1.0, min(1.0, -z_score / 2))  # Invert and scale
        
        return signal, deviation, historical_avg

    def _calculate_persistence(
        self, 
        history: deque
    ) -> Tuple[float, float]:
        """
        Calculate spread persistence (autocorrelation). 
        
        Higher persistence = spread likely to continue
        
        Returns:
            (signal: -1 to 1, autocorr: float)
        """
        if len(history) < 10:
            return 0.0, 0.0
        
        spreads = [s. spread for s in history][-24:]  # Last 24 hours
        
        if len(spreads) < 10:
            return 0.0, 0.0
        
        # Simple autocorrelation (lag 1)
        n = len(spreads)
        mean = sum(spreads) / n
        
        numerator = sum((spreads[i] - mean) * (spreads[i-1] - mean) for i in range(1, n))
        denominator = sum((s - mean) ** 2 for s in spreads)
        
        if denominator == 0:
            return 0.0, 0.0
        
        autocorr = numerator / denominator
        
        # High autocorrelation = high persistence
        # If current spread is positive and persistence is high, expect it to stay positive
        current_sign = 1 if spreads[-1] > 0 else -1
        signal = autocorr * current_sign
        
        return signal, autocorr

    def _calculate_oi_signal(
        self, 
        history: deque
    ) -> Tuple[float, float]:
        """
        Calculate open interest signal. 
        
        Rising OI with stable spread = good
        Falling OI = potential reversal
        
        Returns:
            (signal: -1 to 1, oi_change_pct: float)
        """
        if len(history) < 6:
            return 0.0, 0.0
        
        recent = list(history)[-6:]
        oi_values = [s.open_interest for s in recent if s.open_interest > 0]
        
        if len(oi_values) < 2:
            return 0.0, 0.0
        
        # OI change percentage
        oi_start = oi_values[0]
        oi_end = oi_values[-1]
        
        if oi_start == 0:
            return 0.0, 0.0
        
        oi_change_pct = (oi_end - oi_start) / oi_start
        
        # Rising OI = bullish for spread persistence
        signal = max(-1.0, min(1.0, oi_change_pct * 10))  # Scale
        
        return signal, oi_change_pct

    def _calculate_confidence(
        self,
        history: deque,
        signals: Dict[str, float],
        current_spread: float
    ) -> float:
        """
        Calculate prediction confidence (0.0 to 1.0). 
        
        Factors:
        - Signal agreement (all signals same direction)
        - Historical volatility (lower = higher confidence)
        - Data quality (more history = higher confidence)
        """
        # 1. Signal agreement (0.0 to 0.4)
        signal_values = list(signals.values())
        if signal_values:
            signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in signal_values]
            non_zero = [s for s in signs if s != 0]
            if non_zero:
                agreement = abs(sum(non_zero)) / len(non_zero)
            else:
                agreement = 0.5
        else:
            agreement = 0.5
        
        agreement_score = agreement * 0.4
        
        # 2.  Volatility (0.0 to 0. 3) - lower volatility = higher score
        spreads = [s. spread for s in history]
        if len(spreads) >= 2:
            std_dev = statistics. stdev(spreads)
            # Normalize: assume 0.001 std is "normal"
            volatility_score = max(0.0, 0.3 - (std_dev / 0.001) * 0.1)
        else:
            volatility_score = 0.15
        
        # 3. Data quality (0.0 to 0. 2)
        data_ratio = min(1.0, len(history) / self. HISTORY_SIZE)
        data_score = data_ratio * 0.2
        
        # 4. Spread significance (0.0 to 0.1)
        # Higher absolute spread = more significant
        spread_bps = abs(current_spread) * 10000
        spread_score = min(0.1, spread_bps / 50 * 0.1)  # Max at 50 bps
        
        confidence = agreement_score + volatility_score + data_score + spread_score
        return max(0.0, min(1.0, confidence))

    def _detect_regime(self, history: deque) -> MarketRegime:
        """Detect current market regime"""
        if len(history) < 12:
            return MarketRegime. UNKNOWN
        
        recent = list(history)[-24:]
        spreads = [s. spread for s in recent]
        
        if len(spreads) < 12:
            return MarketRegime.UNKNOWN
        
        # Calculate trend
        first_half = spreads[:len(spreads)//2]
        second_half = spreads[len(spreads)//2:]
        
        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)
        
        trend = second_avg - first_avg
        
        # Calculate volatility
        std_dev = statistics. stdev(spreads) if len(spreads) >= 2 else 0
        
        # Classify
        if std_dev > 0.002:  # High volatility
            return MarketRegime.HIGH_VOLATILITY
        elif std_dev < 0.0002:  # Low volatility
            return MarketRegime.LOW_VOLATILITY
        elif trend > 0.0005:
            return MarketRegime.TRENDING_UP
        elif trend < -0.0005:
            return MarketRegime. TRENDING_DOWN
        else:
            return MarketRegime.RANGING

    async def get_top_opportunities(
        self,
        symbols: List[str],
        min_apy: float = 10.0,
        min_confidence: float = 0.5,
        limit: int = 10
    ) -> List[PredictionResult]:
        """
        Get top trading opportunities sorted by expected APY.
        
        Args:
            symbols: List of symbols to analyze
            min_apy: Minimum APY threshold
            min_confidence: Minimum confidence threshold
            limit: Max results to return
        """
        results = []
        
        for symbol in symbols:
            try:
                prediction = await self.predict(symbol, min_apy=min_apy)
                if prediction and prediction.should_trade and prediction.confidence >= min_confidence:
                    results.append(prediction)
            except Exception as e:
                logger.debug(f"[PREDICT] {symbol}: Error: {e}")
        
        # Sort by APY * confidence (risk-adjusted)
        results.sort(
            key=lambda p: p.predicted_apy * p.confidence,
            reverse=True
        )
        
        return results[:limit]

    def get_history_stats(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get statistics for a symbol's history"""
        history = self._history. get(symbol)
        if not history:
            return None
        
        spreads = [s. spread for s in history]
        
        return {
            "symbol": symbol,
            "observations": len(history),
            "oldest": history[0].timestamp if history else None,
            "newest": history[-1].timestamp if history else None,
            "avg_spread": sum(spreads) / len(spreads) if spreads else 0,
            "min_spread": min(spreads) if spreads else 0,
            "max_spread": max(spreads) if spreads else 0,
            "std_dev": statistics. stdev(spreads) if len(spreads) >= 2 else 0,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get predictor statistics"""
        return {
            **self._stats,
            "symbols_tracked": len(self._history),
            "cache_size": len(self._cache),
        }

    def clear_history(self, symbol: Optional[str] = None):
        """Clear history for a symbol or all symbols"""
        if symbol:
            self._history.pop(symbol, None)
            self._cache.pop(symbol, None)
        else:
            self._history. clear()
            self._cache.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL PREDICTOR INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

_predictor: Optional[FundingPredictor] = None


def get_predictor() -> FundingPredictor:
    """Get or create the global predictor"""
    global _predictor
    if _predictor is None:
        _predictor = FundingPredictor()
    return _predictor


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def update_funding_data(
    symbol: str,
    rate_x10: float,
    rate_lighter: float,
    mark_price: float = 0.0,
    open_interest: float = 0.0
):
    """
    Update predictor with new funding data. 
    Call this from your funding rate fetcher.
    """
    predictor = get_predictor()
    await predictor.add_observation(
        symbol=symbol,
        rate_x10=rate_x10,
        rate_lighter=rate_lighter,
        mark_price=mark_price,
        open_interest=open_interest,
    )


async def get_prediction(
    symbol: str,
    min_apy: float = 0.0
) -> Optional[PredictionResult]:
    """
    Get prediction for a symbol. 
    """
    predictor = get_predictor()
    return await predictor.predict(symbol, min_apy=min_apy)


async def get_best_opportunities(
    symbols: List[str],
    min_apy: float = 10.0,
    min_confidence: float = 0.5,
    limit: int = 10
) -> List[PredictionResult]:
    """
    Get best trading opportunities. 
    """
    predictor = get_predictor()
    return await predictor. get_top_opportunities(
        symbols=symbols,
        min_apy=min_apy,
        min_confidence=min_confidence,
        limit=limit,
    )