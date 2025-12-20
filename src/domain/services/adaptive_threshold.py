# src/domain/services/adaptive_threshold.py
# Note: This file has been moved to domain/services/ for better organization
import time
import logging
from collections import deque
from statistics import mean
from typing import Optional
import config

logger = logging.getLogger(__name__)


class AdaptiveThresholdManager:
    """Adaptive threshold manager with configurable rebate logic.

    Features:
    - Maintains a sliding window of recent funding-rate magnitudes
    - Recalculates a base APY threshold on an interval
    - Computes a configurable, annualized rebate discount for maker trades
    - Applies symbol-specific adjustments and rebate discounts to the threshold
    """

    def __init__(self, window_size: int = 100):
        self.rate_history = deque(maxlen=window_size)
        self.current_threshold = getattr(config, 'MIN_APY_FILTER', 0.10)
        self.last_update = 0
        self.update_interval = getattr(config, 'THRESHOLD_UPDATE_INTERVAL', 300)
        self.min_limit = getattr(config, 'MIN_APY_FALLBACK', 0.05)

        # Known rebate/high-liquidity symbols (can be overridden by config)
        self.rebate_pairs = set(getattr(config, 'REBATE_PAIRS', {"BTC-USD", "ETH-USD", "SOL-USD", "ARB-USD"}))
        # Optional prefixes to match (like 'BTC' matches 'BTC-USD')
        self.rebate_prefixes = tuple(getattr(config, 'REBATE_PREFIXES', ("BTC", "ETH")))

        # Fee settings (per-trade fraction: e.g. 0.0005 = 0.05%)
        # Prefer newer generic names but fall back to older exchange-specific keys
        self.taker_fee = getattr(config, 'TAKER_FEE', getattr(config, 'TAKER_FEE_X10', 0.0005))
        self.maker_fee = getattr(config, 'MAKER_FEE', getattr(config, 'MAKER_FEE_X10', 0.0))

        # Rebate annualization settings
        self.rebate_trades_per_day = getattr(config, 'REBATE_TRADES_PER_DAY', 1)
        self.rebate_max_annual_discount = getattr(config, 'REBATE_MAX_ANNUAL_DISCOUNT', 0.05)
        self.rebate_min_annual_discount = getattr(config, 'REBATE_MIN_ANNUAL_DISCOUNT', 0.001)

    def update_metrics(self, funding_rates: list):
        """Append recent funding rate magnitudes and recalc threshold periodically."""
        if not funding_rates:
            return
        avg_rate = mean([abs(r) for r in funding_rates])
        self.rate_history.append(avg_rate)

        now = time.time()
        if now - self.last_update > self.update_interval:
            self._recalculate_threshold()
            self.last_update = now

    def _recalculate_threshold(self):
        if len(self.rate_history) < 10:
            return

        market_avg_apy = mean(self.rate_history) * 24 * 365

        # Conservative baselines depending on regime
        if market_avg_apy > 0.25:
            new_threshold = 0.06
            regime = "HOT"
        elif market_avg_apy > 0.10:
            new_threshold = 0.10
            regime = "NORMAL"
        else:
            new_threshold = 0.15
            regime = "COLD"

        # Smooth the threshold to avoid abrupt jumps
        self.current_threshold = (self.current_threshold * 0.7) + (new_threshold * 0.3)
        # Clamp to min_limit
        self.current_threshold = max(self.min_limit, self.current_threshold)

        logger.info(
            f"REGIME: {regime} (Avg APY: {market_avg_apy*100:.2f}%) | "
            f"Base Threshold: {self.current_threshold*100:.2f}%"
        )

    def should_trade_for_rebate(self, symbol: Optional[str], is_maker: bool) -> bool:
        """Return True if conditions suggest lowering threshold to capture maker rebate.

        This is a convenience wrapper around `get_rebate_discount` returning whether
        a non-zero rebate discount is available and we are in maker mode.
        """
        return self.get_rebate_discount(symbol, is_maker) > 0.0

    def get_rebate_discount(self, symbol: Optional[str], is_maker: bool) -> float:
        """Compute an annualized APY discount (fraction) from maker/taker fee differences.

        The returned value is an absolute APY discount (e.g. 0.02 for 2% APY) that can be
        subtracted from the base threshold when choosing trades as a maker.
        """
        if not is_maker:
            return 0.0

        per_trade_saving = self.taker_fee - self.maker_fee
        if per_trade_saving <= 0:
            # No saving (or makers are more expensive) => no rebate advantage
            return 0.0

        annualized_saving = per_trade_saving * max(0.0, float(self.rebate_trades_per_day)) * 365.0

        # Apply a boost for known high-liquidity / explicit rebate pairs
        boost = 1.0
        if symbol:
            if symbol in self.rebate_pairs:
                boost = max(boost, 1.5)
            else:
                for p in self.rebate_prefixes:
                    if symbol.startswith(p):
                        boost = max(boost, 1.25)
                        break

        discount = min(self.rebate_max_annual_discount, annualized_saving * boost)

        if discount < self.rebate_min_annual_discount:
            return 0.0

        return float(discount)

    def get_threshold(self, symbol: Optional[str] = None, is_maker: bool = False) -> float:
        """Return the effective APY threshold taking symbol-specific and rebate discounts into account.

        - Applies conservative adjustments for large-cap prefixes (BTC/ETH)
        - Penalizes speculative/meme symbols by raising threshold
        - Lowers the threshold by an annualized rebate discount when being maker
        """
        base = float(self.current_threshold)

        # Symbol-specific adjustments
        # Symbol-specific adjustments
        if symbol:
            # REMOVED: BTC/ETH discount (user wants strict MIN_APY)
            # if symbol.startswith(("BTC", "ETH")):
            #     base = max(self.min_limit, base * 0.8)
            
            if symbol in getattr(config, 'HIGH_RISK_SYMBOLS', ["HYPE-USD", "MEME-USD"]):
                base = base * 1.5

        # Maker/Rebate Discount: compute dynamic discount rather than a fixed block
        discount = self.get_rebate_discount(symbol, is_maker)
        if discount > 0:
            # Lower threshold by the computed annual discount but don't drop below a safe floor
            safe_floor = getattr(config, 'MIN_SAFE_THRESHOLD', 0.03)
            new_base = base - discount
            base = max(self.min_limit, max(safe_floor, new_base))
            if discount > 0.01:
                logger.info(f"Rebate discount {symbol or 'unknown'}: -{discount*100:.2f}% APY -> threshold {base*100:.2f}%")

        return max(self.min_limit, base)

    def get_stats(self) -> dict:
        """Get current stats for monitoring"""
        return {
            "current_threshold": self.current_threshold,
            "samples": len(self.rate_history),
            "market_avg_apy": (mean(self.rate_history) * 24 * 365) if self.rate_history else 0.0
        }


_manager: Optional[AdaptiveThresholdManager] = None


def get_threshold_manager() -> AdaptiveThresholdManager:
    global _manager
    if _manager is None:
        _manager = AdaptiveThresholdManager()
    return _manager