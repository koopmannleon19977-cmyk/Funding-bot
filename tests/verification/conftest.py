"""
Verification tests configuration.

All tests in this directory are integration tests that require real adapters
and external dependencies (Lighter/X10 APIs, database, etc.).

These tests are marked as integration and will be skipped unless the required
environment variables are set (LIGHTER_API_KEY, X10_API_KEY).
"""

import pytest


def pytest_collection_modifyitems(session, config, items):
    """
    Automatically mark all tests in this directory as integration tests.
    This ensures the marker is applied even if individual test files don't import pytest.
    """
    for item in items:
        # Mark all tests in the verification directory as integration tests
        if "verification" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
