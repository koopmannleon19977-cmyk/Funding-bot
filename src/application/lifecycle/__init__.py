"""
Application lifecycle management.

This package handles startup, shutdown, and lifecycle events.
"""

from .shutdown import ShutdownOrchestrator, ShutdownResult

__all__ = ['ShutdownOrchestrator', 'ShutdownResult']
