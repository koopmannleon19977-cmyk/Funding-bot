import time
import random
import sys

def verify_id_generation():
    print("Verifying Order ID Generation...")
    ids = set()
    warnings = []
    
    # Int64 Max
    MAX_INT64 = 9223372036854775807
    
    start_time = time.time()
    
    # Simulate burst of 1000 IDs
    for i in range(1000):
        # The logic we implemented:
        client_oid = int(time.time() * 1_000_000) * 1000 + random.randint(0, 999)
        
        if client_oid in ids:
            warnings.append(f"Duplicate found at iteration {i}: {client_oid}")
        
        if client_oid > MAX_INT64:
            warnings.append(f"Overflow at iteration {i}: {client_oid} > MAX_INT64")
            
        ids.add(client_oid)
        # No sleep, simulate max burst speed
        
    end_time = time.time()
    
    if warnings:
        print("❌ Verification Failed with warnings:")
        for w in warnings:
            print(w)
        sys.exit(1)
        
    print(f"✅ Generated {len(ids)} unique IDs in {end_time - start_time:.4f}s")
    print(f"Sample ID: {list(ids)[0]}")
    print(f"Max Int64: {MAX_INT64}")
    print("Logic is SAFE and UNIQUE.")

if __name__ == "__main__":
    verify_id_generation()
