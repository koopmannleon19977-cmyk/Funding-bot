
import sys
sys.path.append('c:\\Users\\koopm\\funding-bot') # Add project root to path
try:
    from lighter.signer_client import SignerClient
    print("SignerClient attributes:")
    count = 0
    for d in dir(SignerClient):
        if d.startswith('ORDER_'):
            print(f"{d} = {getattr(SignerClient, d)}")
            count += 1
    if count == 0:
        print("No ORDER_ attributes found.")
except ImportError as e:
    print(f"Could not import SignerClient: {e}")
except Exception as e:
    print(f"Error: {e}")
