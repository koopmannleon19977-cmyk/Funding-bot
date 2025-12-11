"""Unit tests for OrderbookValidator"""

import pytest
from decimal import Decimal
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.validation.orderbook_validator import (
    OrderbookValidator,
    OrderbookValidationResult,
    OrderbookQuality,
    OrderbookDepthLevel,
    validate_orderbook_for_maker,
)


class TestOrderbookValidator:
    
    @pytest.fixture
    def validator(self):
        """Create validator with test-friendly thresholds"""
        return OrderbookValidator(
            min_depth_usd=100.0,
            min_opposite_depth_usd=50.0,
            min_bid_levels=2,
            min_ask_levels=2,
            max_spread_percent=1.0,
            warn_spread_percent=0.5,
            max_staleness_seconds=10.0,
            warn_staleness_seconds=5.0,
            excellent_depth_multiple=10.0,
            good_depth_multiple=5.0,
            marginal_depth_multiple=2.0,
        )
    
    def test_empty_orderbook_rejected(self, validator):
        """Test that completely empty orderbook is rejected"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[],
            asks=[],
        )
        assert not result.is_valid
        assert result.quality == OrderbookQuality.EMPTY
        assert result.recommended_action == "skip"
        assert "empty" in result.reason.lower()
        
    def test_missing_bids_for_sell_rejected(self, validator):
        """Test that SELL Maker is rejected when no bids exist"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[],  # No bids = no one to fill our SELL
            asks=[(Decimal("100"), Decimal("10"))],
        )
        assert not result.is_valid
        assert "No bids" in result.reason
        assert result.recommended_action == "skip"
        
    def test_missing_asks_for_buy_rejected(self, validator):
        """Test that BUY Maker is rejected when no asks exist"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="BUY",
            trade_size_usd=Decimal("50"),
            bids=[(Decimal("100"), Decimal("10"))],
            asks=[],  # No asks = no one to fill our BUY
        )
        assert not result.is_valid
        assert "No asks" in result.reason
        assert result.recommended_action == "skip"
        
    def test_healthy_orderbook_accepted(self, validator):
        """Test that healthy orderbook passes validation"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("5")),   # $500
                (Decimal("99.99"), Decimal("10")),   # $999.90
                (Decimal("99.98"), Decimal("20")),   # $1999.60
            ],
            asks=[
                (Decimal("100.05"), Decimal("5")),
                (Decimal("100.10"), Decimal("10")),
                (Decimal("100.15"), Decimal("20")),
            ],
        )
        assert result.is_valid
        assert result.quality in [OrderbookQuality.EXCELLENT, OrderbookQuality.GOOD]
        assert result.recommended_action == "proceed"
        
    def test_wide_spread_rejected(self, validator):
        """Test that wide spread is rejected"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("10")),
                (Decimal("99.00"), Decimal("10")),
            ],
            asks=[
                (Decimal("102.00"), Decimal("10")),  # 2% spread
                (Decimal("103.00"), Decimal("10")),
            ],
        )
        assert not result.is_valid
        assert "Spread" in result.reason
        assert result.recommended_action == "wait"
        
    def test_insufficient_depth_suggests_market_order(self, validator):
        """Test that thin depth suggests market order"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("500"),  # Large trade
            bids=[
                (Decimal("100.00"), Decimal("1")),  # Only $100 depth
                (Decimal("99.99"), Decimal("1")),   # Only $100 more
            ],
            asks=[
                (Decimal("100.01"), Decimal("10")),
                (Decimal("100.02"), Decimal("10")),
            ],
        )
        # Depth ($200) is less than 2x trade size ($1000)
        assert not result.is_valid
        assert result.recommended_action == "use_market_order"
        
    def test_insufficient_levels_rejected(self, validator):
        """Test that insufficient price levels are rejected"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("100")),  # Only 1 level, need 2
            ],
            asks=[
                (Decimal("100.01"), Decimal("100")),
                (Decimal("100.02"), Decimal("100")),
            ],
        )
        assert not result.is_valid
        assert "levels" in result.reason.lower()
        
    def test_stale_orderbook_rejected(self, validator):
        """Test that stale orderbook data is rejected"""
        import time
        old_timestamp = time.time() - 15  # 15 seconds old (> 10s max)
        
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("10")),
                (Decimal("99.99"), Decimal("10")),
            ],
            asks=[
                (Decimal("100.01"), Decimal("10")),
                (Decimal("100.02"), Decimal("10")),
            ],
            orderbook_timestamp=old_timestamp,
        )
        assert not result.is_valid
        assert "stale" in result.reason.lower()
        assert result.recommended_action == "wait"
        
    def test_marginal_quality_for_elevated_spread(self, validator):
        """Test that elevated spread results in marginal quality"""
        result = validator.validate_for_maker_order(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("20")),  # $2000
                (Decimal("99.90"), Decimal("20")),   # $1998
            ],
            asks=[
                (Decimal("100.60"), Decimal("20")),  # 0.6% spread (above 0.5% warn)
                (Decimal("100.70"), Decimal("20")),
            ],
        )
        assert result.is_valid  # Still valid
        assert result.quality == OrderbookQuality.MARGINAL
        
    def test_depth_level_notional_calculation(self):
        """Test OrderbookDepthLevel notional property"""
        level = OrderbookDepthLevel(
            price=Decimal("100.00"),
            size=Decimal("5.5")
        )
        assert level.notional == Decimal("550.00")
        
    def test_get_recommended_price_sell(self, validator):
        """Test recommended price for SELL orders"""
        bids = [
            (Decimal("99.90"), Decimal("10")),
            (Decimal("99.80"), Decimal("10")),
        ]
        asks = [
            (Decimal("100.10"), Decimal("10")),
            (Decimal("100.20"), Decimal("10")),
        ]
        
        price = validator.get_recommended_price(
            symbol="TEST-USD",
            side="SELL",
            bids=bids,
            asks=asks,
            offset_bps=1,
        )
        
        # Should be slightly below best ask (100.10)
        assert price is not None
        assert price < Decimal("100.10")
        assert price > Decimal("100.00")  # But not too far below
        
    def test_get_recommended_price_buy(self, validator):
        """Test recommended price for BUY orders"""
        bids = [
            (Decimal("99.90"), Decimal("10")),
            (Decimal("99.80"), Decimal("10")),
        ]
        asks = [
            (Decimal("100.10"), Decimal("10")),
            (Decimal("100.20"), Decimal("10")),
        ]
        
        price = validator.get_recommended_price(
            symbol="TEST-USD",
            side="BUY",
            bids=bids,
            asks=asks,
            offset_bps=1,
        )
        
        # Should be slightly above best bid (99.90)
        assert price is not None
        assert price > Decimal("99.90")
        assert price < Decimal("100.00")  # But not too far above
        
    def test_get_recommended_price_no_asks(self, validator):
        """Test recommended price when no asks exist"""
        bids = [
            (Decimal("99.90"), Decimal("10")),
        ]
        asks = []
        
        price = validator.get_recommended_price(
            symbol="TEST-USD",
            side="SELL",
            bids=bids,
            asks=asks,
        )
        
        # Should derive from best bid
        assert price is not None
        assert price > Decimal("99.90")  # Above best bid
        
    def test_get_recommended_price_no_bids(self, validator):
        """Test recommended price when no bids exist"""
        bids = []
        asks = [
            (Decimal("100.10"), Decimal("10")),
        ]
        
        price = validator.get_recommended_price(
            symbol="TEST-USD",
            side="BUY",
            bids=bids,
            asks=asks,
        )
        
        # Should derive from best ask
        assert price is not None
        assert price < Decimal("100.10")  # Below best ask
        
    def test_convenience_function(self):
        """Test validate_orderbook_for_maker convenience function"""
        result = validate_orderbook_for_maker(
            symbol="TEST-USD",
            side="SELL",
            trade_size_usd=50.0,
            bids=[(100.0, 10.0), (99.9, 10.0), (99.8, 10.0)],
            asks=[(100.1, 10.0), (100.2, 10.0), (100.3, 10.0)],
        )
        
        assert isinstance(result, OrderbookValidationResult)
        assert result.bid_levels == 3
        assert result.ask_levels == 3
        
    def test_cache_prevents_repeated_warnings(self, validator):
        """Test that cache prevents log spam"""
        # First call
        result1 = validator.validate_for_maker_order(
            symbol="CACHE-TEST",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[(Decimal("100"), Decimal("10")), (Decimal("99"), Decimal("10"))],
            asks=[(Decimal("101"), Decimal("10")), (Decimal("102"), Decimal("10"))],
        )
        
        # Immediate second call should return cached result
        result2 = validator.validate_for_maker_order(
            symbol="CACHE-TEST",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[(Decimal("100"), Decimal("10")), (Decimal("99"), Decimal("10"))],
            asks=[(Decimal("101"), Decimal("10")), (Decimal("102"), Decimal("10"))],
        )
        
        # Results should be identical (cached)
        assert result1.is_valid == result2.is_valid
        assert result1.quality == result2.quality
        
    def test_clear_cache(self, validator):
        """Test cache clearing"""
        # Populate cache
        validator.validate_for_maker_order(
            symbol="CLEAR-TEST",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[(Decimal("100"), Decimal("10")), (Decimal("99"), Decimal("10"))],
            asks=[(Decimal("101"), Decimal("10")), (Decimal("102"), Decimal("10"))],
        )
        
        assert len(validator._validation_cache) > 0
        
        # Clear cache
        validator.clear_cache()
        
        assert len(validator._validation_cache) == 0
        
    def test_spread_calculation(self, validator):
        """Test spread percentage calculation"""
        result = validator.validate_for_maker_order(
            symbol="SPREAD-TEST",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("10")),
                (Decimal("99.90"), Decimal("10")),
            ],
            asks=[
                (Decimal("100.50"), Decimal("10")),  # 0.5% spread from best bid
                (Decimal("100.60"), Decimal("10")),
            ],
        )
        
        # Spread = (100.50 - 100.00) / 100.00 * 100 = 0.5%
        assert result.spread_percent is not None
        assert abs(result.spread_percent - Decimal("0.5")) < Decimal("0.01")
        
    def test_depth_calculation(self, validator):
        """Test depth USD calculation"""
        result = validator.validate_for_maker_order(
            symbol="DEPTH-TEST",
            side="SELL",
            trade_size_usd=Decimal("50"),
            bids=[
                (Decimal("100.00"), Decimal("5")),   # $500
                (Decimal("99.00"), Decimal("10")),   # $990
            ],
            asks=[
                (Decimal("101.00"), Decimal("3")),   # $303
                (Decimal("102.00"), Decimal("2")),   # $204
            ],
        )
        
        # Bid depth = 500 + 990 = 1490
        assert result.bid_depth_usd == Decimal("1490")
        
        # Ask depth = 303 + 204 = 507
        assert result.ask_depth_usd == Decimal("507")


class TestOrderbookValidatorEdgeCases:
    """Test edge cases and boundary conditions"""
    
    @pytest.fixture
    def strict_validator(self):
        """Validator with strict requirements"""
        return OrderbookValidator(
            min_depth_usd=1000.0,
            min_bid_levels=5,
            min_ask_levels=5,
            max_spread_percent=0.1,
        )
    
    def test_exactly_at_minimum_depth(self, strict_validator):
        """Test behavior at exactly minimum depth threshold"""
        # Create exactly $1000 depth
        result = strict_validator.validate_for_maker_order(
            symbol="EDGE-TEST",
            side="SELL",
            trade_size_usd=Decimal("100"),
            bids=[
                (Decimal("100"), Decimal("2")),  # $200
                (Decimal("100"), Decimal("2")),  # $200
                (Decimal("100"), Decimal("2")),  # $200
                (Decimal("100"), Decimal("2")),  # $200
                (Decimal("100"), Decimal("2")),  # $200 = $1000 total
            ],
            asks=[
                (Decimal("100.05"), Decimal("2")),
                (Decimal("100.06"), Decimal("2")),
                (Decimal("100.07"), Decimal("2")),
                (Decimal("100.08"), Decimal("2")),
                (Decimal("100.09"), Decimal("2")),
            ],
        )
        
        # Should pass at exactly minimum
        assert result.is_valid
        
    def test_one_below_minimum_levels(self, strict_validator):
        """Test failure when exactly one level below minimum"""
        result = strict_validator.validate_for_maker_order(
            symbol="EDGE-TEST",
            side="SELL",
            trade_size_usd=Decimal("100"),
            bids=[
                (Decimal("100"), Decimal("100")),  # Only 4 levels
                (Decimal("99"), Decimal("100")),
                (Decimal("98"), Decimal("100")),
                (Decimal("97"), Decimal("100")),
            ],
            asks=[
                (Decimal("100.01"), Decimal("100")),
                (Decimal("100.02"), Decimal("100")),
                (Decimal("100.03"), Decimal("100")),
                (Decimal("100.04"), Decimal("100")),
                (Decimal("100.05"), Decimal("100")),
            ],
        )
        
        assert not result.is_valid
        assert "levels" in result.reason.lower()
        
    def test_zero_trade_size(self):
        """Test handling of zero trade size"""
        validator = OrderbookValidator()
        
        result = validator.validate_for_maker_order(
            symbol="ZERO-TEST",
            side="SELL",
            trade_size_usd=Decimal("0"),
            bids=[(Decimal("100"), Decimal("10")), (Decimal("99"), Decimal("10")), (Decimal("98"), Decimal("10"))],
            asks=[(Decimal("101"), Decimal("10")), (Decimal("102"), Decimal("10")), (Decimal("103"), Decimal("10"))],
        )
        
        # Should handle gracefully (depth_multiple would be 0 or inf)
        assert isinstance(result, OrderbookValidationResult)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

