"""
Orderbook Validation Module for Maker Order Strategy

This module validates orderbook state before placing Maker orders to prevent:
- Orders placed at bad prices (empty/thin orderbook)
- Orders that never fill (no liquidity)
- Unhedged positions (one leg fills, other doesn't)

References:
- Lighter WebSocket Docs: https://apidocs.lighter.xyz/docs/websocket-reference
- Lighter OrderApi: https://github.com/elliottech/lighter-python (docs/OrderApi.md)
"""

from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from decimal import Decimal
from enum import Enum
import logging
import time

logger = logging.getLogger(__name__)


class OrderbookQuality(Enum):
    """Quality classification for orderbook state"""
    EXCELLENT = "excellent"      # Full depth, tight spread
    GOOD = "good"               # Adequate depth, reasonable spread
    MARGINAL = "marginal"       # Minimum requirements met
    INSUFFICIENT = "insufficient"  # Do not trade
    EMPTY = "empty"             # No data available
    CROSSED = "crossed"         # Ask < bid - invalid book


class ExchangeProfile(Enum):
    """Exchange-specific validation profiles"""
    LIGHTER = "lighter"
    X10 = "x10"
    DEFAULT = "default"


@dataclass
class ValidationProfile:
    """Exchange-specific validation thresholds"""
    min_depth_usd: float
    min_opposite_depth_usd: float
    min_bid_levels: int
    min_ask_levels: int
    max_spread_percent: float
    warn_spread_percent: float
    max_staleness_seconds: float
    warn_staleness_seconds: float
    excellent_depth_multiple: float
    good_depth_multiple: float
    marginal_depth_multiple: float


# Predefined profiles for different exchanges
VALIDATION_PROFILES: Dict[ExchangeProfile, ValidationProfile] = {
    ExchangeProfile.LIGHTER: ValidationProfile(
        min_depth_usd=50.0,               # Match trade size - Lighter WS has limited depth
        min_opposite_depth_usd=0.0,        # Don't require opposite side
        min_bid_levels=1,                  # Lighter often has only 1 level on WS
        min_ask_levels=1,                  # Lighter often has only 1 level on WS
        max_spread_percent=2.0,            # Allow wider spread on Lighter
        warn_spread_percent=1.0,           # Warning at 1%
        max_staleness_seconds=10.0,        # Lighter WS can be slower
        warn_staleness_seconds=5.0,
        excellent_depth_multiple=5.0,      # 5x trade size = excellent
        good_depth_multiple=2.0,           # 2x trade size = good
        marginal_depth_multiple=1.0,       # 1x trade size = marginal (minimum)
    ),
    ExchangeProfile.X10: ValidationProfile(
        min_depth_usd=200.0,
        min_opposite_depth_usd=100.0,
        min_bid_levels=2,
        min_ask_levels=2,
        max_spread_percent=0.5,
        warn_spread_percent=0.3,
        max_staleness_seconds=5.0,
        warn_staleness_seconds=2.0,
        excellent_depth_multiple=10.0,
        good_depth_multiple=5.0,
        marginal_depth_multiple=2.0,
    ),
    ExchangeProfile.DEFAULT: ValidationProfile(
        min_depth_usd=500.0,
        min_opposite_depth_usd=200.0,
        min_bid_levels=3,
        min_ask_levels=3,
        max_spread_percent=0.5,
        warn_spread_percent=0.3,
        max_staleness_seconds=5.0,
        warn_staleness_seconds=2.0,
        excellent_depth_multiple=10.0,
        good_depth_multiple=5.0,
        marginal_depth_multiple=2.0,
    ),
}


@dataclass
class OrderbookValidationResult:
    """Result of orderbook validation"""
    is_valid: bool
    quality: OrderbookQuality
    reason: str
    bid_depth_usd: Decimal
    ask_depth_usd: Decimal
    spread_percent: Optional[Decimal]
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    bid_levels: int
    ask_levels: int
    staleness_seconds: float
    recommended_action: str  # "proceed", "wait", "skip", "use_market_order"
    profile_used: str = "default"  # Which profile was used for validation


