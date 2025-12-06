import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any

logger = logging.getLogger(__name__)

async def should_hold_for_funding(
    trade: Dict[str, Any], 
    x10_adapter: Any, 
    lighter_adapter: Any
) -> bool:
    """
    Determines if we should delay closing a trade to capture funding.
    Returns True if we should HOLD (delay close).
    
    Logic:
    1. Check if close to funding settlement (e.g. < 15 mins).
    2. Calculate net funding PnL for the specific trade sides.
    3. If earning funding (> cost threshold), return True (Hold).
    4. If paying funding (< cost threshold), return False (Close immediately).
    """
    try:
        symbol = trade.get('symbol')
        if not symbol:
            return False

        # 1. Calculate time to next funding (top of the hour)
        now = datetime.now(timezone.utc)
        # Next hour timestamp
        next_hour_ts = now.replace(minute=0, second=0, microsecond=0).timestamp() + 3600
        seconds_left = next_hour_ts - now.timestamp()
        minutes_left = seconds_left / 60.0
        
        # Only intervene if within 15 minutes of funding
        if minutes_left > 15:
            return False
            
        # 2. Get current rates
        rate_x10 = x10_adapter.funding_rates.get(symbol, 0.0) or 0.0
        rate_lighter = lighter_adapter.funding_rates.get(symbol, 0.0) or 0.0
        
        # 3. Determine Position Size
        size_usd = float(trade.get('size_usd', 0) or trade.get('notional_usd', 0) or 0)
        if size_usd == 0:
            return False

        # 4. Calculate Net Funding PnL
        # Rule: 
        # Long (BUY): Pays if Rate > 0 (PnL -= Size * Rate)
        # Short (SELL): Receives if Rate > 0 (PnL += Size * Rate)
        
        # X10 Side
        side_x10 = trade.get('side_x10', 'BUY')
        funding_x10 = 0.0
        if side_x10 == 'BUY':
            funding_x10 = size_usd * rate_x10 * -1  # Long pays positive rate
        else:
            funding_x10 = size_usd * rate_x10 * 1   # Short receives positive rate

        # Lighter Side
        side_lighter = trade.get('side_lighter', 'SELL')
        funding_lighter = 0.0
        if side_lighter == 'BUY':
            funding_lighter = size_usd * rate_lighter * -1
        else:
            funding_lighter = size_usd * rate_lighter * 1
            
        net_funding = funding_x10 + funding_lighter
        
        # 5. Decision
        # Threshold: We only care if the funding impact is significant (> $0.02)
        # This prevents holding for negligible amounts
        THRESHOLD_USD = 0.02
        
        # If we are EARNING money
        if net_funding > THRESHOLD_USD:
            logger.info(
                f"💰 [SMART EXIT] {symbol}: Holding for Funding! "
                f"Est. PnL: +${net_funding:.4f} in {minutes_left:.1f} min"
            )
            return True  # HOLD!
            
        # If we are PAYING money
        elif net_funding < -THRESHOLD_USD:
            logger.info(
                f"🏃 [SMART EXIT] {symbol}: Exiting ASAP! "
                f"Avoid Fee: -${abs(net_funding):.4f} in {minutes_left:.1f} min"
            )
            return False # CLOSE NOW!
            
        # Neutral / Insignificant
        return False

    except Exception as e:
        logger.error(f"Error in smart exit check for {trade.get('symbol')}: {e}")
        return False
