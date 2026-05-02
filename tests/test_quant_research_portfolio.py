# -*- coding: utf-8 -*-
"""Phase 4 — Portfolio Optimizer + Research Risk tests.

Pure-math tests against deterministic fixtures (no DB / no network).
Two layers:

1. ``optimizer.py``  — verify each of the 5 algorithms, then verify the
   constraint pipeline (long_only / floor / ceiling / cash / turnover)
   is applied in the documented order.
2. ``risk.py``       — verify concentration / VaR / CVaR / drawdown /
   volatility / beta on hand-computed inputs where the answer is known
   in closed form.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.quant_research.portfolio.optimizer import (
    PortfolioOptimizerInputs,
    optimize_portfolio,
)
from src.quant_research.portfolio.risk import (
    ResearchRiskInputs,
    compute_beta,
    compute_concentration,
    compute_drawdown,
    compute_historical_cvar,
    compute_historical_var,
    compute_volatility,
    evaluate_research_risk,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

def _three_stock_panel(seed: int = 7, days: int = 250) -> pd.DataFrame:
    """3-stock daily-returns panel. Stock A is the most volatile, B
    in the middle, C the calmest — used to check inverse-vol weighting
    monotonicity."""
    rng = np.random.default_rng(seed)
    idx = [date(2026, 1, 1) + timedelta(days=i) for i in range(days)]
    return pd.DataFrame(
        {
            "AAA": rng.normal(0.001, 0.030, days),
            "BBB": rng.normal(0.001, 0.015, days),
            "CCC": rng.normal(0.001, 0.005, days),
        },
        index=idx,
    )


# =====================================================================
# Optimizer
# =====================================================================

class OptimizerEqualWeightTests(unittest.TestCase):
    def test_equal_weight_simple(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
        ))
        self.assertEqual(out.status, "ok")
        self.assertEqual(set(out.weights), {"AAA", "BBB", "CCC"})
        for v in out.weights.values():
            self.assertAlmostEqual(v, 1 / 3, places=5)

    def test_equal_weight_with_cash_reserve(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
            cash_weight=0.4,
        ))
        # Each name gets (1 - 0.4) / 3 ≈ 0.2
        for v in out.weights.values():
            self.assertAlmostEqual(v, 0.6 / 3, places=5)
        self.assertAlmostEqual(out.cash_weight, 0.4)


class OptimizerInverseVolTests(unittest.TestCase):
    def test_lowest_vol_gets_highest_weight(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="inverse_volatility",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
        ))
        self.assertEqual(out.status, "ok")
        # CCC (lowest vol) > BBB > AAA
        self.assertGreater(out.weights["CCC"], out.weights["BBB"])
        self.assertGreater(out.weights["BBB"], out.weights["AAA"])
        # Sum to 1.0
        self.assertAlmostEqual(sum(out.weights.values()), 1.0, places=5)


class OptimizerMinVarTests(unittest.TestCase):
    def test_minvar_concentrates_more_than_invvol(self) -> None:
        panel = _three_stock_panel()
        invvol = optimize_portfolio(PortfolioOptimizerInputs(
            objective="inverse_volatility",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
        ))
        minvar = optimize_portfolio(PortfolioOptimizerInputs(
            objective="min_variance_simplified",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
        ))
        # Min-variance with diagonal cov uses 1/σ² which concentrates
        # more weight on the lowest-vol name than 1/σ does.
        self.assertGreater(minvar.weights["CCC"], invvol.weights["CCC"])


class OptimizerRiskBudgetPlaceholderTests(unittest.TestCase):
    def test_returns_not_supported(self) -> None:
        panel = _three_stock_panel(days=10)
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="risk_budget_placeholder",
            symbols=["AAA", "BBB"],
            returns_panel=panel,
        ))
        self.assertEqual(out.status, "not_supported")
        self.assertEqual(out.weights, {})


class OptimizerConstraintPipelineTests(unittest.TestCase):
    def test_max_weight_cap_redistributes(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="min_variance_simplified",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
            max_weight_per_symbol=0.5,
        ))
        self.assertEqual(out.status, "ok")
        # Without cap, CCC would dominate (~80%+); with cap, ≤ 50%
        for sym, w in out.weights.items():
            self.assertLessEqual(w, 0.5 + 1e-6, f"{sym} exceeded cap")
        self.assertAlmostEqual(sum(out.weights.values()), 1.0, places=4)

    def test_min_weight_floor_drops_tiny(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="inverse_volatility",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
            min_weight_per_symbol=0.30,  # AAA's allocation is too small
        ))
        # The lowest-weight name (AAA) should be dropped or kept ≥ floor
        for w in out.weights.values():
            self.assertGreaterEqual(w, 0.30 - 1e-6)

    def test_max_turnover_blends_toward_current(self) -> None:
        panel = _three_stock_panel()
        current = {"AAA": 0.0, "BBB": 0.0, "CCC": 1.0}  # 100% in CCC
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
            current_weights=current,
            max_turnover=0.20,  # only allow 20% L1 turnover
        ))
        # Without turnover cap → 1/3 each. With cap, weights stay closer
        # to current. Expect CCC > BBB ≈ AAA.
        self.assertGreater(out.weights.get("CCC", 0.0), out.weights.get("BBB", 0.0))
        diff = sum(abs(out.weights.get(k, 0.0) - current[k]) for k in current) / 2.0
        self.assertLessEqual(diff, 0.20 + 1e-6)

    def test_long_only_clamps_negatives(self) -> None:
        # Build a fake panel where one stock has positive mean and the
        # other negative; both with non-zero variance so the optimizer
        # can compute meaningful scores.
        rng = np.random.default_rng(42)
        idx = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
        panel = pd.DataFrame(
            {
                "WIN": rng.normal(0.005, 0.01, 60),    # +50 bps/day mean
                "LOSER": rng.normal(-0.005, 0.01, 60),  # -50 bps/day mean
            },
            index=idx,
        )
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="max_sharpe_simplified",
            symbols=["WIN", "LOSER"],
            returns_panel=panel,
            long_only=True,
        ))
        # LOSER should be either dropped or weight = 0
        self.assertGreaterEqual(out.weights.get("LOSER", 0.0), 0.0)
        self.assertEqual(out.weights.get("LOSER", 0.0), 0.0)
        # WIN should hold all the long-only weight
        self.assertAlmostEqual(out.weights.get("WIN", 0.0), 1.0, places=5)

    def test_unknown_symbol_dropped_with_diagnostic(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB", "DOES_NOT_EXIST"],
            returns_panel=panel,
        ))
        self.assertEqual(out.status, "ok")
        self.assertNotIn("DOES_NOT_EXIST", out.weights)
        self.assertTrue(any("missing" in d.lower() for d in out.diagnostics))

    def test_sector_limit_is_partial_coverage(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB", "CCC"],
            returns_panel=panel,
            sector_exposure_limit={"tech": 0.4},
        ))
        self.assertEqual(out.status, "ok")
        self.assertEqual(
            out.assumptions.get("sector_constraint_status"),
            "partial_coverage",
        )

    def test_no_real_orders_emitted(self) -> None:
        panel = _three_stock_panel()
        out = optimize_portfolio(PortfolioOptimizerInputs(
            objective="equal_weight",
            symbols=["AAA", "BBB"],
            returns_panel=panel,
        ))
        self.assertIs(out.assumptions.get("trade_orders_emitted"), False)
        self.assertIs(out.assumptions.get("is_research_only"), True)


# =====================================================================
# Risk
# =====================================================================

class ConcentrationTests(unittest.TestCase):
    def test_alert_at_threshold(self) -> None:
        c = compute_concentration({"A": 0.6, "B": 0.4}, threshold_pct=50.0)
        self.assertTrue(c["alert"])
        self.assertEqual(c["top_weight_pct"], 60.0)

    def test_below_threshold(self) -> None:
        c = compute_concentration({"A": 0.4, "B": 0.4, "C": 0.2}, threshold_pct=50.0)
        self.assertFalse(c["alert"])

    def test_empty_weights(self) -> None:
        c = compute_concentration({})
        self.assertEqual(c["rows"], [])
        self.assertEqual(c["hhi"], 0.0)


class VolatilityDrawdownTests(unittest.TestCase):
    def test_volatility_constant_returns_zero(self) -> None:
        s = pd.Series([0.0] * 30)
        v = compute_volatility(s)
        self.assertEqual(v["daily"], 0.0)

    def test_max_drawdown_known_case(self) -> None:
        # Up 10%, then -50%, then flat → DD = -50% from peak
        rets = pd.Series([0.10, -0.50, 0.0])
        dd = compute_drawdown(rets)
        # NAV = 1.10, 0.55, 0.55. Peak = 1.10. DD = 0.55/1.10 - 1 = -0.5
        self.assertAlmostEqual(dd["max_drawdown"], -0.5, places=4)


class VaRCVaRTests(unittest.TestCase):
    def test_var_at_95_picks_5th_percentile(self) -> None:
        # 100 returns: 90 zeros + 10 losses of -10%. Sorted ascending,
        # the first 10 entries are -0.10; the 5th-percentile of 100
        # observations sits in the middle of that block, so VaR(95) ≈ -0.10.
        rets = pd.Series([-0.10] * 10 + [0.0] * 90)
        var = compute_historical_var(rets, confidence=0.95)
        self.assertIsNotNone(var)
        self.assertLess(var, -0.05)

    def test_cvar_is_at_or_below_var(self) -> None:
        rets = pd.Series([0.01, 0.02, -0.01, -0.02, -0.05, -0.10] * 10)
        var = compute_historical_var(rets, confidence=0.90)
        cvar = compute_historical_cvar(rets, confidence=0.90)
        self.assertIsNotNone(var)
        self.assertIsNotNone(cvar)
        self.assertLessEqual(cvar, var + 1e-9)

    def test_too_short_returns_none(self) -> None:
        rets = pd.Series([0.0] * 10)
        self.assertIsNone(compute_historical_var(rets))
        self.assertIsNone(compute_historical_cvar(rets))


class BetaTests(unittest.TestCase):
    def test_beta_one_when_identical(self) -> None:
        rng = np.random.default_rng(0)
        bench = pd.Series(rng.normal(0, 0.02, 60))
        beta, status = compute_beta(bench, bench)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(beta, 1.0, places=6)

    def test_beta_two_when_doubled(self) -> None:
        rng = np.random.default_rng(0)
        bench = pd.Series(rng.normal(0, 0.02, 60))
        port = bench * 2.0
        beta, status = compute_beta(port, bench)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(beta, 2.0, places=4)

    def test_no_benchmark_not_supported(self) -> None:
        rets = pd.Series(np.zeros(60))
        beta, status = compute_beta(rets, None)
        self.assertEqual(status, "not_supported")
        self.assertIsNone(beta)

    def test_short_history_not_supported(self) -> None:
        rets = pd.Series(np.zeros(20))
        bench = pd.Series(np.zeros(20))
        beta, status = compute_beta(rets, bench)
        self.assertEqual(status, "not_supported")


class EvaluateResearchRiskBundleTests(unittest.TestCase):
    def test_full_bundle_runs_without_error(self) -> None:
        panel = _three_stock_panel(days=200)
        result = evaluate_research_risk(ResearchRiskInputs(
            weights={"AAA": 0.5, "BBB": 0.3, "CCC": 0.2},
            returns_panel=panel,
            var_confidence=0.95,
            concentration_threshold_pct=40.0,
        ))
        self.assertGreater(result.daily_observation_count, 100)
        self.assertIn("rows", result.concentration)
        self.assertIsNotNone(result.volatility["annualized"])
        self.assertIsNotNone(result.drawdown["max_drawdown"])
        self.assertIsNotNone(result.historical_var)
        self.assertIsNotNone(result.historical_cvar)
        self.assertEqual(result.beta_status, "not_supported")  # no benchmark
        self.assertIs(result.assumptions.get("is_research_only"), True)
        self.assertIs(result.assumptions.get("trade_orders_emitted"), False)


if __name__ == "__main__":
    unittest.main()
