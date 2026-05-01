# -*- coding: utf-8 -*-
"""Factor registry — single source of truth for built-in factor lookup.

The HTTP layer (``GET /api/v1/quant/factors``) returns a list of
``BuiltinFactorEntry`` rows from this module; the evaluator
(``factors.evaluator``) resolves a factor by ``builtin_id`` here.

Adding a new builtin: implement the function in ``builtins.py`` and add
the entry to ``BUILTIN_FACTORS`` there. This module only exposes
read-only views — no mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

import pandas as pd

from src.quant_research.factors.builtins import BUILTIN_FACTORS


@dataclass(frozen=True)
class BuiltinFactorEntry:
    """Read-only snapshot of one built-in factor for the registry view."""
    id: str
    name: str
    description: str
    expected_direction: str  # "positive" / "negative" / "unknown"
    lookback_days: int


def list_builtin_factors() -> List[BuiltinFactorEntry]:
    """Return entries sorted alphabetically by id (stable for FE)."""
    return [
        BuiltinFactorEntry(
            id=fid,
            name=entry["name"],
            description=entry["description"],
            expected_direction=entry["expected_direction"],
            lookback_days=entry["lookback_days"],
        )
        for fid, entry in sorted(BUILTIN_FACTORS.items())
    ]


def get_builtin_factor_function(factor_id: str) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Resolve ``factor_id`` to its compute function, or ``None`` if missing."""
    entry = BUILTIN_FACTORS.get(factor_id)
    if entry is None:
        return None
    return entry["fn"]


def get_builtin_factor_meta(factor_id: str) -> Optional[BuiltinFactorEntry]:
    """Return entry metadata only (no callable) for a single factor."""
    entry = BUILTIN_FACTORS.get(factor_id)
    if entry is None:
        return None
    return BuiltinFactorEntry(
        id=factor_id,
        name=entry["name"],
        description=entry["description"],
        expected_direction=entry["expected_direction"],
        lookback_days=entry["lookback_days"],
    )
