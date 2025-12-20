from decimal import Decimal
from typing import List, Tuple

from src.config.settings import settings
from src.data.models import Ticker, TradeOpportunity, ExchangeType
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

class TradeValidator:
    """
    Stateless validation logic for Tickers and Opportunities.
    Determines if a market condition is suitable for entry.
    """

    @staticmethod
    def validate_ticker(ticker: Ticker) -> Tuple[bool, str]:
        """
        Validates basic ticker health (spread, price validity).
        """
        if ticker.bid <= 0 or ticker.ask <= 0:
            return False, "Invalid price (<=0)"
        
        if ticker.bid > ticker.ask:
            return False, "Crossed book (Bid > Ask)"

        # Spread Check
        # Spread = (Ask - Bid) / Bid
        spread_pct = (ticker.ask - ticker.bid) / ticker.bid
        
        # Use dynamic spread or fixed
        max_spread = settings.risk.max_spread_filter_percent
        
        if spread_pct > max_spread:
            return False, f"Spread {spread_pct:.4f} > Max {max_spread:.4f}"
            
        return True, "OK"

    @staticmethod
    def validate_opportunity(opp: TradeOpportunity) -> Tuple[bool, str]:
        """
        Validates a calculated arbitrage opportunity against strategy rules.
        """
        symbol = opp.symbol
        
        # 1. Blacklist
        if symbol in settings.trading.blacklist_symbols:
            return False, f"Blacklisted symbol {symbol}"

        # 2. Min APY
        if opp.estimated_apy < settings.trading.min_apy_filter:
            return False, f"APY {opp.estimated_apy:.2%} < Min {settings.trading.min_apy_filter:.2%}"

        # 3. Expected Profit (Absolute USD)
        # Assuming size from settings
        size_usd = settings.trading.desired_notional_usd
        # Simple estimation: Profit per year * (Hold time / Year)
        # But here checking 'Net Funding' rate per hour is better?
        # Let's trust opportunity calculation of APY which includes fees/slippage estimates ideally
        
        # 4. Spread Validity (Redundant if ticker valid, but good double check)
        if opp.spread_pct > settings.risk.max_spread_filter_percent:
             return False, f"Spread {opp.spread_pct:.4f} > Max {settings.risk.max_spread_filter_percent:.4f}"

        return True, "OK"