@dataclass
class OrderbookDepthLevel:
    """Single level in orderbook"""
    price: Decimal
    size: Decimal
    
    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass
class PriceImpactResult:
    """
    Result of price impact simulation.
    
    Simulates walking through orderbook levels to calculate
    actual execution price and slippage for a given order size.
    """
    # Input parameters
    side: str                          # BUY or SELL
    order_size_usd: Decimal            # Requested order size in USD
    
    # Execution simulation
    can_fill: bool                     # Whether full order can be filled
    filled_size_usd: Decimal           # How much could be filled
    avg_execution_price: Decimal       # Volume-weighted average price
    best_price: Decimal                # Best available price (top of book)
    worst_price: Decimal               # Worst price used in fill
    
    # Impact metrics
    slippage_percent: Decimal          # Slippage from best price to avg price
    price_impact_percent: Decimal      # Impact from mid-price to avg execution price
    levels_consumed: int               # How many orderbook levels consumed
    
    # Breakdown
    fills_by_level: List[Tuple[Decimal, Decimal, Decimal]]  # [(price, size_coins, size_usd), ...]
    
    @property
    def total_cost_usd(self) -> Decimal:
        """Total USD cost for this execution"""
        return self.filled_size_usd
    
    @property
    def is_acceptable(self) -> bool:
        """Quick check if execution is acceptable (can fill + reasonable slippage)"""
        return self.can_fill and self.slippage_percent < Decimal("1.0")  # <1% slippage


