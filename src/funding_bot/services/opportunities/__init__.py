"""
Opportunity engine package (facade).
"""

from __future__ import annotations

from funding_bot.services.opportunities.engine import OpportunityEngine
from funding_bot.services.opportunities.types import FilterResult

__all__ = ["OpportunityEngine", "FilterResult"]
