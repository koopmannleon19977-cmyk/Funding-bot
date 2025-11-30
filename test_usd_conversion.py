"""
Test script for USD to baseAmount conversion
Demonstrates the usd_to_base_amount function
"""
import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adapters.lighter_adapter import LighterAdapter


async def test_usd_conversion():
    """Test USD to baseAmount conversion for various markets"""
    
    print("=" * 80)
    print("Testing USD → baseAmount Conversion".center(80))
    print("=" * 80)
    
    adapter = LighterAdapter()
    
    # Load market data
    print("\n📊 Loading market data...")
    await adapter.load_market_cache(force=True)
    
    if not adapter.market_info:
        print("❌ No markets loaded. Check your configuration.")
        return
    
    print(f"✅ Loaded {len(adapter.market_info)} markets\n")
    
    # Test cases
    test_cases = [
        ("APT-USD", 14.36, 10.20),    # Original example
        ("APT-USD", 50.00, 10.20),    # Larger order
        ("APT-USD", 5.00, 10.20),     # Small order
        ("ETH-USD", 100.00, 3500.00), # ETH example
        ("BTC-USD", 1000.00, 45000.00), # BTC example (if available)
    ]
    
    for symbol, usd_amount, price in test_cases:
        print("\n" + "─" * 80)
        
        # Check if market exists
        if symbol not in adapter.market_info:
            print(f"⚠️  Skipping {symbol} (not available)")
            continue
        
        try:
            # Call the conversion function
            base_amount = adapter.usd_to_base_amount(symbol, usd_amount, price)
            
            # Get market info for verification
            market_data = adapter.market_info[symbol]
            size_decimals = market_data.get('sd', 8)
            base_scale = 10 ** size_decimals
            
            # Calculate actual values
            actual_quantity = base_amount / base_scale
            actual_usd = actual_quantity * price
            
            # Display results
            print(f"\n✅ {symbol} Conversion Successful:")
            print(f"   Target: ${usd_amount:.2f} @ ${price:.2f}")
            print(f"   Result: {base_amount} units")
            print(f"   → {actual_quantity:.8f} tokens")
            print(f"   → ${actual_usd:.2f} USD value")
            print(f"   Precision: {size_decimals} decimals (1 token = {base_scale:,} units)")
            
        except ValueError as e:
            print(f"\n❌ {symbol} Conversion Failed:")
            print(f"   {str(e)}")
        except Exception as e:
            print(f"\n❌ {symbol} Unexpected Error:")
            print(f"   {type(e).__name__}: {str(e)}")
    
    print("\n" + "=" * 80)
    print("Test Complete".center(80))
    print("=" * 80)


async def test_order_flow():
    """Test the complete order flow with USD conversion"""
    
    print("\n\n" + "=" * 80)
    print("Testing Complete Order Flow".center(80))
    print("=" * 80)
    
    adapter = LighterAdapter()
    
    # Load markets
    await adapter.load_market_cache(force=True)
    
    # Test with APT-USD
    symbol = "APT-USD"
    usd_size = 14.36
    
    if symbol not in adapter.market_info:
        print(f"⚠️  {symbol} not available")
        return
    
    print(f"\n📝 Order Scenario: Buy ${usd_size:.2f} of {symbol}")
    
    # Step 1: Get current price
    current_price = adapter.get_price(symbol)
    if not current_price:
        print(f"❌ No price data for {symbol}")
        return
    
    print(f"   Current Price: ${current_price:.6f}")
    
    # Step 2: Convert USD to baseAmount
    try:
        base_amount = adapter.usd_to_base_amount(symbol, usd_size, current_price)
        print(f"   Base Amount: {base_amount} units")
    except ValueError as e:
        print(f"❌ Conversion failed: {e}")
        return
    
    # Step 3: Calculate limit price (with 0.5% slippage)
    from decimal import Decimal
    slippage = Decimal("0.005")  # 0.5%
    limit_price = Decimal(str(current_price)) * (Decimal("1") + slippage)
    
    print(f"   Limit Price: ${float(limit_price):.6f} (0.5% slippage)")
    
    # Step 4: Scale price to API units
    market_data = adapter.market_info[symbol]
    price_decimals = market_data.get('pd', 6)
    quote_scale = 10 ** price_decimals
    price_scaled = int(float(limit_price) * quote_scale)
    
    print(f"   Scaled Price: {price_scaled} units ({price_decimals} decimals)")
    
    # Step 5: Validate
    print(f"\n✅ Order Ready for API:")
    print(f"   market_id: {market_data.get('i')}")
    print(f"   baseAmount: {base_amount}")
    print(f"   price: {price_scaled}")
    print(f"   side: BUY")
    
    print("\n💡 This order would execute as:")
    actual_qty = base_amount / (10 ** market_data.get('sd', 8))
    actual_value = actual_qty * float(limit_price)
    print(f"   Buy {actual_qty:.8f} {symbol.split('-')[0]} @ ${float(limit_price):.6f}")
    print(f"   Total Value: ${actual_value:.2f}")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    # Run tests
    asyncio.run(test_usd_conversion())
    asyncio.run(test_order_flow())
