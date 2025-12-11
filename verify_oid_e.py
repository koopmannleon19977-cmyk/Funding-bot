import time
import random
import sys

def verify_strategy_e():
    print("Verifying Strategy E (Epoch Offset + Huge Random)...")
    MAX_INT64 = 9223372036854775807
    EPOCH_OFFSET = 1_700_000_000
    
    ids = set()
    collisions = 0
    max_val = 0
    
    start = time.time()
    for i in range(10000): # 10k iterations
        # Strategy E
        ts = time.time() - EPOCH_OFFSET
        # ms * 100M + rand(100M)
        val = int(ts * 1000) * 100_000_000 + random.randint(0, 99_999_999)
        
        if val in ids:
            collisions += 1
        ids.add(val)
        if val > max_val: max_val = val
        
    print(f"Collisions: {collisions}/10000")
    print(f"Max Val: {max_val}")
    print(f"Safe? {max_val < MAX_INT64}")
    
    if collisions == 0 and max_val < MAX_INT64:
        print("✅ STRATEGY E PASSED")
    else:
        print("❌ FAILED")

if __name__ == "__main__":
    verify_strategy_e()
