# -*- coding: utf-8 -*-
"""Tests for the cross-sectional quant context service.

Two layers:

1. **Pure-function shape tests** — ``_percentile_rank`` and
   ``_interpret_percentile`` independent of any DB or config.

2. **Integration with stub DB** — feed synthetic OHLCV through a
   mock ``DatabaseManager`` and confirm the service produces a
   well-shaped dict ready for sub-agent injection.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from typing import List

import numpy as np
import pandas as pd

from src.services.cross_sectional_quant_service import (
    _interpret_percentile,
    _percentile_rank,
    _resolve_pool,
    clear_cache,
    compute_cross_sectional_context,
    get_or_compute,
)


# =====================================================================
# Helpers
# =====================================================================

def _make_bars(n: int = 60, seed: int = 1):
    """Synthetic OHLCV "bars" — list of namespaces with ``to_dict()``
    so they look like the real DataPoint rows the pipeline DB returns."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 1.0, size=n))
    opens = closes + rng.normal(0, 0.3, size=n)
    highs = np.maximum(closes, opens) + rng.uniform(0.1, 1.0, size=n)
    lows = np.minimum(closes, opens) - rng.uniform(0.1, 1.0, size=n)
    volumes = rng.integers(1_000_000, 5_000_000, size=n)
    dates = [(date.today() - timedelta(days=n - i)).isoformat() for i in range(n)]

    bars = []
    for i in range(n):
        row = {
            "date": dates[i],
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": int(volumes[i]),
        }
        # to_dict() mimics the real DataPoint shape
        bar = SimpleNamespace(**row)
        bar.to_dict = (lambda r=row: r)
        bars.append(bar)
    return bars


class _StubDB:
    """Minimal stand-in for DatabaseManager with seedable per-symbol
    OHLCV histories."""

    def __init__(self, histories: dict):
        self._histories = histories  # {symbol: List[bar]}

    def get_data_range(self, code, start, end):
        return self._histories.get(code.upper(), [])


# =====================================================================
# Pure helpers
# =====================================================================

class PureHelperTests(unittest.TestCase):
    def test_percentile_rank_middle(self):
        sample = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(_percentile_rank(3.0, sample), 50.0)

    def test_percentile_rank_max(self):
        sample = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(_percentile_rank(10.0, sample), 100.0)

    def test_percentile_rank_min(self):
        sample = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(_percentile_rank(0.5, sample), 0.0)

    def test_percentile_rank_empty(self):
        self.assertIsNone(_percentile_rank(1.0, []))

    def test_interpret_high_negative_factor(self):
        # high RSI on a "negative" factor (mean-revert) → bearish
        self.assertIn("bearish", _interpret_percentile(95.0, "negative"))

    def test_interpret_low_positive_factor(self):
        # low momentum on a "positive" factor → bearish
        self.assertIn("bearish", _interpret_percentile(5.0, "positive"))

    def test_interpret_mid(self):
        self.assertEqual(_interpret_percentile(50.0, "positive"), "mid")

    def test_resolve_pool_us_has_aapl(self):
        pool = _resolve_pool("US")
        self.assertIn("AAPL", pool)

    def test_resolve_pool_unknown_returns_empty(self):
        self.assertEqual(_resolve_pool("xx"), ())


# =====================================================================
# Integration with stub DB
# =====================================================================

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_returns_none_when_pool_too_thin(self):
        # Only one pool member has data — should bail out (need >= 5).
        db = _StubDB({"AAPL": _make_bars(60)})
        result = compute_cross_sectional_context("AAPL", "us", db=db)
        self.assertIsNone(result)

    def test_returns_none_for_unknown_market(self):
        db = _StubDB({})
        self.assertIsNone(
            compute_cross_sectional_context("AAPL", "xx", db=db)
        )

    def test_returns_factors_with_well_loaded_pool(self):
        # Seed all 20 US pool members + the target with synthetic
        # histories so every factor function can compute.
        from src.services.cross_sectional_quant_service import _US_POOL

        histories = {sym: _make_bars(60, seed=hash(sym) % 1000) for sym in _US_POOL}
        # AAPL is in the pool — make sure target df is also there.
        result = compute_cross_sectional_context("AAPL", "us", db=_StubDB(histories))
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["market"], "us")
        self.assertGreaterEqual(result["pool_size_eligible"], 5)
        self.assertGreater(len(result["factors"]), 0)
        for f in result["factors"]:
            self.assertIn("id", f)
            self.assertIn("value", f)
            self.assertIn("percentile", f)
            self.assertIn("expected_direction", f)
            self.assertIn("interpretation", f)
            # Percentile within bounds.
            self.assertGreaterEqual(f["percentile"], 0)
            self.assertLessEqual(f["percentile"], 100)

    def test_target_df_param_avoids_redundant_db_call(self):
        from src.services.cross_sectional_quant_service import _US_POOL

        # Seed pool but NOT the target's symbol in the DB; pass target_df explicitly.
        histories = {sym: _make_bars(60, seed=hash(sym) % 1000) for sym in _US_POOL[1:]}
        target_bars = _make_bars(60, seed=42)
        target_df = pd.DataFrame([b.to_dict() for b in target_bars])
        result = compute_cross_sectional_context(
            "AAPL", "us", db=_StubDB(histories), target_df=target_df,
        )
        self.assertIsNotNone(result)
        self.assertGreater(len(result["factors"]), 0)

    def test_cache_round_trip(self):
        from src.services.cross_sectional_quant_service import _US_POOL

        histories = {sym: _make_bars(60, seed=hash(sym) % 1000) for sym in _US_POOL}
        db = _StubDB(histories)

        first = get_or_compute("AAPL", "us", db=db)
        self.assertIsNotNone(first)

        # Second call should NOT re-query — break the DB so it would
        # raise if it did, then confirm we get the same payload back.
        broken_db = _StubDB({})
        second = get_or_compute("AAPL", "us", db=broken_db)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
