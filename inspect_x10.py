import sys
import os
import inspect

sys.path.append(os.getcwd())

try:
    from x10.perpetual.trading_client import PerpetualTradingClient
    from x10.perpetual.accounts import StarkPerpetualAccount
    
    print("--- PerpetualTradingClient ---")
    for m in dir(PerpetualTradingClient):
        if not m.startswith("_"):
            print(f"  {m}")

    print("\n--- StarkPerpetualAccount ---")
    for m in dir(StarkPerpetualAccount):
        if not m.startswith("_"):
            print(f"  {m}")

    print("\n--- Searching for 'order' methods ---")
    
    def search_class(cls):
        for name in dir(cls):
            if "order" in name.lower() and not name.startswith("_"):
                try:
                    attr = getattr(cls, name)
                    if callable(attr):
                        print(f"Found {cls.__name__}.{name}: {inspect.signature(attr)}")
                except:
                    print(f"Found {cls.__name__}.{name} (signature error)")

    search_class(PerpetualTradingClient)
    search_class(StarkPerpetualAccount)

except ImportError as e:
    print(f"ImportError: {e}")
except Exception as e:
    print(f"Error: {e}")
