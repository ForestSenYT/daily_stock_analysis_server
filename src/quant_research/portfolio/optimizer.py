# -*- coding: utf-8 -*-
"""Portfolio optimizer — research-only target weight suggestions.

Five lightweight algorithms (no convex solver, no scipy.optimize) so
the base service stays import-free of heavy deps:

1. ``equal_weight``                — equal weight across all symbols.
2. ``inverse_volatility``          — wᵢ ∝ 1 / σᵢ, then normalise to 1.
3. ``max_sharpe_simplified``       — analytical 2-asset / closed-form
   approximation that picks weights proportional to (μ / σ²) within
   the long_only cone; not the full Markowitz tangency portfolio
   because we have no riskless rate input.
4. ``min_variance_simplified``     — diagonal-cov approximation:
   wᵢ ∝ 1 / σᵢ², which matches the analytical min-variance solution
   when the off-diagonal correlations are ignored. (Full inverse
   covariance would need a solver and a stable estimator.)
5. ``risk_budget_placeholder``     — declared but always returns
   ``not_supported`` for now. Phase 4+ may ship real risk parity.

All five algorithms share a generic constraint pipeline:
- ``long_only``       (default ``True``) → clamp negatives to 0.
- ``min_weight_per_symbol``           → drop names below the floor and
                                         renormalise.
- ``max_weight_per_symbol``           → cap then redistribute spillover
                                         pro-rata to non-capped names.
- ``cash_weight``                     → fix a fraction in cash; the
                                         remainder is allocated by the
                                         algorithm.
- ``max_turnover``                    → blend new weights with current
                                         weights so that L1 turnover
                                         doesn't exceed the cap.
- ``sector_exposure_limit``           → returns
                                         ``status: partial_coverage`` —
                                         we don't have a sector
                                         taxonomy in this repo yet.

The output is *target weights* — research suggestions only, never
trading orders. The caller interprets / records them as needed; this
module does not import ``PortfolioService``.

Out of scope for Phase 4:
- Black-Litterman views
- Constrained convex optimization (would need cvxpy)
- Per-symbol short bounds (long_only=False just clamps to ±max)
- Transaction-cost aware optimization
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VALID_OBJECTIVES = (
    "equal_weight",
    "inverse_volatility",
    "max_sharpe_simplified",
    "min_variance_simplified",
    "risk_budget_placeholder",
)

# Hard caps / defaults
TRADING_DAYS_PER_YEAR = 252
DEFAULT_MIN_WEIGHT_FLOOR = 0.0
DEFAULT_MAX_WEIGHT_CEILING = 1.0


# =====================================================================
# Inputs
# =====================================================================

@dataclass
class PortfolioOptimizerInputs:
    """Pure-research optimizer inputs.

    Either ``returns_panel`` is required (rows=date, cols=symbol).
    Constraints are all optional and applied in a deterministic order
    so two calls with identical inputs always produce identical output.
    """
    objective: str
    symbols: List[str]
    returns_panel: pd.DataFrame
    long_only: bool = True
    min_weight_per_symbol: float = 0.0
    max_weight_per_symbol: float = 1.0
    cash_weight: float = 0.0
    max_turnover: Optional[float] = None
    current_weights: Optional[Dict[str, float]] = None  # for max_turnover
    sector_exposure_limit: Optional[Dict[str, float]] = None  # not yet supported


# =====================================================================
# Output
# =====================================================================

@dataclass
class PortfolioOptimizerOutput:
    """Optimizer output.

    ``status`` is one of:
      - ``"ok"``                              — weights computed
      - ``"not_supported"``                   — feature requires deps not present (e.g. sector / risk budget)
      - ``"insufficient_data"``               — too few returns / symbols
      - ``"infeasible_constraints"``          — caller's bounds leave no valid solution
    """
    status: str
    objective: str
    weights: Dict[str, float]
    cash_weight: float
    expected_annual_return: Optional[float]
    expected_annual_volatility: Optional[float]
    diagnostics: List[str] = field(default_factory=list)
    assumptions: Dict[str, object] = field(default_factory=dict)


# =====================================================================
# Constraint pipeline (pure functions)
# =====================================================================

def _apply_long_only(weights: Dict[str, float], long_only: bool) -> Dict[str, float]:
    if not long_only:
        return weights
    return {s: max(0.0, w) for s, w in weights.items()}


def _normalise_to_sum(weights: Dict[str, float], target: float) -> Dict[str, float]:
    """Scale weights so the total equals ``target``. Zero-sum input
    → return as-is (caller will surface diagnostic)."""
    total = sum(weights.values())
    if abs(total) < 1e-12:
        return weights
    factor = target / total
    return {s: w * factor for s, w in weights.items()}


def _apply_min_weight_floor(
    weights: Dict[str, float],
    floor: float,
    target_sum: float,
) -> Dict[str, float]:
    """Drop names below ``floor`` then renormalise the remainder."""
    if floor <= 0:
        return weights
    kept = {s: w for s, w in weights.items() if abs(w) >= floor}
    if not kept:
        return {}
    return _normalise_to_sum(kept, target_sum)


def _apply_max_weight_ceiling(
    weights: Dict[str, float],
    ceiling: float,
    target_sum: float,
) -> Dict[str, float]:
    """Iteratively cap names at ``ceiling`` and redistribute spillover
    to non-capped names pro-rata. Converges in O(N) iterations."""
    if ceiling >= target_sum or ceiling <= 0:
        return weights
    out = dict(weights)
    for _ in range(len(out) + 2):  # bounded loop, 1 cap per iter at most
        capped = {s: w for s, w in out.items() if w > ceiling + 1e-12}
        if not capped:
            break
        # Pin capped at ceiling, redistribute spillover to uncapped
        spillover = sum(out[s] - ceiling for s in capped)
        uncapped = {s: w for s, w in out.items() if s not in capped}
        if not uncapped:
            # All names capped: the constraint is infeasible → uniform cap
            return {s: target_sum / len(out) for s in out}
        uncap_total = sum(uncapped.values())
        if uncap_total <= 0:
            return out
        for s in capped:
            out[s] = ceiling
        for s, w in uncapped.items():
            out[s] = w + spillover * (w / uncap_total)
    return out


def _apply_max_turnover(
    new_weights: Dict[str, float],
    current_weights: Optional[Dict[str, float]],
    max_turnover: Optional[float],
) -> Dict[str, float]:
    """Blend ``new_weights`` toward ``current_weights`` so that the L1
    distance ``½ Σ|new - current|`` does not exceed ``max_turnover``.

    Returns ``new_weights`` unchanged when no current_weights / no cap.
    """
    if max_turnover is None or current_weights is None:
        return new_weights
    if max_turnover <= 0:
        return dict(current_weights)
    all_keys = set(new_weights) | set(current_weights)
    new = {k: float(new_weights.get(k, 0.0)) for k in all_keys}
    cur = {k: float(current_weights.get(k, 0.0)) for k in all_keys}
    desired_diff = sum(abs(new[k] - cur[k]) for k in all_keys) / 2.0
    if desired_diff <= max_turnover:
        return new
    # Linear blend: w_t = α·new + (1-α)·cur, choose α so turnover cap is met
    alpha = max_turnover / desired_diff
    return {k: alpha * new[k] + (1 - alpha) * cur[k] for k in all_keys}


# =====================================================================
# Objectives
# =====================================================================

def _equal_weight(symbols: List[str]) -> Dict[str, float]:
    if not symbols:
        return {}
    w = 1.0 / len(symbols)
    return {s: w for s in symbols}


def _inverse_vol(returns_panel: pd.DataFrame) -> Dict[str, float]:
    """Inverse-volatility weights, normalised to sum 1. Symbols with
    zero or NaN vol are dropped."""
    vol = returns_panel.std(ddof=1)
    inv = 1.0 / vol.replace(0.0, np.nan)
    inv = inv.dropna()
    total = inv.sum()
    if total <= 0:
        return {}
    return {s: float(inv[s] / total) for s in inv.index}


def _max_sharpe_simplified(returns_panel: pd.DataFrame) -> Dict[str, float]:
    """Closed-form sketch: wᵢ ∝ μᵢ / σᵢ² with negatives clamped to 0
    (so it stays in long-only cone). Not the textbook tangency
    portfolio (which needs a riskless rate and a full covariance
    inverse) — labelled "simplified" deliberately."""
    mu = returns_panel.mean()
    var = returns_panel.var(ddof=1)
    score = mu / var.replace(0.0, np.nan)
    score = score.fillna(0.0)
    score = score.clip(lower=0.0)
    total = score.sum()
    if total <= 0:
        return _equal_weight(list(returns_panel.columns))
    return {s: float(score[s] / total) for s in score.index}


def _min_variance_simplified(returns_panel: pd.DataFrame) -> Dict[str, float]:
    """Diagonal-cov min-variance: wᵢ ∝ 1/σᵢ². Equivalent to inverse-vol
    weights but penalising vol more aggressively."""
    var = returns_panel.var(ddof=1)
    inv_var = 1.0 / var.replace(0.0, np.nan)
    inv_var = inv_var.dropna()
    total = inv_var.sum()
    if total <= 0:
        return {}
    return {s: float(inv_var[s] / total) for s in inv_var.index}


# =====================================================================
# Top-level
# =====================================================================

def optimize_portfolio(inputs: PortfolioOptimizerInputs) -> PortfolioOptimizerOutput:
    """End-to-end optimizer with constraint pipeline.

    Caller pre-validates input shapes (size limits, valid objective).
    """
    if inputs.objective not in VALID_OBJECTIVES:
        raise ValueError(f"Unknown objective: {inputs.objective!r}")

    diagnostics: List[str] = []
    assumptions: Dict[str, object] = {
        "is_research_only": True,
        "trade_orders_emitted": False,
        "long_only": bool(inputs.long_only),
        "min_weight_per_symbol": float(inputs.min_weight_per_symbol),
        "max_weight_per_symbol": float(inputs.max_weight_per_symbol),
        "cash_weight": float(inputs.cash_weight),
        "max_turnover": inputs.max_turnover,
        "trading_days_per_year": TRADING_DAYS_PER_YEAR,
        "covariance_method": "diagonal_only_phase4",
        "shorting_supported": False,
        "objective": inputs.objective,
    }

    # Sector limit is declared but not implemented this phase.
    if inputs.sector_exposure_limit:
        diagnostics.append(
            "sector_exposure_limit was supplied but no sector taxonomy "
            "is available in this build; constraint ignored "
            "(status: partial_coverage)."
        )
        assumptions["sector_constraint_status"] = "partial_coverage"

    # Risk-budget placeholder
    if inputs.objective == "risk_budget_placeholder":
        return PortfolioOptimizerOutput(
            status="not_supported",
            objective=inputs.objective,
            weights={},
            cash_weight=float(inputs.cash_weight),
            expected_annual_return=None,
            expected_annual_volatility=None,
            diagnostics=diagnostics + [
                "risk_budget_placeholder is declared but requires a "
                "risk-parity solver not shipped in Phase 4. Use "
                "inverse_volatility or min_variance_simplified instead."
            ],
            assumptions=assumptions,
        )

    # Validate the symbol pool intersects the returns panel.
    valid_symbols = [s for s in inputs.symbols if s in inputs.returns_panel.columns]
    missing = [s for s in inputs.symbols if s not in inputs.returns_panel.columns]
    if missing:
        diagnostics.append(
            f"{len(missing)} symbols missing from returns_panel — "
            f"dropped: {missing[:8]}"
            + ("…" if len(missing) > 8 else "")
        )

    target_invest_sum = max(0.0, 1.0 - float(inputs.cash_weight))
    if not valid_symbols or target_invest_sum <= 0:
        return PortfolioOptimizerOutput(
            status="insufficient_data" if not valid_symbols else "infeasible_constraints",
            objective=inputs.objective,
            weights={},
            cash_weight=float(inputs.cash_weight),
            expected_annual_return=None,
            expected_annual_volatility=None,
            diagnostics=diagnostics + [
                "No symbols intersected the returns panel; "
                "optimizer produced no weights."
                if not valid_symbols
                else "cash_weight ≥ 1 — no capital to allocate."
            ],
            assumptions=assumptions,
        )

    panel = inputs.returns_panel[valid_symbols].dropna(axis=0, how="all")
    if panel.empty or len(panel) < 2:
        return PortfolioOptimizerOutput(
            status="insufficient_data",
            objective=inputs.objective,
            weights={},
            cash_weight=float(inputs.cash_weight),
            expected_annual_return=None,
            expected_annual_volatility=None,
            diagnostics=diagnostics + [
                "Returns panel has < 2 rows for the supplied symbols."
            ],
            assumptions=assumptions,
        )

    # --- Algorithm dispatch ---
    if inputs.objective == "equal_weight":
        raw = _equal_weight(valid_symbols)
    elif inputs.objective == "inverse_volatility":
        raw = _inverse_vol(panel)
    elif inputs.objective == "max_sharpe_simplified":
        raw = _max_sharpe_simplified(panel)
    elif inputs.objective == "min_variance_simplified":
        raw = _min_variance_simplified(panel)
    else:  # pragma: no cover — covered by VALID_OBJECTIVES check above
        raise ValueError(f"Unhandled objective: {inputs.objective}")

    if not raw:
        return PortfolioOptimizerOutput(
            status="insufficient_data",
            objective=inputs.objective,
            weights={},
            cash_weight=float(inputs.cash_weight),
            expected_annual_return=None,
            expected_annual_volatility=None,
            diagnostics=diagnostics + [
                "Algorithm produced no usable weights "
                "(degenerate variance / zero-sum / NaN-only column)."
            ],
            assumptions=assumptions,
        )

    # --- Constraint pipeline (long-only → floor → ceiling → turnover) ---
    weights = _normalise_to_sum(raw, target_invest_sum)
    weights = _apply_long_only(weights, inputs.long_only)
    weights = _normalise_to_sum(weights, target_invest_sum)
    weights = _apply_min_weight_floor(weights, inputs.min_weight_per_symbol, target_invest_sum)
    if not weights:
        return PortfolioOptimizerOutput(
            status="infeasible_constraints",
            objective=inputs.objective,
            weights={},
            cash_weight=float(inputs.cash_weight),
            expected_annual_return=None,
            expected_annual_volatility=None,
            diagnostics=diagnostics + [
                "All names fell below min_weight_per_symbol; constraint infeasible."
            ],
            assumptions=assumptions,
        )
    weights = _apply_max_weight_ceiling(weights, inputs.max_weight_per_symbol, target_invest_sum)
    weights = _apply_max_turnover(weights, inputs.current_weights, inputs.max_turnover)

    # --- Expected return / vol on the realised panel (research only) ---
    daily_mean = panel.mean()
    aligned = pd.Series({s: weights.get(s, 0.0) for s in panel.columns})
    portfolio_daily = (panel * aligned).sum(axis=1)
    expected_annual_return = float(portfolio_daily.mean() * TRADING_DAYS_PER_YEAR)
    if len(portfolio_daily) >= 2:
        expected_annual_volatility = float(portfolio_daily.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        expected_annual_volatility = None

    # Drop near-zeros for cleaner output
    weights_clean = {s: round(float(w), 6) for s, w in weights.items() if abs(w) > 1e-6}

    return PortfolioOptimizerOutput(
        status="ok",
        objective=inputs.objective,
        weights=weights_clean,
        cash_weight=float(inputs.cash_weight),
        expected_annual_return=expected_annual_return,
        expected_annual_volatility=expected_annual_volatility,
        diagnostics=diagnostics,
        assumptions=assumptions,
    )
