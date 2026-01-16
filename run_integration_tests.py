#!/usr/bin/env python
"""
Run integration tests with environment variables loaded from .env file.
This script bypasses subprocess environment variable propagation issues.
"""

import os
import sys
from pathlib import Path

def main():
    # Load environment variables from .env file
    env_file = Path(__file__).parent / ".env"

    if not env_file.exists():
        print(f"ERROR: .env file not found at {env_file}")
        sys.exit(1)

    print(f"Loading environment variables from {env_file}")

    # Read and parse .env file
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Parse KEY=VALUE format
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                os.environ[key] = value
                # Mask sensitive values in output
                if 'KEY' in key or 'SECRET' in key or 'TOKEN' in key:
                    display_value = value[:8] + '...' if len(value) > 8 else '***'
                else:
                    display_value = value
                print(f"  {key}={display_value}")

    # Verify required environment variables are set
    required_vars = ['LIGHTER_API_KEY_PRIVATE_KEY', 'X10_API_KEY']
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        print(f"\nERROR: Missing required environment variables: {missing_vars}")
        sys.exit(1)

    print("\n[OK] All required environment variables loaded")
    print("\nRunning integration tests...\n")

    # Now run pytest
    import pytest

    # Run pytest with the tests
    sys.exit(pytest.main([
        "tests/integration/liquidity/test_preflight_liquidity_live.py",
        "-v",
        "-s",
        "--tb=short"
    ]))

if __name__ == "__main__":
    main()
