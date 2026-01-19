"""
Pytest configuration for funding_bot tests.

This file ensures the src directory is in the Python path
so that imports work correctly.
"""

import os
import sys
from pathlib import Path

import pytest

# Add src directory to Python path
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))


# =============================================================================
# Pytest Configuration
# =============================================================================


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "unit: marks tests as unit tests (fast, isolated, no external deps). Default marker.",
    )
    config.addinivalue_line(
        "markers",
        "integration: marks tests requiring external dependencies (APIs, SDKs, env vars). "
        "Skipped by default in CI unless required env vars are set.",
    )


def pytest_collection_modifyitems(config, items):
    """
    Skip integration tests if required env vars are not set.

    Integration tests are marked with @pytest.mark.integration and require
    specific environment variables to run (e.g., API keys, exchange credentials).
    """
    # Env vars that enable integration tests
    required_env_vars = ["LIGHTER_API_KEY_PRIVATE_KEY", "X10_API_KEY"]
    has_integration_env = all(os.environ.get(var) for var in required_env_vars)

    for item in items:
        if "integration" in item.keywords and not has_integration_env:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"Integration test skipped: set required env vars ({', '.join(required_env_vars)}) to run"
                )
            )


# =============================================================================
# Common Fixtures
# =============================================================================


@pytest.fixture
def sample_decimal():
    """Sample Decimal fixture for financial calculations."""
    from decimal import Decimal

    return Decimal("100.00")
