# src/prediction.py - KOMPLETT ERSETZEN

import logging
import time
from typing import Tuple, Optional
from decimal import Decimal
import config

logger = logging.getLogger(__name__)

def calculate_smart_size(
    base_notional: float,
    apy_pct: float,
    spread_pct: float,
    confidence: float = 0.5,
    open_interest: float = 0.0,  # NEU
    max_oi_fraction: float = 0.02  # Max 2% des OI
) -> float:
    """
    Berechnet optimale Trade-Größe basierend auf:
    - APY (höher = größer)
    - Spread (niedriger = größer)
    - Confidence (höher = größer)
    - Open Interest (limitiert max size)
    """
    # Basis-Multiplikatoren
    apy_mult = min(2.0, 1.0 + (apy_pct / 100))  # +100% bei 100% APY
    spread_mult = max(0.5, 1.0 - (spread_pct * 50))  # -50% bei 1% spread
    conf_mult = 0.5 + (confidence * 0.5)  # 0.5x bis 1.0x
    
    # Kombinierter Multiplikator
    combined_mult = apy_mult * spread_mult * conf_mult
    
    # Basis-Größe berechnen
    size = base_notional * combined_mult
    
    # ============================================================
    # NEU: OI-basiertes Limit
    # ============================================================
    if open_interest > 0:
        max_size_by_oi = open_interest * max_oi_fraction
        if size > max_size_by_oi:
            logger.debug(f"Size limited by OI: ${size:.2f} → ${max_size_by_oi:.2f} (OI=${open_interest:.0f})")
            size = max_size_by_oi
    
    # Minimum und Maximum
    size = max(15.0, min(size, getattr(config, 'MAX_NOTIONAL_USD', 500)))
    
    return size


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