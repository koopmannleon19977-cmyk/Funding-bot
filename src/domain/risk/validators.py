# src/domain/risk/validators.py
# Note: This file has been moved to domain/risk/ for better organization
# NOTE: This file may not be actively used - it references models that may not exist
from decimal import Decimal
from typing import List, Tuple, Any, Dict

import config
import logging

logger = logging.getLogger(__name__)

class TradeValidator:
    """
    Stateless validation logic for Tickers and Opportunities.
    Determines if a market condition is suitable for entry.
    
    NOTE: This class may reference models that don't exist in the current architecture.
    It's kept for compatibility but may need updates if used.
    """

    @staticmethod
    def validate_ticker(ticker: Any) -> Tuple[bool, str]:
        """
        Validates basic ticker health (spread, price validity).
        """
        # Handle both dict and object access
        bid = getattr(ticker, 'bid', ticker.get('bid', 0) if isinstance(ticker, dict) else 0)
        ask = getattr(ticker, 'ask', ticker.get('ask', 0) if isinstance(ticker, dict) else 0)
        
        if bid <= 0 or ask <= 0:
            return False, "Invalid price (<=0)"
        
        if bid > ask:
            return False, "Crossed book (Bid > Ask)"

        # Spread Check
        # Spread = (Ask - Bid) / Bid
        spread_pct = (ask - bid) / bid if bid > 0 else 0
        
        # Use dynamic spread or fixed
        max_spread = getattr(config, 'MAX_SPREAD_FILTER_PERCENT', 0.002)
        
        if spread_pct > max_spread:
            return False, f"Spread {spread_pct:.4f} > Max {max_spread:.4f}"
            
        return True, "OK"

    @staticmethod
    def validate_opportunity(opp: Any) -> Tuple[bool, str]:
        """
        Validates a calculated arbitrage opportunity against strategy rules.
        
        Args:
            opp: Opportunity object or dict with 'symbol', 'estimated_apy', 'spread_pct' fields
        """
        # Handle both dict and object access
        if isinstance(opp, dict):
            symbol = opp.get('symbol', '')
            estimated_apy = opp.get('estimated_apy', 0)
            spread_pct = opp.get('spread_pct', 0)
        else:
            symbol = getattr(opp, 'symbol', '')
            estimated_apy = getattr(opp, 'estimated_apy', 0)
            spread_pct = getattr(opp, 'spread_pct', 0)
        
        # 1. Blacklist
        blacklist = getattr(config, 'BLACKLIST_SYMBOLS', set())
        if symbol in blacklist:
            return False, f"Blacklisted symbol {symbol}"

        # 2. Min APY
        min_apy = getattr(config, 'MIN_APY_FILTER', 0.35)
        if estimated_apy < min_apy:
            return False, f"APY {estimated_apy:.2%} < Min {min_apy:.2%}"

        # 3. Expected Profit (Absolute USD)
        # Assuming size from settings
        size_usd = getattr(config, 'DESIRED_NOTIONAL_USD', 150.0)
        # Simple estimation: Profit per year * (Hold time / Year)
        # But here checking 'Net Funding' rate per hour is better?
        # Let's trust opportunity calculation of APY which includes fees/slippage estimates ideally
        
        # 4. Spread Validity (Redundant if ticker valid, but good double check)
        max_spread = getattr(config, 'MAX_SPREAD_FILTER_PERCENT', 0.002)
        if spread_pct > max_spread:
             return False, f"Spread {spread_pct:.4f} > Max {max_spread:.4f}"

        return True, "OK"
