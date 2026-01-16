"""
Market data snapshots and DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import FundingRate


@dataclass
class PriceSnapshot:
    """Price data with timestamp for staleness detection."""

    symbol: str
    lighter_price: Decimal = Decimal("0")
    x10_price: Decimal = Decimal("0")
    lighter_updated: datetime | None = None
    x10_updated: datetime | None = None

    @property
    def mid_price(self) -> Decimal:
        """Average of both exchange prices."""
        if self.lighter_price and self.x10_price:
            return (self.lighter_price + self.x10_price) / 2
        return self.lighter_price or self.x10_price

    @property
    def spread_pct(self) -> Decimal:
        """Price spread as percentage."""
        # Important: If one side is missing, we cannot infer cross-exchange spread.
        # Returning 0 would be dangerously misleading (looks like "perfect spread").
        if not self.lighter_price or not self.x10_price or self.mid_price == 0:
            return Decimal("1.0")
        return abs(self.lighter_price - self.x10_price) / self.mid_price

    def is_stale(self, max_age_seconds: float = 30.0) -> bool:
        """Check if price data is stale."""
        now = datetime.now(UTC)

        # We treat the snapshot as stale only when BOTH exchanges are stale/unknown.
        #
        # Rationale:
        # - Lighter does not provide cheap "all markets" price snapshots; we often refresh
        #   only a subset (trickle/on-demand) to respect weighted rate limits.
        # - Opportunity selection will be verified again at execution time using fresh orderbook data.
        #
        # This reduces "stale price data" false negatives during scanning while keeping safety for
        # cases where we have no fresh data on either side.
        def _side_stale(updated: datetime | None) -> bool:
            if not updated:
                return True
            return (now - updated).total_seconds() > max_age_seconds

        return _side_stale(self.lighter_updated) and _side_stale(self.x10_updated)


@dataclass
class FundingSnapshot:
    """Funding rate data from both exchanges."""

    symbol: str
    lighter_rate: FundingRate | None = None
    x10_rate: FundingRate | None = None

    @property
    def net_rate_hourly(self) -> Decimal:
        """Net funding rate (positive = we earn)."""
        lighter = (
            self.lighter_rate.rate_hourly if self.lighter_rate else Decimal("0")
        )
        x10 = self.x10_rate.rate_hourly if self.x10_rate else Decimal("0")
        # The maximum arbitrage revenue is the absolute difference between rates.
        # OpportunitiesService determines the specific direction to capture it.
        return abs(lighter - x10)

    @property
    def apy(self) -> Decimal:
        """Annualized funding rate."""
        return self.net_rate_hourly * Decimal("24") * Decimal("365")


@dataclass
class OrderbookSnapshot:
    """L1 orderbook data from both exchanges."""

    symbol: str
    lighter_bid: Decimal = Decimal("0")
    lighter_ask: Decimal = Decimal("0")
    x10_bid: Decimal = Decimal("0")
    x10_ask: Decimal = Decimal("0")
    lighter_bid_qty: Decimal = Decimal("0")
    lighter_ask_qty: Decimal = Decimal("0")
    x10_bid_qty: Decimal = Decimal("0")
    x10_ask_qty: Decimal = Decimal("0")
    # These timestamps indicate when we last obtained a "depth-valid" snapshot for that exchange
    # (i.e., both bid/ask AND bid_qty/ask_qty > 0). Background refreshes preserve these values.
    lighter_updated: datetime | None = None
    x10_updated: datetime | None = None

    def entry_spread_pct(self, long_exchange: str) -> Decimal:
        """
        Calculate entry spread based on trade direction.

        For Long X10, Short Lighter:
          - We BUY on X10 (pay x10_ask)
          - We SELL on Lighter as maker (get lighter_bid or slightly better)
          - Entry cost = x10_ask - lighter_bid

        For Long Lighter, Short X10:
          - We BUY on Lighter as maker (pay lighter_ask or slightly better)
          - We SELL on X10 (get x10_bid)
          - Entry cost = lighter_ask - x10_bid

        Returns the spread as a decimal (e.g., 0.005 = 0.5%)

        IMPORTANT: Returns a HIGH spread (1.0 = 100%) if orderbook data is missing.
        This ensures trades are REJECTED when we can't verify the spread.
        """
        # Calculate mid price for percentage calculation
        mid = (self.lighter_bid + self.lighter_ask + self.x10_bid + self.x10_ask) / 4

        # If we have no orderbook data at all, return VERY HIGH spread to filter out
        if mid == 0:
            return Decimal("1.0")  # 100% spread = always filtered

        if long_exchange == "X10":
            # Long X10 (taker buy at ask), Short Lighter (maker sell at bid)
            if self.x10_ask > 0 and self.lighter_bid > 0:
                spread = (self.x10_ask - self.lighter_bid) / mid
                return spread
            # Missing data - return high spread to reject
            return Decimal("1.0")
        # Long Lighter (maker buy at ask), Short X10 (taker sell at bid)
        if self.lighter_ask > 0 and self.x10_bid > 0:
            spread = (self.lighter_ask - self.x10_bid) / mid
            return spread
        # Missing data - return high spread to reject
        return Decimal("1.0")


@dataclass
class OrderbookDepthSnapshot:
    """
    Top-N orderbook levels from both exchanges.

    Levels are (price, qty) tuples. Bids are sorted descending; asks ascending.
    """

    symbol: str
    lighter_bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    lighter_asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    x10_bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    x10_asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    lighter_updated: datetime | None = None
    x10_updated: datetime | None = None
