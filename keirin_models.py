"""Shared data models for the Keirin predictor suite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


DEFAULT_LEG_TYPES: Tuple[str, ...] = ("逃げ", "捲り", "追込", "自在")
"""Common riding style names (脚質) used in race forms."""


@dataclass(frozen=True)
class Rider:
    """Representation of a rider for prediction purposes."""

    name: str
    score: float
    upset: float
    leg_type: str
    line: Optional[str] = None
    line_power: Optional[float] = None


__all__ = ["DEFAULT_LEG_TYPES", "Rider"]