class OrderbookValidator:
    """
    Validates orderbook state before placing Maker orders.
    
    Configuration thresholds based on trade size and market conditions.
    Supports exchange-specific profiles for different orderbook characteristics.
    """
    
    def __init__(
        self,
        profile: ExchangeProfile = ExchangeProfile.DEFAULT,
        # Individual overrides (take precedence over profile)
        min_depth_usd: Optional[float] = None,
        min_opposite_depth_usd: Optional[float] = None,
        min_bid_levels: Optional[int] = None,
        min_ask_levels: Optional[int] = None,
        max_spread_percent: Optional[float] = None,
        warn_spread_percent: Optional[float] = None,
        max_staleness_seconds: Optional[float] = None,
        warn_staleness_seconds: Optional[float] = None,
        excellent_depth_multiple: Optional[float] = None,
        good_depth_multiple: Optional[float] = None,
        marginal_depth_multiple: Optional[float] = None,
    ):
        # Get base profile
        base_profile = VALIDATION_PROFILES[profile]
        self.profile_name = profile.value
        
        # Apply profile values with optional overrides
        self.min_depth_usd = Decimal(str(min_depth_usd if min_depth_usd is not None else base_profile.min_depth_usd))
        self.min_opposite_depth_usd = Decimal(str(min_opposite_depth_usd if min_opposite_depth_usd is not None else base_profile.min_opposite_depth_usd))
        self.min_bid_levels = min_bid_levels if min_bid_levels is not None else base_profile.min_bid_levels
        self.min_ask_levels = min_ask_levels if min_ask_levels is not None else base_profile.min_ask_levels
        self.max_spread_percent = Decimal(str(max_spread_percent if max_spread_percent is not None else base_profile.max_spread_percent))
        self.warn_spread_percent = Decimal(str(warn_spread_percent if warn_spread_percent is not None else base_profile.warn_spread_percent))
        self.max_staleness_seconds = max_staleness_seconds if max_staleness_seconds is not None else base_profile.max_staleness_seconds
        self.warn_staleness_seconds = warn_staleness_seconds if warn_staleness_seconds is not None else base_profile.warn_staleness_seconds
        self.excellent_depth_multiple = Decimal(str(excellent_depth_multiple if excellent_depth_multiple is not None else base_profile.excellent_depth_multiple))
        self.good_depth_multiple = Decimal(str(good_depth_multiple if good_depth_multiple is not None else base_profile.good_depth_multiple))
        self.marginal_depth_multiple = Decimal(str(marginal_depth_multiple if marginal_depth_multiple is not None else base_profile.marginal_depth_multiple))
        
        # Cache for validation results (avoid spamming logs)
        self._validation_cache: Dict[str, Tuple[float, OrderbookValidationResult]] = {}
        self._cache_ttl = 1.0  # 1 second cache
        
        logger.debug(
            f"OrderbookValidator initialized with profile={self.profile_name}: "
            f"min_depth=${self.min_depth_usd}, min_levels={self.min_bid_levels}/{self.min_ask_levels}, "
            f"max_spread={self.max_spread_percent}%"
        )

    def _calculate_spread_percent(
        self,
        best_bid: Optional[Decimal],
        best_ask: Optional[Decimal],
    ) -> Tuple[Optional[Decimal], bool]:
        """
        Calculate spread percentage and detect crossed books.

        Returns:
            (spread_percent, is_crossed)
            - spread_percent is always >= 0 when present
            - is_crossed is True if best_ask < best_bid
        """
        if best_bid is None or best_ask is None:
            return None, False

        if best_bid <= 0:
            return None, False

        if best_ask < best_bid:
            spread = abs(best_bid - best_ask) / best_bid * Decimal("100")
            logger.warning(
                f"⚠️ CROSSED BOOK DETECTED: best_ask={best_ask} < best_bid={best_bid}"
            )
            return spread, True

        spread = (best_ask - best_bid) / best_bid * Decimal("100")
        if spread < 0:
            logger.error(
                f"❌ LOGIC ERROR: Negative spread computed ({spread}) with ask >= bid"
            )
            spread = abs(spread)

        return spread, False
        
    def validate_for_maker_order(
        self,
        symbol: str,
        side: str,  # "BUY" or "SELL"
        trade_size_usd: Decimal,
        bids: List[Tuple[Decimal, Decimal]],  # [(price, size), ...]
        asks: List[Tuple[Decimal, Decimal]],  # [(price, size), ...]
        orderbook_timestamp: Optional[float] = None,
    ) -> OrderbookValidationResult:
        """
        Validate orderbook for placing a Maker order.
        
        For a SELL Maker order (we want to be filled by buyers):
        - We place on the ASK side
        - We need BIDS to exist (buyers to fill us)
        - Depth check focuses on BID side
        
        For a BUY Maker order (we want to be filled by sellers):
        - We place on the BID side
        - We need ASKS to exist (sellers to fill us)
        - Depth check focuses on ASK side
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            side: Order side - "BUY" or "SELL"
            trade_size_usd: Intended trade size in USD
            bids: List of (price, size) tuples, sorted by price descending
            asks: List of (price, size) tuples, sorted by price ascending
            orderbook_timestamp: Unix timestamp when orderbook was received
            
        Returns:
            OrderbookValidationResult with validation status and details
        """
        
        # Check cache first
        cache_key = f"{symbol}:{side}:{self.profile_name}"
        now = time.time()
        if cache_key in self._validation_cache:
            cached_time, cached_result = self._validation_cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_result
        
        # Calculate staleness
        staleness = 0.0
        if orderbook_timestamp:
            staleness = now - orderbook_timestamp
            
        # Parse orderbook levels
        bid_levels = [OrderbookDepthLevel(Decimal(str(p)), Decimal(str(s))) for p, s in bids]
        ask_levels = [OrderbookDepthLevel(Decimal(str(p)), Decimal(str(s))) for p, s in asks]
        
        # Calculate depths
        bid_depth_usd = sum(level.notional for level in bid_levels)
        ask_depth_usd = sum(level.notional for level in ask_levels)
        
        # Get best prices
        best_bid = bid_levels[0].price if bid_levels else None
        best_ask = ask_levels[0].price if ask_levels else None

        # Calculate spread with crossed book detection
        spread_percent, is_crossed = self._calculate_spread_percent(best_bid, best_ask)
            
        # Determine which side we need depth on
        # SELL Maker = we're on ask side, need buyers (bids) to fill us
        # BUY Maker = we're on bid side, need sellers (asks) to fill us
        if side.upper() == "SELL":
            relevant_depth = bid_depth_usd
            relevant_levels = len(bid_levels)
            min_relevant_levels = self.min_bid_levels
            opposite_depth = ask_depth_usd
            relevant_side_name = "bids"
        else:  # BUY
            relevant_depth = ask_depth_usd
            relevant_levels = len(ask_levels)
            min_relevant_levels = self.min_ask_levels
            opposite_depth = bid_depth_usd
            relevant_side_name = "asks"
            
        # === VALIDATION CHECKS ===
        
        reasons = []
        is_valid = True
        quality = OrderbookQuality.EXCELLENT
        recommended_action = "proceed"

        # Check 0: Crossed book (invalid regardless of other metrics)
        if is_crossed:
            result = OrderbookValidationResult(
                is_valid=False,
                quality=OrderbookQuality.CROSSED,
                reason=f"Crossed book: ask ({best_ask}) < bid ({best_bid})",
                bid_depth_usd=bid_depth_usd,
                ask_depth_usd=ask_depth_usd,
                spread_percent=spread_percent,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_levels=len(bid_levels),
                ask_levels=len(ask_levels),
                staleness_seconds=staleness,
                recommended_action="wait",
                profile_used=self.profile_name,
            )
            self._cache_result(cache_key, result)
            logger.warning(
                f"❌ {symbol} Orderbook CROSSED for {side} Maker (profile={self.profile_name}): "
                f"ask {best_ask} < bid {best_bid}"
            )
            return result
        
        # Check 1: Empty orderbook
        if not bid_levels and not ask_levels:
            result = OrderbookValidationResult(
                is_valid=False,
                quality=OrderbookQuality.EMPTY,
                reason="Orderbook is completely empty",
                bid_depth_usd=Decimal("0"),
                ask_depth_usd=Decimal("0"),
                spread_percent=None,
                best_bid=None,
                best_ask=None,
                bid_levels=0,
                ask_levels=0,
                staleness_seconds=staleness,
                recommended_action="skip",
                profile_used=self.profile_name
            )
            self._cache_result(cache_key, result)
            logger.warning(f"❌ {symbol} Orderbook EMPTY (profile={self.profile_name}) - skipping Maker order")
            return result
            
        # Check 2: Missing relevant side entirely
        if relevant_levels == 0:
            result = OrderbookValidationResult(
                is_valid=False,
                quality=OrderbookQuality.EMPTY,
                reason=f"No {relevant_side_name} in orderbook - cannot fill {side} Maker order",
                bid_depth_usd=bid_depth_usd,
                ask_depth_usd=ask_depth_usd,
                spread_percent=spread_percent,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_levels=len(bid_levels),
                ask_levels=len(ask_levels),
                staleness_seconds=staleness,
                recommended_action="skip",
                profile_used=self.profile_name
            )
            self._cache_result(cache_key, result)
            logger.warning(f"❌ {symbol} No {relevant_side_name} (profile={self.profile_name}) - cannot place {side} Maker order")
            return result
            
        # Check 3: Staleness
        if staleness > self.max_staleness_seconds:
            reasons.append(f"Orderbook stale ({staleness:.1f}s > {self.max_staleness_seconds}s)")
            is_valid = False
            quality = OrderbookQuality.INSUFFICIENT
            recommended_action = "wait"
        elif staleness > self.warn_staleness_seconds:
            reasons.append(f"Orderbook aging ({staleness:.1f}s)")
            if quality == OrderbookQuality.EXCELLENT:
                quality = OrderbookQuality.GOOD
                
        # Check 4: Spread (only if we have both sides)
        if spread_percent is not None:
            if spread_percent > self.max_spread_percent:
                reasons.append(f"Spread too wide ({spread_percent:.2f}% > {self.max_spread_percent}%)")
                is_valid = False
                quality = OrderbookQuality.INSUFFICIENT
                recommended_action = "wait"
            elif spread_percent > self.warn_spread_percent:
                reasons.append(f"Spread elevated ({spread_percent:.2f}%)")
                if quality in [OrderbookQuality.EXCELLENT, OrderbookQuality.GOOD]:
                    quality = OrderbookQuality.MARGINAL
                    
        # Check 5: Minimum levels on relevant side
        if relevant_levels < min_relevant_levels:
            reasons.append(f"Insufficient {relevant_side_name} levels ({relevant_levels} < {min_relevant_levels})")
            is_valid = False
            quality = OrderbookQuality.INSUFFICIENT
            recommended_action = "wait"
            
        # Check 6: Minimum depth on relevant side
        if relevant_depth < self.min_depth_usd:
            reasons.append(f"Insufficient {relevant_side_name} depth (${relevant_depth:.2f} < ${self.min_depth_usd})")
            is_valid = False
            quality = OrderbookQuality.INSUFFICIENT
            recommended_action = "wait"
            
        # Check 7: Minimum opposite depth (for spread calculation reliability)
        # Only check if threshold is > 0
        if self.min_opposite_depth_usd > 0 and opposite_depth < self.min_opposite_depth_usd:
            reasons.append(f"Low opposite side depth (${opposite_depth:.2f})")
            if quality in [OrderbookQuality.EXCELLENT, OrderbookQuality.GOOD]:
                quality = OrderbookQuality.MARGINAL
                
        # Check 8: Depth relative to trade size
        depth_multiple = relevant_depth / trade_size_usd if trade_size_usd > 0 else Decimal("0")
        
        if depth_multiple < self.marginal_depth_multiple:
            reasons.append(f"Depth too thin for trade size ({depth_multiple:.1f}x < {self.marginal_depth_multiple}x)")
            is_valid = False
            quality = OrderbookQuality.INSUFFICIENT
            recommended_action = "use_market_order"  # Suggest taker instead
        elif depth_multiple < self.good_depth_multiple:
            if quality in [OrderbookQuality.EXCELLENT, OrderbookQuality.GOOD]:
                quality = OrderbookQuality.MARGINAL
        elif depth_multiple < self.excellent_depth_multiple:
            if quality == OrderbookQuality.EXCELLENT:
                quality = OrderbookQuality.GOOD
                
        # Build final reason string
        if not reasons:
            spread_str = f", {spread_percent:.2f}% spread" if spread_percent is not None else ""
            reason = f"Orderbook healthy: {relevant_levels} {relevant_side_name}, ${relevant_depth:.0f} depth{spread_str}"
        else:
            reason = "; ".join(reasons)
            
        # Override action based on validity
        if is_valid:
            recommended_action = "proceed"
            
        result = OrderbookValidationResult(
            is_valid=is_valid,
            quality=quality,
            reason=reason,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            spread_percent=spread_percent,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_levels=len(bid_levels),
            ask_levels=len(ask_levels),
            staleness_seconds=staleness,
            recommended_action=recommended_action,
            profile_used=self.profile_name
        )
        
        self._cache_result(cache_key, result)
        
        # Log appropriately
        if not is_valid:
            logger.warning(f"❌ {symbol} Orderbook INVALID for {side} Maker (profile={self.profile_name}): {reason}")
        elif quality == OrderbookQuality.MARGINAL:
            logger.info(f"⚠️ {symbol} Orderbook MARGINAL for {side} Maker (profile={self.profile_name}): {reason}")
        else:
            logger.debug(f"✅ {symbol} Orderbook {quality.value} for {side} Maker (profile={self.profile_name}): {reason}")
            
        return result
        
    def _cache_result(self, key: str, result: OrderbookValidationResult):
        """Cache validation result"""
        self._validation_cache[key] = (time.time(), result)
        
    def clear_cache(self):
        """Clear validation cache"""
        self._validation_cache.clear()
        
    def simulate_price_impact(
        self,
        side: str,
        order_size_usd: Decimal,
        bids: List[Tuple[Decimal, Decimal]],  # [(price, size), ...]
        asks: List[Tuple[Decimal, Decimal]],  # [(price, size), ...]
        mid_price: Optional[Decimal] = None,
    ) -> PriceImpactResult:
        """
        Simulate price impact by walking through orderbook levels.
        
        For a BUY order: walks through ASK levels (sellers)
        For a SELL order: walks through BID levels (buyers)
        
        Args:
            side: "BUY" or "SELL"
            order_size_usd: Order size in USD to simulate
            bids: Bid levels [(price, size_in_coins), ...]
            asks: Ask levels [(price, size_in_coins), ...]
            mid_price: Optional mid-price for impact calculation
            
        Returns:
            PriceImpactResult with detailed execution simulation
        """
        # Determine which side of the book we're consuming
        # BUY = consume asks (we're buying from sellers)
        # SELL = consume bids (we're selling to buyers)
        if side.upper() == "BUY":
            levels = [OrderbookDepthLevel(Decimal(str(p)), Decimal(str(s))) for p, s in asks]
            is_buying = True
        else:
            levels = [OrderbookDepthLevel(Decimal(str(p)), Decimal(str(s))) for p, s in bids]
            is_buying = False
            
        # Handle empty book
        if not levels:
            return PriceImpactResult(
                side=side,
                order_size_usd=order_size_usd,
                can_fill=False,
                filled_size_usd=Decimal("0"),
                avg_execution_price=Decimal("0"),
                best_price=Decimal("0"),
                worst_price=Decimal("0"),
                slippage_percent=Decimal("0"),
                price_impact_percent=Decimal("0"),
                levels_consumed=0,
                fills_by_level=[],
            )
        
        # Calculate mid-price if not provided
        if mid_price is None:
            best_bid = Decimal(str(bids[0][0])) if bids else None
            best_ask = Decimal(str(asks[0][0])) if asks else None
            if best_bid and best_ask:
                mid_price = (best_bid + best_ask) / 2
            elif best_bid:
                mid_price = best_bid
            elif best_ask:
                mid_price = best_ask
            else:
                mid_price = levels[0].price
        
        # Walk through levels
        remaining_usd = order_size_usd
        total_coins = Decimal("0")
        total_usd = Decimal("0")
        fills_by_level: List[Tuple[Decimal, Decimal, Decimal]] = []
        levels_consumed = 0
        best_price = levels[0].price
        worst_price = levels[0].price
        
        for level in levels:
            if remaining_usd <= 0:
                break
                
            # Calculate how much we can fill at this level
            level_notional = level.notional
            fill_usd = min(remaining_usd, level_notional)
            fill_coins = fill_usd / level.price
            
            # Record fill
            fills_by_level.append((level.price, fill_coins, fill_usd))
            total_coins += fill_coins
            total_usd += fill_usd
            remaining_usd -= fill_usd
            levels_consumed += 1
            worst_price = level.price
            
        # Calculate metrics
        can_fill = remaining_usd <= Decimal("0.01")  # Allow tiny remainder
        filled_size_usd = total_usd
        
        if total_coins > 0:
            avg_execution_price = total_usd / total_coins
        else:
            avg_execution_price = Decimal("0")
        
        # Calculate slippage from best price to avg execution price
        if best_price > 0:
            if is_buying:
                # For buys, slippage is how much MORE we pay vs best ask
                slippage_percent = ((avg_execution_price - best_price) / best_price) * 100
            else:
                # For sells, slippage is how much LESS we receive vs best bid
                slippage_percent = ((best_price - avg_execution_price) / best_price) * 100
        else:
            slippage_percent = Decimal("0")
            
        # Calculate price impact from mid-price
        if mid_price > 0:
            if is_buying:
                price_impact_percent = ((avg_execution_price - mid_price) / mid_price) * 100
            else:
                price_impact_percent = ((mid_price - avg_execution_price) / mid_price) * 100
        else:
            price_impact_percent = Decimal("0")
        
        return PriceImpactResult(
            side=side,
            order_size_usd=order_size_usd,
            can_fill=can_fill,
            filled_size_usd=filled_size_usd,
            avg_execution_price=avg_execution_price,
            best_price=best_price,
            worst_price=worst_price,
            slippage_percent=max(slippage_percent, Decimal("0")),  # Always positive
            price_impact_percent=max(price_impact_percent, Decimal("0")),
            levels_consumed=levels_consumed,
            fills_by_level=fills_by_level,
        )
        
    def get_recommended_price(
        self,
        symbol: str,
        side: str,
        bids: List[Tuple[Decimal, Decimal]],
        asks: List[Tuple[Decimal, Decimal]],
        offset_bps: int = 1,  # Basis points offset from best price
    ) -> Optional[Decimal]:
        """
        Get recommended Maker price based on orderbook state.
        
        For SELL: Place slightly below best ask (to be competitive)
        For BUY: Place slightly above best bid (to be competitive)
        
        Args:
            symbol: Trading pair
            side: "BUY" or "SELL"
            bids: Bid levels
            asks: Ask levels
            offset_bps: Basis points to offset from best price (default 1bp = 0.01%)
            
        Returns:
            Recommended limit price or None if orderbook invalid
        """
        offset_multiplier = Decimal(str(1 + offset_bps / 10000))
        offset_divisor = Decimal(str(1 - offset_bps / 10000))
        
        if side.upper() == "SELL":
            # For SELL Maker, we need to be on ask side
            # Place slightly below best ask to be first in queue
            if not asks:
                if bids:
                    # No asks, use best bid + spread estimate
                    best_bid = Decimal(str(bids[0][0]))
                    return best_bid * Decimal("1.001")  # 0.1% above best bid
                return None
            best_ask = Decimal(str(asks[0][0]))
            return best_ask * offset_divisor  # Slightly below best ask
            
        else:  # BUY
            # For BUY Maker, we need to be on bid side
            # Place slightly above best bid to be first in queue
            if not bids:
                if asks:
                    # No bids, use best ask - spread estimate
                    best_ask = Decimal(str(asks[0][0]))
                    return best_ask * Decimal("0.999")  # 0.1% below best ask
                return None
            best_bid = Decimal(str(bids[0][0]))
            return best_bid * offset_multiplier  # Slightly above best bid


