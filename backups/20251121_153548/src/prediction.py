# src/prediction.py
import logging
from typing import Tuple
import config

logger = logging.getLogger(__name__)

async def predict_next_funding_rates(
    symbol: str,
    btc_trend_pct: float = 0.0
) -> Tuple[float, float, float]:
    """
    Predicts next funding rates with BTC correlation.
    
    Args:
        symbol: Trading pair (e.g. "ETH-USD")
        btc_trend_pct: BTC 24h change in % (e.g. 5.2 for +5.2%)
    
    Returns:
        (predicted_lighter_rate, predicted_x10_rate, confidence)
    """
    # Placeholder: Erweitere mit ML-Model
    # Für jetzt: Simple Heuristic
    
    base_confidence = 0.5
    
    # BTC Momentum Boost
    if abs(btc_trend_pct) > 3.0:
        # Strong momentum → higher confidence
        base_confidence += 0.2
    
    # Symbol-specific adjustments
    if symbol.startswith("BTC"):
        base_confidence += 0.1
    elif symbol.startswith(("ETH", "SOL")):
        base_confidence += 0.05
    
    confidence = min(base_confidence, 1.0)
    
    return 0.0, 0.0, confidence

def calculate_smart_size(confidence: float, total_balance: float) -> float:
    """
    Kelly-Lite Sizing basierend auf Confidence.
    
    Args:
        confidence: 0.0-1.0 (Vorhersage-Qualität)
        total_balance: Gesamte verfügbare Balance
    
    Returns:
        Position size in USD
    """
    # Base Size
    base_size = config.DESIRED_NOTIONAL_USD
    
    # Confidence Multiplier (0.5 - 1.5x)
    multiplier = 0.5 + (confidence * 1.0)
    
    # Calculate size
    size = base_size * multiplier
    
    # Safety Caps
    size = max(size, config.MIN_POSITION_SIZE_USD)
    size = min(size, config.MAX_TRADE_SIZE_USD)
    
    # Balance Check (max 5% of balance)
    max_allowed = total_balance * 0.05
    size = min(size, max_allowed)
    
    logger.debug(f"Smart Size: Confidence={confidence:.2f} → ${size:.2f}")
    return size