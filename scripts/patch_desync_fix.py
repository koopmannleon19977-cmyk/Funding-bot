"""
Patch script to add DESYNC protection to monitor_funding_final.py
"""
import re

# Read the file
with open('scripts/monitor_funding_final.py', 'r', encoding='utf-8') as f:
    content = f.read()

# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 1: Add RECENTLY_OPENED_TRADES after WATCHDOG_TIMEOUT
# ═══════════════════════════════════════════════════════════════════════════════
old_watchdog = '''WATCHDOG_TIMEOUT = 120  # Sekunden ohne Daten = Verbindungsproblem'''

new_watchdog = '''WATCHDOG_TIMEOUT = 120  # Sekunden ohne Daten = Verbindungsproblem

# ============================================================
# DESYNC PROTECTION: Track recently attempted trades  
# ============================================================
# Trades attempted within this window are protected from sync_check closure
RECENTLY_OPENED_TRADES: Dict[str, float] = {}  # symbol -> timestamp when attempted
RECENTLY_OPENED_PROTECTION_SECONDS = 60.0  # Protect trades for 60s after open attempt'''

if old_watchdog in content:
    content = content.replace(old_watchdog, new_watchdog)
    print("✅ Added RECENTLY_OPENED_TRADES global")
else:
    print("❌ Could not find WATCHDOG_TIMEOUT line")

# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 2: Add RECENTLY_OPENED_TRADES registration before trade execution
# ═══════════════════════════════════════════════════════════════════════════════
old_opening = '''            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f} (Min=${min_req:.1f})")

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel('''

new_opening = '''            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f} (Min=${min_req:.1f})")
            
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: Register trade BEFORE execution to prevent sync_check race
            # This protects the trade from being closed as "orphan" during execution  
            # ═══════════════════════════════════════════════════════════════
            RECENTLY_OPENED_TRADES[symbol] = time.time()

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel('''

if old_opening in content:
    content = content.replace(old_opening, new_opening)
    print("✅ Added RECENTLY_OPENED_TRADES registration before trade execution")
else:
    print("❌ Could not find trade opening line")

# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 3: Modify sync_check_and_fix to use RECENTLY_OPENED_TRADES
# ═══════════════════════════════════════════════════════════════════════════════
old_recently_opened = '''        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Skip symbols that were recently opened (within 30 seconds)
        # Prevents race condition where sync check closes positions just opened
        # ═══════════════════════════════════════════════════════════════
        recently_opened = set()
        current_time = time.time()
        
        try:
            open_trades = await get_open_trades()
            for trade in open_trades:
                symbol = trade.get('symbol')
                if not symbol:
                    continue
                
                entry_time = parse_iso_time(trade.get('entry_time'))
                if entry_time:
                    # Convert to timestamp for comparison
                    entry_timestamp = entry_time.timestamp()
                    age_seconds = current_time - entry_timestamp
                    
                    if age_seconds < 30.0:  # 30 second grace period
                        recently_opened.add(symbol)
                        logger.debug(f"🔒 Skipping sync check for {symbol} (recently opened, age={age_seconds:.1f}s)")
        except Exception as e:
            logger.debug(f"Error checking recently opened trades: {e}")'''

new_recently_opened = '''        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Skip symbols that were recently opened (within protection window)
        # This includes BOTH DB-tracked trades AND in-flight trade attempts
        # ═══════════════════════════════════════════════════════════════
        recently_opened = set()
        current_time = time.time()
        
        # 1. Check RECENTLY_OPENED_TRADES dict (protects in-flight trades without DB entry)
        for sym, open_time in list(RECENTLY_OPENED_TRADES.items()):
            age = current_time - open_time
            if age < RECENTLY_OPENED_PROTECTION_SECONDS:
                recently_opened.add(sym)
                logger.debug(f"🔒 Skipping sync check for {sym} (in RECENTLY_OPENED_TRADES, age={age:.1f}s)")
            else:
                # Cleanup expired entries
                RECENTLY_OPENED_TRADES.pop(sym, None)
        
        # 2. Check DB trades (original logic)
        try:
            open_trades = await get_open_trades()
            for trade in open_trades:
                symbol = trade.get('symbol')
                if not symbol:
                    continue
                
                entry_time = parse_iso_time(trade.get('entry_time'))
                if entry_time:
                    # Convert to timestamp for comparison
                    entry_timestamp = entry_time.timestamp()
                    age_seconds = current_time - entry_timestamp
                    
                    if age_seconds < 30.0:  # 30 second grace period
                        recently_opened.add(symbol)
                        logger.debug(f"🔒 Skipping sync check for {symbol} (recently opened in DB, age={age_seconds:.1f}s)")
        except Exception as e:
            logger.debug(f"Error checking recently opened trades: {e}")'''

if old_recently_opened in content:
    content = content.replace(old_recently_opened, new_recently_opened)
    print("✅ Modified sync_check_and_fix to use RECENTLY_OPENED_TRADES")
else:
    print("❌ Could not find sync_check recently_opened section")

# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 4: Reduce POSITION_CACHE_TTL from 5.0 to 2.0
# ═══════════════════════════════════════════════════════════════════════════════
old_cache_ttl = '''POSITION_CACHE_TTL = 5.0  # 5s for fresher data (was 30s)'''
new_cache_ttl = '''POSITION_CACHE_TTL = 2.0  # REDUCED from 5.0s for faster desync detection'''

if old_cache_ttl in content:
    content = content.replace(old_cache_ttl, new_cache_ttl)
    print("✅ Reduced POSITION_CACHE_TTL to 2.0s")
else:
    print("❌ Could not find POSITION_CACHE_TTL line")

# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 5: Remove predictor.start() call (FundingPredictorV2 has no start method)
# ═══════════════════════════════════════════════════════════════════════════════
old_predictor = '''    # A) Prediction Engine holen
    predictor = get_predictor()
    await predictor.start()  # This loads history AND starts auto-save'''

new_predictor = '''    # A) Prediction Engine holen
    predictor = get_predictor()
    # Note: FundingPredictorV2 is ready to use immediately (no start() needed)'''

if old_predictor in content:
    content = content.replace(old_predictor, new_predictor)
    print("✅ Removed predictor.start() call")
else:
    print("❌ Could not find predictor.start() block")

# Write the patched file
with open('scripts/monitor_funding_final.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("\n✅ Patch complete! Run 'python -c \"import py_compile; py_compile.compile('scripts/monitor_funding_final.py')\"' to verify syntax.")
