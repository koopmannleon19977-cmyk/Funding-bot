

try:
    from lighter.signer_client import SignerClient
    import inspect
    sig = inspect.signature(SignerClient.cancel_all_orders)
    print(f"Signature: {sig}")
    for name, param in sig.parameters.items():
        print(f"Param: {name}, Default: {param.default}")
except ImportError:
    print("Lighter SDK not found")
except Exception as e:
    print(e)
