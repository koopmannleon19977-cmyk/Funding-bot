import time
import random
import sys

def verify_strategies():
    print("Verifying Order ID Strategies...")
    MAX_INT64 = 9223372036854775807
    
    strategies = {
        "A: time_ns()": lambda: time.time_ns(),
        "B: ms*1M + rand(1M)": lambda: int(time.time() * 1000) * 1_000_000 + random.randint(0, 999_999),
        "C: old_failed_improved": lambda: int(time.time() * 1_000_000) * 1000 + random.randint(0, 999) # This failed
    }

    for name, func in strategies.items():
        print(f"\nTesting {name}...")
        ids = set()
        collisions = 0
        min_val = float('inf')
        max_val = 0
        
        start = time.time()
        for i in range(5000): # 5000 iterations to stress test
            val = func()
            if val in ids:
                collisions += 1
            ids.add(val)
            if val < min_val: min_val = val
            if val > max_val: max_val = val
            
        print(f"  Collisions: {collisions}/5000")
        print(f"  Max val safe? {max_val < MAX_INT64} (Val: {max_val})")
        
        if collisions == 0 and max_val < MAX_INT64:
            print(f"  ✅ CANDIDATE PASSED")
        else:
            print(f"  ❌ FAILED")

if __name__ == "__main__":
    verify_strategies()
