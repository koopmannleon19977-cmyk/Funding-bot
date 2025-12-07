# src/orderbook_utils.py - Orderbook Imbalance Calculation Utilities
"""
Helper functions for orderbook data processing.
Used by WebSocketManager to calculate and feed orderbook imbalance to FundingPredictorV2.
"""


from src.utils import safe_float


def calculate_orderbook_imbalance(bids: list, asks: list, depth: int = 5) -> float:
    """
    Calculate orderbook imbalance from top N levels.
    
    Returns: float between -1.0 (all asks) and +1.0 (all bids)
    - Positive = more bid volume (buying pressure)
    - Negative = more ask volume (selling pressure)
    
    Used by FundingPredictorV2 to enhance funding rate predictions.
    
    Args:
        bids: List of bid orders [[price, size], ...] or [{"price": p, "size": s}, ...]
        asks: List of ask orders [[price, size], ...] or [{"price": p, "size": s}, ...]
        depth: Number of levels to consider (default: 5)
    
    Returns:
        float: Imbalance ratio from -1.0 to +1.0
    """
    if not bids and not asks:
        return 0.0
    
    # Sum volume from top N levels
    # Format: [[price, size], ...] or [{"price": p, "size": s}, ...]
    bid_vol = 0.0
    for b in bids[:depth]:
        if isinstance(b, (list, tuple)) and len(b) >= 2:
            bid_vol += safe_float(b[1])
        elif isinstance(b, dict):
            bid_vol += safe_float(b.get("size") or b.get("s") or b.get("quantity", 0))
    
    ask_vol = 0.0
    for a in asks[:depth]:
        if isinstance(a, (list, tuple)) and len(a) >= 2:
            ask_vol += safe_float(a[1])
        elif isinstance(a, dict):
            ask_vol += safe_float(a.get("size") or a.get("s") or a.get("quantity", 0))
    
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    
    return (bid_vol - ask_vol) / total
