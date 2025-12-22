"""Expose the dashboard API server from the infrastructure layer for unit tests."""
from src.infrastructure.api_server import DashboardApi

__all__ = ["DashboardApi"]
