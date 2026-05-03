# -*- coding: utf-8 -*-
"""Compute single-symbol quant factor snapshots for the /analyze pipeline.

The quant research lab's factor library (``src/quant_research/factors/builtins``)
exposes pure ``df → pd.Series`` functions designed for cross-sectional
evaluation across a stock pool. For the per-stock /analyze flow we
just need the **latest value** of each factor on this one symbol so
the LLM can incorporate it into the report's reasoning.

This service is a thin adapter:
  * Takes an OHLCV DataFrame the pipeline already loaded (no extra fetch).
  * Calls each registered builtin factor function.
  * Returns a compact dict with factor id, localized name, latest value,
    and the registry's ``expected_direction`` so the LLM knows whether
    the value is bullish or bearish for forward returns.

Hard rules:
  * **Pure** — no DB writes, no network calls, no side effects.
  * **Best-effort** — a single broken factor never poisons the others;
    its slot is dropped from the output.
  * **Feature-flag aware** — returns ``None`` when
    ``QUANT_RESEARCH_ENABLED=false`` so the LLM doesn't see misleading
    "lab-disabled" data.
  * **Cheap** — no rolling window past what builtins already need.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# Minimum rows of OHLCV history needed before we attempt to compute any
# factor — the longest builtin lookback is 20d, so 30 rows leaves headroom
# for warm-up. Below this we skip the whole computation rather than
# returning a half-populated dict that would confuse the LLM.
_MIN_HISTORY_ROWS = 30


def _safe_finite(value: Any) -> Optional[float]:
    """Coerce to a JSON-friendly float, dropping NaN / inf."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _quant_research_enabled(config: Any) -> bool:
    return bool(getattr(config, "quant_research_enabled", False))


def compute_quant_signals(
    df: Optional[pd.DataFrame],
    *,
    config: Any,
    as_of: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute the latest value of every registered builtin factor.

    Parameters
    ----------
    df:
        Daily OHLCV panel for the symbol, sorted ascending by date.
        Must include at least ``close`` and ``volume`` columns. The
        pipeline already builds this for trend analysis, so callers
        pass it through directly.
    config:
        Top-level :class:`Config` (or any object with a
        ``quant_research_enabled`` attribute).
    as_of:
        Optional ISO date string for the snapshot timestamp. Defaults
        to the index value of the last row (or ``None`` if the df has
        no usable index).

    Returns
    -------
    A compact dict shaped like::

        {
          "as_of": "2026-05-04",
          "factors": [
            {
              "id": "return_5d",
              "name": "5-day return",
              "value": 0.034,
              "expected_direction": "positive",
              "description": "...short hint...",
            },
            ...
          ]
        }

    Returns ``None`` when:
      * the master flag is off,
      * the df is missing / empty / shorter than the warm-up minimum,
      * every factor fails (extremely rare).
    """
    if not _quant_research_enabled(config):
        return None
    if df is None or df.empty:
        return None
    if len(df) < _MIN_HISTORY_ROWS:
        logger.debug(
            "[quant_signals] skipping: only %d rows of OHLCV (need %d)",
            len(df), _MIN_HISTORY_ROWS,
        )
        return None
    if "close" not in df.columns:
        logger.debug("[quant_signals] skipping: df missing 'close' column")
        return None

    try:
        from src.quant_research.factors.builtins import BUILTIN_FACTORS
    except Exception as exc:  # noqa: BLE001 — defensive against optional deps
        logger.info(
            "[quant_signals] BUILTIN_FACTORS unavailable, skipping: %s", exc,
        )
        return None

    factors: List[Dict[str, Any]] = []
    for factor_id, entry in sorted(BUILTIN_FACTORS.items()):
        fn = entry.get("fn")
        if fn is None:
            continue
        try:
            series = fn(df)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[quant_signals] factor %r failed: %s", factor_id, exc,
            )
            continue
        if series is None or len(series) == 0:
            continue
        latest = _safe_finite(series.iloc[-1])
        if latest is None:
            # Factor warmup not satisfied yet; drop the slot rather than
            # showing a None to the LLM.
            continue
        factors.append({
            "id": factor_id,
            "name": entry.get("name") or factor_id,
            "value": round(latest, 6),
            "expected_direction": entry.get("expected_direction") or "unknown",
            "description": entry.get("description") or "",
        })

    if not factors:
        return None

    if as_of is None:
        # Best-effort: pull the last row's date from the index or a
        # ``date`` column if present.
        try:
            last_index = df.index[-1]
            if hasattr(last_index, "isoformat"):
                as_of = last_index.isoformat()
            elif "date" in df.columns:
                last_date = df["date"].iloc[-1]
                if hasattr(last_date, "isoformat"):
                    as_of = last_date.isoformat()
                else:
                    as_of = str(last_date)
            else:
                as_of = None
        except Exception:  # noqa: BLE001
            as_of = None

    return {
        "as_of": as_of,
        "factors": factors,
    }
