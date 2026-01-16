"""
Shared types for opportunity scanning.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FilterResult:
    """Result of filter pipeline."""

    passed: bool
    reason: str = ""
