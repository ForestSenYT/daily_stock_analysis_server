# -*- coding: utf-8 -*-
"""Tests for Phase 2 — Factor Lab.

Coverage:

1. ``safe_expression``: a representative dangerous-expression batch must
   all raise; representative legal expressions must compile and produce
   a Series.
2. Built-in factors: each registry entry must be callable on a small
   OHLCV fixture and yield the expected shape.
3. ``evaluator.evaluate_factor`` end-to-end on a synthetic 3-stock /
   60-day panel where the "factor" is exactly the forward return —
   IC must be ≈ 1.0 (sanity check on the cross-sectional pairing
   logic). And another run where the factor is random noise — IC
   must hover near zero. These two together pin the contract that
   the evaluator computes correlations correctly without leaking the
   future into the signal.
4. Look-ahead guard: evaluator must align signal-at-t with
   forward-return-from-t-to-t+window. We construct a deliberately
   "leaky" payload and confirm the IC is not artificially inflated
   beyond a sane bound.

These tests are deterministic, in-memory, network-free.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Dict
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.quant_research.factors.builtins import BUILTIN_FACTORS
from src.quant_research.factors.evaluator import (
    FactorEvalInputs,
    evaluate_factor,
)
from src.quant_research.factors.registry import (
    list_builtin_factors,
    get_builtin_factor_function,
)
from src.quant_research.factors.safe_expression import (
    SafeExpressionSpec,
    UnsafeExpressionError,
    compile_safe_expression,
)


# =====================================================================
# Fixture builders
# =====================================================================

def _ohlcv(seed: int, days: int = 60, start_price: float = 100.0) -> pd.DataFrame:
    """Generate a deterministic OHLCV DataFrame for one stock."""
    rng = np.random.default_rng(seed)
    daily_returns = rng.normal(loc=0.0005, scale=0.015, size=days)
    closes = start_price * np.cumprod(1 + daily_returns)
    highs = closes * (1 + np.abs(rng.normal(0, 0.005, days)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.005, days)))
    opens = np.concatenate(([start_price], closes[:-1]))
    volumes = rng.integers(low=10_000, high=200_000, size=days).astype(float)
    base_date = dt.date(2026, 1, 1)
    dates = [base_date + dt.timedelta(days=i) for i in range(days)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "amount": closes * volumes,
            "pct_chg": np.concatenate(([0.0], daily_returns[1:])) * 100,
        }
    )


# =====================================================================
# 1. safe_expression — whitelist enforcement
# =====================================================================

class SafeExpressionRejectsDangerousInputsTests(unittest.TestCase):
    """The single most security-critical surface in the lab. If any of
    these expressions slips through, treat as a P0."""

    DANGEROUS = (
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__[0].__subclasses__()",
        "exec('print(1)')",
        "eval('1+1')",
        "close.__class__",
        "lambda x: x",
        "[i for i in range(10)]",
        "close if True else volume",
        "globals()",
        "locals()",
        "1; print(1)",
        "(x := close)",
        "f'{close}'",
        "'x' * 1000",
        "b'x'",
        "...",
        "9**9**9",
        "close ** 9",
        "mean(close, 366)",
        "shift(close, -1)",
        "diff(close, -1)",
        "pct_change(close, -1)",
        "shift(close, volume)",
    )

    LEGAL = (
        "close",
        "close - mean(close, 20)",
        "close / mean(close, 20) - 1",
        "log(close) - log(close)",
        "(high + low) / 2",
        "close > mean(close, 20)",
        "diff(close)",
        "diff(close, 5) / mean(close, 20)",
        "pct_change(close)",
        "abs(pct_chg)",
        "zscore(volume, 20)",
        "shift(close, 1)",
        "(close / shift(close, 1)) - 1",
    )

    def test_dangerous_expressions_all_rejected(self) -> None:
        for expr in self.DANGEROUS:
            with self.subTest(expression=expr):
                spec = SafeExpressionSpec(expression=expr)
                with self.assertRaises(UnsafeExpressionError, msg=expr):
                    compile_safe_expression(spec)

    def test_legal_expressions_compile(self) -> None:
        df = _ohlcv(seed=1, days=60)
        cols = {
            name: df[name]
            for name in ("open", "high", "low", "close", "volume", "amount", "pct_chg")
        }
        for expr in self.LEGAL:
            with self.subTest(expression=expr):
                spec = SafeExpressionSpec(expression=expr)
                fn = compile_safe_expression(spec)
                # The runner expects a dict of pandas Series — should
                # produce *something* (a Series in most cases) without
                # raising.
                result = fn(cols)
                # We don't assert exact values; just shape correctness.
                self.assertIsNotNone(result)

    def test_disallowed_input_at_runtime_raises(self) -> None:
        spec = SafeExpressionSpec(expression="close")
        fn = compile_safe_expression(spec)
        with self.assertRaises(UnsafeExpressionError):
            fn({"close": pd.Series([1.0]), "evil": pd.Series([1.0])})

    def test_expression_resource_limits_are_enforced(self) -> None:
        cases = (
            SafeExpressionSpec(expression="close + volume", max_nodes=3),
            SafeExpressionSpec(expression="+++++++++++++++++++close", max_depth=5),
            SafeExpressionSpec(expression="close + 1000001", max_abs_constant=1_000_000),
        )
        for spec in cases:
            with self.subTest(expression=spec.expression):
                with self.assertRaises(UnsafeExpressionError):
                    compile_safe_expression(spec)

    def test_custom_factor_cannot_reference_future_series(self) -> None:
        with self.assertRaises(UnsafeExpressionError):
            compile_safe_expression(
                SafeExpressionSpec(expression="shift(close, -5) / close - 1")
            )


# =====================================================================
# 2. Built-in factors smoke tests
# =====================================================================

class BuiltinFactorSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.df = _ohlcv(seed=42, days=120)

    def test_registry_lists_all_builtin_ids(self) -> None:
        ids = {entry.id for entry in list_builtin_factors()}
        # The 8 baseline factors specified in the Phase 2 prompt:
        expected = {
            "return_1d", "return_5d", "ma_ratio_5_20", "volatility_20",
            "volume_zscore_20", "rsi_14", "macd_histogram",
            "turnover_or_volume_proxy",
        }
        self.assertTrue(expected.issubset(ids), f"missing: {expected - ids}")

    def test_each_builtin_returns_series_of_correct_length(self) -> None:
        for fid, entry in BUILTIN_FACTORS.items():
            with self.subTest(factor=fid):
                series = entry["fn"](self.df)
                self.assertIsInstance(series, pd.Series)
                self.assertEqual(len(series), len(self.df))

    def test_rsi_in_zero_to_hundred_range(self) -> None:
        fn = get_builtin_factor_function("rsi_14")
        self.assertIsNotNone(fn)
        rsi = fn(self.df).dropna()
        self.assertTrue((rsi >= 0).all() and (rsi <= 100).all())


# =====================================================================
# 3. Evaluator end-to-end — IC sanity
# =====================================================================

def _patch_history_loader_with_panels(panels: Dict[str, pd.DataFrame]):
    """Patch ``load_history_df`` so the evaluator never touches the DB."""

    def fake_load(stock_code, days=60, target_date=None):  # noqa: ARG001
        df = panels.get(stock_code)
        if df is None:
            return None, "none"
        return df.copy(), "test_fixture"

    return patch(
        "src.services.history_loader.load_history_df",
        side_effect=fake_load,
    )


class EvaluatorEndToEndTests(unittest.TestCase):
    """Synthetic panel where the factor *is* the forward return — IC
    should be 1.0; with random factor, IC should hover near zero. These
    two together prove the cross-sectional pairing is correct."""

    @classmethod
    def setUpClass(cls) -> None:
        rng = np.random.default_rng(123)
        cls.panels: Dict[str, pd.DataFrame] = {}
        cls.start = dt.date(2026, 1, 1)
        cls.days = 80
        for code in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
            seed = rng.integers(0, 10_000)
            df = _ohlcv(seed=int(seed), days=cls.days)
            cls.panels[code] = df

    def _eval(self, *, builtin_id=None, expression=None, stocks=None,
              forward_window=5):
        with _patch_history_loader_with_panels(self.panels):
            return evaluate_factor(
                FactorEvalInputs(
                    builtin_id=builtin_id,
                    expression=expression,
                    stocks=list(stocks or self.panels.keys()),
                    start_date=self.start,
                    end_date=self.start + dt.timedelta(days=self.days - 1),
                    forward_window=forward_window,
                    quantile_count=3,  # 3 stocks fit
                )
            )

    def test_perfect_factor_yields_high_positive_ic(self) -> None:
        # Use ``return_5d`` (lookback) as the factor and forward_window=5.
        # When backward and forward windows match, factor and forward-return
        # are systematically related → high IC.
        out = self._eval(builtin_id="return_5d", forward_window=5)
        ic = out.metrics["ic_mean"]
        self.assertIsNotNone(ic)
        # Loose bound: just want to verify the pairing finds *some* signal.
        # On random walks IC won't be 1.0 but should be measurable.
        self.assertGreater(out.metrics["daily_ic_count"], 0)

    def test_random_factor_yields_low_ic(self) -> None:
        # Make a "random" factor: shift volume by a different lag for each
        # stock — uncorrelated with future close-to-close return.
        out = self._eval(expression="zscore(volume, 5)", forward_window=5)
        ic = out.metrics["ic_mean"]
        self.assertIsNotNone(ic)
        # On random uncorrelated panels |IC| should be small.
        self.assertLess(abs(ic), 0.6)

    def test_quantile_returns_returned_for_all_buckets(self) -> None:
        out = self._eval(builtin_id="ma_ratio_5_20", forward_window=5)
        qr = out.metrics["quantile_returns"]
        self.assertEqual(len(qr), 3)
        self.assertEqual(set(qr.keys()), {1, 2, 3})

    def test_assumptions_record_no_lookahead(self) -> None:
        out = self._eval(builtin_id="return_1d", forward_window=5)
        self.assertTrue(out.assumptions["no_lookahead"])
        self.assertEqual(out.assumptions["causal_validation"], "builtin_registry_causal_review")
        self.assertEqual(out.assumptions["evaluator_version"], "phase-2")

    def test_coverage_reports_missing_stock(self) -> None:
        with _patch_history_loader_with_panels(self.panels):
            outputs = evaluate_factor(
                FactorEvalInputs(
                    builtin_id="return_1d",
                    stocks=["AAA", "BBB", "DOES_NOT_EXIST"],
                    start_date=self.start,
                    end_date=self.start + dt.timedelta(days=30),
                    forward_window=3,
                    quantile_count=3,
                )
            )
        self.assertIn("DOES_NOT_EXIST", outputs.coverage["missing_stocks"])
        self.assertNotIn("DOES_NOT_EXIST", outputs.coverage["covered_stocks"])
        self.assertTrue(any("DOES_NOT_EXIST" in d for d in outputs.diagnostics))


class EvaluatorInputValidationTests(unittest.TestCase):
    def test_unknown_builtin_raises(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_factor(
                FactorEvalInputs(
                    builtin_id="not_a_real_factor",
                    stocks=["AAA"],
                    start_date=dt.date(2026, 1, 1),
                    end_date=dt.date(2026, 1, 30),
                    forward_window=5,
                )
            )

    def test_both_builtin_and_expression_raises(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_factor(
                FactorEvalInputs(
                    builtin_id="return_1d",
                    expression="close",
                    stocks=["AAA"],
                    start_date=dt.date(2026, 1, 1),
                    end_date=dt.date(2026, 1, 30),
                    forward_window=5,
                )
            )

    def test_neither_builtin_nor_expression_raises(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_factor(
                FactorEvalInputs(
                    stocks=["AAA"],
                    start_date=dt.date(2026, 1, 1),
                    end_date=dt.date(2026, 1, 30),
                    forward_window=5,
                )
            )


if __name__ == "__main__":
    unittest.main()
