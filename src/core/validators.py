# src/domain/risk/validators.py
# Note: This file has been moved to domain/risk/ for better organization
# NOTE: This file may not be actively used - it references models that may not exist
import logging
from typing import Any

import config

logger = logging.getLogger(__name__)


class TradeValidator:
    """
    Stateless validation logic for Tickers and Opportunities.
    Determines if a market condition is suitable for entry.

    NOTE: This class may reference models that don't exist in the current architecture.
    It's kept for compatibility but may need updates if used.
    """

    @staticmethod
    def validate_ticker(ticker: Any) -> tuple[bool, str]:
        """
        Validates basic ticker health (spread, price validity).
        """
        # Handle both dict and object access
        bid = getattr(ticker, "bid", ticker.get("bid", 0) if isinstance(ticker, dict) else 0)
        ask = getattr(ticker, "ask", ticker.get("ask", 0) if isinstance(ticker, dict) else 0)

        if bid <= 0 or ask <= 0:
            return False, "Invalid price (<=0)"

        if bid > ask:
            return False, "Crossed book (Bid > Ask)"

        # Spread Check
        # Spread = (Ask - Bid) / Bid
        spread_pct = (ask - bid) / bid if bid > 0 else 0

        # Use dynamic spread or fixed
        max_spread = getattr(config, "MAX_SPREAD_FILTER_PERCENT", 0.002)

        if spread_pct > max_spread:
            return False, f"Spread {spread_pct:.4f} > Max {max_spread:.4f}"

        return True, "OK"

    @staticmethod
    def validate_opportunity(opp: Any) -> tuple[bool, str]:
        """
        Validates a calculated arbitrage opportunity against strategy rules.

        Args:
            opp: Opportunity object or dict with 'symbol', 'estimated_apy', 'spread_pct' fields
        """
        # Handle both dict and object access
        if isinstance(opp, dict):
            symbol = opp.get("symbol", "")
            estimated_apy = opp.get("estimated_apy", 0)
            spread_pct = opp.get("spread_pct", 0)
            orderbook_depth = opp.get("orderbook_depth_usd") or opp.get("liquidity_usd") or 0
        else:
            symbol = getattr(opp, "symbol", "")
            estimated_apy = getattr(opp, "estimated_apy", 0)
            spread_pct = getattr(opp, "spread_pct", 0)
            orderbook_depth = getattr(opp, "orderbook_depth_usd", 0)

        # 1. Blacklist
        blacklist = getattr(config, "BLACKLIST_SYMBOLS", set())
        if symbol in blacklist:
            return False, f"Blacklisted symbol {symbol}"

        # 2. Min APY (baseline) + aggressive monthly target gate
        min_apy = getattr(config, "MIN_APY_FILTER", 0.35)
        if estimated_apy < min_apy:
            return False, f"APY {estimated_apy:.2%} < Min {min_apy:.2%}"

        target_monthly_roi = getattr(config, "TARGET_MONTHLY_ROI", 0.10)
        target_annual_apy = getattr(config, "TARGET_ANNUAL_APY", target_monthly_roi * 12)
        if estimated_apy < target_annual_apy:
            return False, (
                f"APY {estimated_apy:.2%} < Target {target_annual_apy:.2%} für Monatsziel {target_monthly_roi:.0%}"
            )

        # 3. Expected Profit (Absolute USD)
        # Assuming size from settings
        size_usd = getattr(config, "DESIRED_NOTIONAL_USD", 150.0)
        # Simple estimation: Profit per year * (Hold time / Year)
        # But here checking 'Net Funding' rate per hour is better?
        # Let's trust opportunity calculation of APY which includes fees/slippage estimates ideally

        # 3b. Monthly ROI sanity (per-position capital efficiency)
        expected_monthly_pnl = (estimated_apy / 12) * size_usd
        min_monthly_pnl = target_monthly_roi * size_usd
        if expected_monthly_pnl < min_monthly_pnl:
            return False, (
                f"Erwarteter Monats-PnL ${expected_monthly_pnl:.2f} < Ziel ${min_monthly_pnl:.2f} "
                f"({target_monthly_roi:.0%})"
            )

        # 4. Spread + Depth validity (redundant if ticker valid, but good double check)
        max_spread = getattr(config, "MAX_SPREAD_FILTER_PERCENT", 0.002)
        elite_spread = getattr(config, "ELITE_SPREAD_FILTER_PERCENT", max_spread)
        spread_cap = min(max_spread, elite_spread) if elite_spread > 0 else max_spread
        if spread_pct > spread_cap:
            return False, f"Spread {spread_pct:.4f} > Max {spread_cap:.4f}"

        elite_depth = getattr(config, "ELITE_MIN_ORDERBOOK_DEPTH_USD", 0)
        if elite_depth and orderbook_depth and orderbook_depth < elite_depth:
            return False, f"Liquidity ${orderbook_depth:.0f} < Elite-Min ${elite_depth:.0f}"

        return True, "OK"
