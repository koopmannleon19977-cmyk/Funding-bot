#!/usr/bin/env python3
"""Fix script for Lighter ghost position issues"""

# Fix 1: lighter_adapter.py - Remove order_expiry for IOC orders (causes "OrderExpiry is invalid")
print("Fixing lighter_adapter.py...")

with open('src/adapters/lighter_adapter.py', 'r', encoding='utf-8') as f:
    content = f.read()

# IOC orders should not have order_expiry or use 0
old_ioc = '''                order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,'''
new_ioc = '''                order_expiry=0,  # IOC orders don't need expiry'''

# Only replace in the IOC function context (check if IMMEDIATE_OR_CANCEL is nearby)
if 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL' in content and old_ioc in content:
    # Find the IOC function and replace only there
    import re
    # Replace only the first occurrence after IMMEDIATE_OR_CANCEL
    pattern = r'(time_in_force=SignerClient\.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL.*?reduce_only=True.*?trigger_price=SignerClient\.NIL_TRIGGER_PRICE.*?)order_expiry=SignerClient\.DEFAULT_28_DAY_ORDER_EXPIRY'
    replacement = r'\1order_expiry=0'
    new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
    
    if new_content != content:
        with open('src/adapters/lighter_adapter.py', 'w', encoding='utf-8') as f:
            f.write(new_content)
        print("✅ Fixed IOC order_expiry in lighter_adapter.py")
    else:
        print("⚠️ Pattern not matched exactly, trying simpler approach...")
        # Simpler approach - just find the specific section
        if 'order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,' in content:
            # Count occurrences - we want to change only the one in _execute_close_order_ioc
            parts = content.split('_execute_close_order_ioc')
            if len(parts) >= 2:
                # Second part contains our IOC function
                ioc_part = parts[1].split('order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,', 1)
                if len(ioc_part) >= 2:
                    new_ioc_part = ioc_part[0] + 'order_expiry=0,  # IOC orders expire immediately' + ioc_part[1]
                    new_content = parts[0] + '_execute_close_order_ioc' + new_ioc_part
                    with open('src/adapters/lighter_adapter.py', 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print("✅ Fixed IOC order_expiry (simpler approach)")
else:
    print("❌ Could not find IOC pattern in lighter_adapter.py")

# Fix 2: monitor_funding_final.py - Check return value in sync_check_and_fix
print("\nFixing monitor_funding_final.py (sync_check_and_fix)...")

with open('scripts/monitor_funding_final.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_sync = '''                                try:
                                    await lighter.close_live_position(sym, original_side, notional)
                                    logger.info(f"✅ Closed orphaned Lighter {sym}")'''

new_sync = '''                                try:
                                    success, _ = await lighter.close_live_position(sym, original_side, notional)
                                    if success:
                                        logger.info(f"✅ Closed orphaned Lighter {sym}")
                                    else:
                                        logger.error(f"❌ Failed to close orphaned Lighter {sym} - close returned False")'''

if old_sync in content:
    content = content.replace(old_sync, new_sync)
    with open('scripts/monitor_funding_final.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("✅ Fixed sync_check_and_fix return value check")
else:
    print("❌ Could not find sync_check_and_fix pattern")

print("\n✅ All fixes applied. Restart the bot for changes to take effect.")
