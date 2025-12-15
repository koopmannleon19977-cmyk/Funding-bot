import asyncio
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

# Direct test without instantiating adapters
# Simulating the exact logic from lighter_adapter.py

def test_filter_logic():
    """Test the timestamp filter logic with real data from logs"""
    
    # Trades from state_snapshot.json
    trades = {
        '1000SHIB-USD': {'created_at': 1765788966065},  # ms
        'WLFI-USD': {'created_at': 1765789004337},       # ms  
        'ONDO-USD': {'created_at': 1765789105291}        # ms
    }
    
    # Sample payments from 10:05 log (after 10:00 funding payment)
    # These are from the fetch_position_funding output
    all_payments = {
        '1000SHIB-USD': [
            {'timestamp': 1765789200, 'funding_received': -0.004863},  # 10:00 UTC
            {'timestamp': 1764856800, 'funding_received': -0.000261},
            {'timestamp': 1764190800, 'funding_received': -0.000302},
            {'timestamp': 1763730000, 'funding_received': -0.000169},
        ],
        'WLFI-USD': [
            {'timestamp': 1765789200, 'funding_received': -0.005393},  # 10:00 UTC
            {'timestamp': 1765785600, 'funding_received': -0.008644},  # 09:00 UTC
            {'timestamp': 1765728000, 'funding_received': -0.007429},
        ],
        'ONDO-USD': [
            {'timestamp': 1765789200, 'funding_received': -0.002874},  # 10:00 UTC
            {'timestamp': 1765728000, 'funding_received': -0.001036},
            {'timestamp': 1765724400, 'funding_received': -0.000561},
        ]
    }
    
    print("=" * 70)
    print("TESTING EXACT FILTER LOGIC FROM lighter_adapter.py")
    print("=" * 70)
    
    for symbol, trade in trades.items():
        print(f"\n### {symbol} ###")
        
        # This is from_time as calculated in funding_tracker.py
        created_at = trade['created_at']
        from_time = created_at  # After fix: already in ms, not multiplied again
        
        print(f"  Trade created_at: {created_at} ms")
        print(f"  from_time passed to get_funding_for_symbol: {from_time}")
        
        # This is the logic in get_funding_for_symbol
        since_timestamp = from_time  # What's passed in
        
        # Filter logic from lighter_adapter.py line 2331-2333
        if since_timestamp:
            since_ts = since_timestamp // 1000 if since_timestamp > 1e12 else since_timestamp
            print(f"  since_ts (for filtering): {since_ts}")
        
        payments = all_payments.get(symbol, [])
        print(f"\n  Pre-filter payments ({len(payments)}):")
        for p in payments:
            ts = p['timestamp']
            passed = ts >= since_ts
            print(f"    ts={ts}: {ts} >= {since_ts} = {passed}")
        
        # Apply filter
        filtered = [f for f in payments if (f.get('timestamp') or 0) >= since_ts]
        
        print(f"\n  Post-filter payments ({len(filtered)}):")
        for p in filtered:
            print(f"    ts={p['timestamp']} received={p['funding_received']}")
        
        # Calculate total
        total = sum(f.get('funding_received', 0.0) for f in filtered)
        print(f"\n  TOTAL FUNDING: ${total:.6f}")
        
        # Check the condition for logging
        if abs(total) > 0.00001:
            print(f"  ✅ Would log: '💵 Lighter {symbol}: Total funding=${total:.6f}'")
        else:
            print(f"  ❌ Would NOT log (abs({total}) <= 0.00001)")

if __name__ == '__main__':
    test_filter_logic()
