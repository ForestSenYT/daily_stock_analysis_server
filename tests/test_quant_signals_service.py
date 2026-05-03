# -*- coding: utf-8 -*-
"""Tests for the quant signals service used by the /analyze pipeline.

Two layers:

1. **Pure-function shape tests** — feed a synthetic OHLCV DataFrame,
   confirm the output dict matches the contract the executor injects
   into the LLM user message (factor id / name / value / direction /
   description; latest values are JSON-friendly floats).

2. **Defensive paths** — disabled flag short-circuits to None;
   too-short history skips; bad column shapes don't crash.
"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from src.services.quant_signals_service import compute_quant_signals


def _make_df(n: int = 60, seed: int = 1) -> pd.DataFrame:
    """Synthetic OHLCV with enough history to satisfy 20-day windows."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 1.0, size=n))
    opens = closes + rng.normal(0, 0.3, size=n)
    highs = np.maximum(closes, opens) + rng.uniform(0.1, 1.0, size=n)
    lows = np.minimum(closes, opens) - rng.uniform(0.1, 1.0, size=n)
    volumes = rng.integers(1_000_000, 5_000_000, size=n)
    dates = pd.date_range(end="2026-05-04", periods=n, freq="B")
    return pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _enabled_config() -> SimpleNamespace:
    return SimpleNamespace(quant_research_enabled=True)


def _disabled_config() -> SimpleNamespace:
    return SimpleNamespace(quant_research_enabled=False)


class QuantSignalsShapeTests(unittest.TestCase):

    def test_returns_factors_list_for_full_history(self) -> None:
        df = _make_df(60)
        out = compute_quant_signals(df, config=_enabled_config())
        self.assertIsNotNone(out)
        self.assertIn("factors", out)
        factors = out["factors"]
        # At least the "always-on" short-window factors should compute.
        self.assertGreater(len(factors), 0)

    def test_factor_row_shape(self) -> None:
        df = _make_df(60)
        out = compute_quant_signals(df, config=_enabled_config())
        for f in out["factors"]:
            self.assertIn("id", f)
            self.assertIn("name", f)
            self.assertIn("value", f)
            self.assertIn("expected_direction", f)
            self.assertIn(f["expected_direction"], {"positive", "negative", "unknown"})
            # Value must be a finite float (no NaN / inf leaking out).
            self.assertIsInstance(f["value"], float)
            self.assertFalse(math.isnan(f["value"]))
            self.assertFalse(math.isinf(f["value"]))

    def test_as_of_picked_from_dates(self) -> None:
        df = _make_df(60)
        out = compute_quant_signals(df, config=_enabled_config())
        self.assertIsNotNone(out.get("as_of"))
        # Either ISO date or string starting with year — accept both.
        self.assertTrue(str(out["as_of"]).startswith("2026-"))


class QuantSignalsDefensiveTests(unittest.TestCase):

    def test_disabled_flag_returns_none(self) -> None:
        df = _make_df(60)
        out = compute_quant_signals(df, config=_disabled_config())
        self.assertIsNone(out)

    def test_none_df_returns_none(self) -> None:
        out = compute_quant_signals(None, config=_enabled_config())
        self.assertIsNone(out)

    def test_empty_df_returns_none(self) -> None:
        out = compute_quant_signals(pd.DataFrame(), config=_enabled_config())
        self.assertIsNone(out)

    def test_too_short_history_returns_none(self) -> None:
        # Below the 30-row warm-up minimum.
        df = _make_df(15)
        out = compute_quant_signals(df, config=_enabled_config())
        self.assertIsNone(out)

    def test_missing_close_column_returns_none(self) -> None:
        df = _make_df(60).drop(columns=["close"])
        out = compute_quant_signals(df, config=_enabled_config())
        self.assertIsNone(out)

    def test_individual_factor_failure_does_not_kill_others(self) -> None:
        """If one factor raises, the rest should still come through.
        Patch a builtin to raise and confirm the output still has
        the other factors."""
        import src.quant_research.factors.builtins as builtins_mod

        # Find any one builtin and break its function. We restore in
        # tearDown via a try/finally so subsequent tests aren't poisoned.
        target_id = next(iter(builtins_mod.BUILTIN_FACTORS.keys()))
        original_fn = builtins_mod.BUILTIN_FACTORS[target_id]["fn"]

        def _boom(_df):
            raise RuntimeError("synthetic factor failure")

        builtins_mod.BUILTIN_FACTORS[target_id]["fn"] = _boom
        try:
            df = _make_df(60)
            out = compute_quant_signals(df, config=_enabled_config())
            self.assertIsNotNone(out)
            ids = {f["id"] for f in out["factors"]}
            self.assertNotIn(target_id, ids)
            self.assertGreater(len(ids), 0)
        finally:
            builtins_mod.BUILTIN_FACTORS[target_id]["fn"] = original_fn


if __name__ == "__main__":
    unittest.main()