# Singleton instances for each exchange profile
_validators: Dict[ExchangeProfile, OrderbookValidator] = {}


def get_orderbook_validator(profile: ExchangeProfile = ExchangeProfile.LIGHTER) -> OrderbookValidator:
    """
    Get singleton orderbook validator instance for specified profile.
    
    Args:
        profile: Exchange profile (LIGHTER, X10, or DEFAULT)
        
    Returns:
        OrderbookValidator configured for the specified exchange
    """
    global _validators
    
    if profile not in _validators:
        # Load config overrides if available
        try:
            import config
            
            if profile == ExchangeProfile.LIGHTER:
                _validators[profile] = OrderbookValidator(
                    profile=ExchangeProfile.LIGHTER,
                    min_depth_usd=getattr(config, 'OB_LIGHTER_MIN_DEPTH_USD', None),
                    min_bid_levels=getattr(config, 'OB_LIGHTER_MIN_BID_LEVELS', None),
                    min_ask_levels=getattr(config, 'OB_LIGHTER_MIN_ASK_LEVELS', None),
                    max_spread_percent=getattr(config, 'OB_LIGHTER_MAX_SPREAD_PCT', None),
                    max_staleness_seconds=getattr(config, 'OB_LIGHTER_MAX_STALENESS', None),
                )
            elif profile == ExchangeProfile.X10:
                _validators[profile] = OrderbookValidator(
                    profile=ExchangeProfile.X10,
                    min_depth_usd=getattr(config, 'OB_X10_MIN_DEPTH_USD', None),
                    min_bid_levels=getattr(config, 'OB_X10_MIN_BID_LEVELS', None),
                    min_ask_levels=getattr(config, 'OB_X10_MIN_ASK_LEVELS', None),
                    max_spread_percent=getattr(config, 'OB_X10_MAX_SPREAD_PCT', None),
                    max_staleness_seconds=getattr(config, 'OB_X10_MAX_STALENESS', None),
                )
            else:
                _validators[profile] = OrderbookValidator(
                    profile=ExchangeProfile.DEFAULT,
                    min_depth_usd=getattr(config, 'OB_MIN_DEPTH_USD', None),
                    min_opposite_depth_usd=getattr(config, 'OB_MIN_OPPOSITE_DEPTH_USD', None),
                    min_bid_levels=getattr(config, 'OB_MIN_BID_LEVELS', None),
                    min_ask_levels=getattr(config, 'OB_MIN_ASK_LEVELS', None),
                    max_spread_percent=getattr(config, 'OB_MAX_SPREAD_PERCENT', None),
                    warn_spread_percent=getattr(config, 'OB_WARN_SPREAD_PERCENT', None),
                    max_staleness_seconds=getattr(config, 'OB_MAX_STALENESS_SECONDS', None),
                    warn_staleness_seconds=getattr(config, 'OB_WARN_STALENESS_SECONDS', None),
                )
        except ImportError:
            _validators[profile] = OrderbookValidator(profile=profile)
            
    return _validators[profile]


