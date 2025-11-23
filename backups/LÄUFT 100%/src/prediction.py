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
    ✅ PHASE 2: BTC Correlation + Symbol Intelligence
    
    Args:
        symbol: Trading pair (e.g. "ETH-USD")
        btc_trend_pct: BTC 24h change in % (e.g. 5.2 for +5.2%)
    
    Returns:
        (0.0, 0.0, confidence)
    """
    base_confidence = 0.5
    
    # ===== 1. BTC MOMENTUM BOOST =====
    btc_abs = abs(btc_trend_pct)
    
    if btc_abs > config.BTC_STRONG_MOMENTUM_PCT:
        base_confidence += 0.3
    elif btc_abs > config.BTC_MEDIUM_MOMENTUM_PCT:
        base_confidence += 0.2
    elif btc_abs > config.BTC_WEAK_MOMENTUM_PCT:
        base_confidence += 0.1
    
    # ===== 2. SYMBOL-SPECIFIC BOOST =====
    symbol_base = symbol.replace("-USD", "")
    
    for prefix, boost in config.SYMBOL_CONFIDENCE_BOOST.items():
        if symbol_base.startswith(prefix):
            base_confidence += boost
            break
    
    # ===== 3. CAP CONFIDENCE =====
    confidence = min(base_confidence, 0.95)
    
    return 0.0, 0.0, confidence


def calculate_smart_size(confidence: float, total_balance: float) -> float:
    """
    ✅ PHASE 2: Kelly-Lite Position Sizing
    
    Uses confidence tiers for aggressive-conservative sizing.
    Respects balance limits to prevent over-exposure.
    
    Args:
        confidence: 0.0-1.0 (Vorhersage-Qualität)
        total_balance: Gesamte verfügbare Balance
    
    Returns:
        Position size in USD
    """
    # ===== 1. BASE SIZE =====
    base_size = config.DESIRED_NOTIONAL_USD
    
    # ===== 2. CONFIDENCE MULTIPLIER =====
    if confidence >= 0.8:
        multiplier = config.POSITION_SIZE_MULTIPLIERS["high"]
    elif confidence >= 0.7:
        multiplier = config.POSITION_SIZE_MULTIPLIERS["medium"]
    elif confidence >= 0.6:
        multiplier = config.POSITION_SIZE_MULTIPLIERS["normal"]
    else:
        multiplier = config.POSITION_SIZE_MULTIPLIERS["low"]
    
    size = base_size * multiplier
    
    # ===== 3. HARD CAPS =====
    size = max(size, config.MIN_POSITION_SIZE_USD)
    size = min(size, config.MAX_TRADE_SIZE_USD)
    
    # ===== 4. BALANCE SAFETY =====
    max_allowed = total_balance * config.MAX_POSITION_SIZE_PCT
    min_required = total_balance * config.MIN_POSITION_SIZE_PCT
    size = max(size, min_required)
    size = min(size, max_allowed)
    
    logger.debug(
        f"Smart Size: balance={total_balance:.2f} conf={confidence:.2f} -> size={size:.2f} (base={base_size})"
    )
    return size