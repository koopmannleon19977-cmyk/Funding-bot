"""Patch to add visible logging for RECENTLY_OPENED_TRADES protection"""
import re

# Read the file
with open('scripts/monitor_funding_final.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Old code block
old_code = '''        # 1. Check RECENTLY_OPENED_TRADES dict (protects in-flight trades without DB entry)
        for sym, open_time in list(RECENTLY_OPENED_TRADES.items()):
            age = current_time - open_time
            if age < RECENTLY_OPENED_PROTECTION_SECONDS:
                recently_opened.add(sym)
                logger.debug(f"🔒 Skipping sync check for {sym} (in RECENTLY_OPENED_TRADES, age={age:.1f}s)")
            else:
                # Cleanup expired entries
                RECENTLY_OPENED_TRADES.pop(sym, None)'''

# New code block with INFO logging
new_code = '''        # 1. Check RECENTLY_OPENED_TRADES dict (protects in-flight trades without DB entry)
        protected_by_dict = []
        for sym, open_time in list(RECENTLY_OPENED_TRADES.items()):
            age = current_time - open_time
            if age < RECENTLY_OPENED_PROTECTION_SECONDS:
                recently_opened.add(sym)
                protected_by_dict.append(f"{sym}({age:.0f}s)")
            else:
                # Cleanup expired entries
                logger.info(f"🗑️ Expired protection for {sym} (age={age:.1f}s)")
                RECENTLY_OPENED_TRADES.pop(sym, None)
        
        if protected_by_dict:
            logger.info(f"🛡️ RECENTLY_OPENED protection active: {protected_by_dict}")'''

if old_code in content:
    content = content.replace(old_code, new_code)
    print("✅ Added INFO logging for RECENTLY_OPENED_TRADES")
else:
    print("❌ Could not find the target code block")
    print("Searching for partial match...")
    if "RECENTLY_OPENED_TRADES.items()" in content:
        print("Found RECENTLY_OPENED_TRADES.items() - manual check needed")

# Also add logging when a trade is registered
old_register = '''            RECENTLY_OPENED_TRADES[symbol] = time.time()

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel('''

new_register = '''            RECENTLY_OPENED_TRADES[symbol] = time.time()
            logger.info(f"🛡️ Registered {symbol} in RECENTLY_OPENED_TRADES for {RECENTLY_OPENED_PROTECTION_SECONDS}s protection")

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel('''

if old_register in content:
    content = content.replace(old_register, new_register)
    print("✅ Added registration logging")
else:
    print("❌ Could not find registration code block")

# Write back
with open('scripts/monitor_funding_final.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("\n✅ Patch complete! Run syntax check with:")
print("   python -c \"import py_compile; py_compile.compile('scripts/monitor_funding_final.py'); print('Syntax OK')\"")
