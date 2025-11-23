# src/prediction.py - KOMPLETT ERSETZEN

import logging
import time
from typing import Tuple, Optional
from decimal import Decimal
import config

logger = logging.getLogger(__name__)

def calculate_smart_size(confidence: float, total_balance: float) -> float:
    """
    Kelly-Criterion basierte Position Sizing
    
    Args:
        confidence: 0.0-1.0 (from prediction)
        total_balance: Total USD balance
    
    Returns:
        Position size in USD
    """
    BASE_SIZE = config.DESIRED_NOTIONAL_USD
    
    # Kelly Formula (simplified for funding arb):
    # f = (p * b - q) / b
    # where p = win_prob, q = loss_prob, b = win/loss ratio
    
    # Map confidence to win probability
    # confidence 0.5 = 50% edge, 0.85 = 85% edge
    win_prob = 0.5 + (confidence * 0.5)  # 0.5-1.0 range
    loss_prob = 1.0 - win_prob
    
    # Funding arb has asymmetric payoff:
    # Win: ~0.15% per 8h (funding payment)
    # Loss: ~0.15% per 8h (if flipped)
    # → b = 1.0 (symmetric)
    win_loss_ratio = 1.0
    
    # Kelly fraction
    kelly_fraction = (win_prob * win_loss_ratio - loss_prob) / win_loss_ratio
    
    # Apply Kelly with safety factor (0.25 = Quarter Kelly)
    # Full Kelly is too aggressive for live trading
    KELLY_SAFETY_FACTOR = 0.25
    optimal_fraction = kelly_fraction * KELLY_SAFETY_FACTOR
    
    # Calculate raw size
    raw_size = total_balance * optimal_fraction
    
    # Apply confidence-based multipliers
    if confidence >= 0.85:
        multiplier = config.POSITION_SIZE_MULTIPLIERS.get("high", 2.0)
    elif confidence >= 0.75:
        multiplier = config.POSITION_SIZE_MULTIPLIERS.get("medium", 1.5)
    elif confidence >= 0.65:
        multiplier = config.POSITION_SIZE_MULTIPLIERS.get("normal", 1.2)
    else:
        multiplier = config.POSITION_SIZE_MULTIPLIERS.get("low", 0.8)
    
    # Final size
    final_size = BASE_SIZE * multiplier
    
    # Hard caps
    min_size = total_balance * config.MIN_POSITION_SIZE_PCT
    max_size = total_balance * config.MAX_POSITION_SIZE_PCT
    
    final_size = max(min_size, min(final_size, max_size))
    final_size = max(config.MIN_POSITION_SIZE_USD, min(final_size, config.MAX_TRADE_SIZE_USD))
    
    # Never risk more than 5% of balance on single trade
    MAX_RISK_PCT = 0.05
    final_size = min(final_size, total_balance * MAX_RISK_PCT)
    
    logger.debug(
        f"Kelly Sizing: conf={confidence:.2f}, kelly_f={kelly_fraction:.3f}, "
        f"optimal_f={optimal_fraction:.3f}, mult={multiplier:.1f}x, "
        f"size=${final_size:.2f}"
    )
    
    return final_size


def predict_next_funding_rates(
    lighter_adapter,
    x10_adapter, 
    symbol: str,
    current_lighter_rate: float,
    current_x10_rate: float
) -> Tuple[float, float]:
    """
    Legacy function - returns 0.0, 0.0
    Use prediction_v2.py instead
    """
    return 0.0, 0.0