def validate_orderbook_for_maker(
    symbol: str,
    side: str,
    trade_size_usd: float,
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    orderbook_timestamp: Optional[float] = None,
    profile: ExchangeProfile = ExchangeProfile.LIGHTER,
) -> OrderbookValidationResult:
    """
    Convenience function to validate orderbook for Maker order.
    
    Args:
        symbol: Trading pair (e.g., "DOGE-USD")
        side: "BUY" or "SELL"
        trade_size_usd: Trade size in USD
        bids: List of (price, size) tuples
        asks: List of (price, size) tuples
        orderbook_timestamp: Unix timestamp of orderbook data
        profile: Exchange profile to use (default: LIGHTER)
        
    Returns:
        OrderbookValidationResult with validation status
    
    Example usage:
        result = validate_orderbook_for_maker(
            symbol="DOGE-USD",
            side="SELL",
            trade_size_usd=50.0,
            bids=[(0.138, 1000), (0.1379, 2000)],
            asks=[(0.1381, 500)],
            profile=ExchangeProfile.LIGHTER,
        )
        
        if not result.is_valid:
            if result.recommended_action == "use_market_order":
                # Fall back to taker order
                pass
            elif result.recommended_action == "wait":
                # Wait and retry
                pass
            else:
                # Skip this trade
                pass
    """
    validator = get_orderbook_validator(profile)
    return validator.validate_for_maker_order(
        symbol=symbol,
        side=side,
        trade_size_usd=Decimal(str(trade_size_usd)),
        bids=bids,
        asks=asks,
        orderbook_timestamp=orderbook_timestamp,
    )


