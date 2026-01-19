"""
Pytest configuration for integration tests.

This file loads environment variables from .env and handles credential checking.
"""

import os
from pathlib import Path

import pytest


def pytest_configure(config):
    """
    Load environment variables from .env file before tests are collected.
    This is the earliest hook that runs before test collection.
    """
    # Find .env file (search up from current directory)
    current_dir = Path(__file__).resolve()
    env_paths = [
        current_dir.parent.parent.parent.parent / ".env",  # Project root
        Path.cwd() / ".env",  # Current working directory
    ]

    env_file = None
    for path in env_paths:
        if path.exists():
            env_file = path
            break

    if env_file:
        # Load environment variables from .env file
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue
                # Parse KEY=VALUE format
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    os.environ[key] = value


def pytest_collection_modifyitems(config, items):
    """
    Skip integration tests if API keys are missing after test collection.
    This runs AFTER pytest_configure, so environment variables are loaded.
    """
    import sys

    lighter_key = os.getenv("LIGHTER_API_KEY_PRIVATE_KEY")
    x10_key = os.getenv("X10_API_KEY")

    # Debug output
    print("\n[DEBUG] pytest_collection_modifyitems called", file=sys.stderr)
    print(f"[DEBUG] lighter_key value: {repr(lighter_key[:20] if lighter_key else None)}", file=sys.stderr)
    print(f"[DEBUG] x10_key value: {repr(x10_key[:20] if x10_key else None)}", file=sys.stderr)
    print(f"[DEBUG] bool(lighter_key): {bool(lighter_key)}", file=sys.stderr)
    print(f"[DEBUG] bool(x10_key): {bool(x10_key)}", file=sys.stderr)
    print(f"[DEBUG] Condition 'not lighter_key or not x10_key': {not lighter_key or not x10_key}", file=sys.stderr)

    if not lighter_key or not x10_key:
        print("[DEBUG] Skipping tests because condition is True", file=sys.stderr)
        # Skip all integration tests
        skip_marker = pytest.mark.skip(
            reason="Missing API keys - set LIGHTER_API_KEY_PRIVATE_KEY and X10_API_KEY to run"
        )
        for item in items:
            if item.get_closest_marker("integration"):
                item.add_marker(skip_marker)