def simulate_price_impact(
    side: str,
    order_size_usd: float,
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    mid_price: Optional[float] = None,
    profile: ExchangeProfile = ExchangeProfile.LIGHTER,
) -> PriceImpactResult:
    """
    Convenience function to simulate price impact for an order.
    
    Args:
        side: "BUY" or "SELL"
        order_size_usd: Order size in USD
        bids: Bid levels [(price, size_coins), ...]
        asks: Ask levels [(price, size_coins), ...]
        mid_price: Optional mid-price for impact calculation
        profile: Exchange profile (default: LIGHTER)
        
    Returns:
        PriceImpactResult with execution simulation
        
    Example:
        impact = simulate_price_impact(
            side="BUY",
            order_size_usd=150.0,
            bids=[(100.0, 2.0), (99.5, 5.0)],
            asks=[(100.5, 1.0), (101.0, 3.0), (101.5, 5.0)],
        )
        
        if impact.can_fill and impact.slippage_percent < 0.5:
            # Good execution expected
            print(f"Avg price: ${impact.avg_execution_price:.4f}")
            print(f"Slippage: {impact.slippage_percent:.3f}%")
    """
    validator = get_orderbook_validator(profile)
    return validator.simulate_price_impact(
        side=side,
        order_size_usd=Decimal(str(order_size_usd)),
        bids=[(Decimal(str(p)), Decimal(str(s))) for p, s in bids],
        asks=[(Decimal(str(p)), Decimal(str(s))) for p, s in asks],
        mid_price=Decimal(str(mid_price)) if mid_price else None,
    )
