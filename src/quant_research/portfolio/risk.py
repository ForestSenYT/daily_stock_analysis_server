# -*- coding: utf-8 -*-
"""Portfolio-level research risk metrics.

Two distinct surfaces:

1. **Standalone research risk** — given a *hypothetical* set of weights
   plus a returns matrix, compute concentration / VaR / CVaR / drawdown /
   volatility / beta. This path does NOT touch the live
   ``portfolio_trades`` table — it answers "what would the risk profile
   look like if I held *these* weights over *this* historical window?"
   This is what ``POST /api/v1/quant/risk/evaluate`` consumes.

2. **Live portfolio research view** — a thin adapter over the existing
   ``PortfolioRiskService`` so the quant module can render the current
   account in the same dashboard without re-implementing concentration
   math. This is what ``GET /api/v1/quant/portfolio/current-risk``
   consumes (Phase 4 just delegates to ``PortfolioRiskService``).

Why a fresh module rather than extending the existing service?
- Keeps research / live dashboards on different APIs (so a future
  research-only feature flag can disable the research path without
  affecting live risk monitoring).
- Avoids coupling the heavy ``PortfolioService`` (FX conversion, trade
  events, account replay) to research code that only needs a returns
  matrix.

Out of scope for Phase 4:
- Running covariance shrinkage (Ledoit-Wolf etc.) — pure sample cov
  for now.
- Conditional VaR with historical bootstrap / Monte Carlo — only the
  empirical-quantile variant is implemented.
- Sector / industry concentration — needs a sector taxonomy this repo
  doesn't ship; we report ``not_supported`` when requested.
- Beta requires a benchmark returns series; missing → ``not_supported``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
DEFAULT_VAR_CONFIDENCE = 0.95


# =====================================================================
# Inputs
# =====================================================================

@dataclass
class ResearchRiskInputs:
    """Pure-research risk inputs — does not depend on live portfolio."""
    weights: Dict[str, float]
    returns_panel: pd.DataFrame  # rows=date, cols=symbol
    benchmark_returns: Optional[pd.Series] = None  # daily, indexed by date
    var_confidence: float = DEFAULT_VAR_CONFIDENCE
    concentration_threshold_pct: float = 35.0


# =====================================================================
# Helpers
# =====================================================================

def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """Drop symbols with zero weight; preserve sign for short legs."""
    return {sym: float(w) for sym, w in weights.items() if abs(float(w)) > 1e-12}


def _portfolio_daily_returns(
    weights: Dict[str, float],
    returns_panel: pd.DataFrame,
) -> pd.Series:
    """Σ wᵢ × rᵢ_t for each date t. Missing returns → 0 contribution."""
    if returns_panel.empty or not weights:
        return pd.Series(dtype=float)
    aligned_cols = [s for s in weights if s in returns_panel.columns]
    if not aligned_cols:
        return pd.Series(dtype=float)
    sub = returns_panel[aligned_cols].fillna(0.0)
    w_arr = np.array([float(weights[s]) for s in aligned_cols], dtype=float)
    return pd.Series(sub.to_numpy() @ w_arr, index=sub.index, dtype=float)


# =====================================================================
# Concentration
# =====================================================================

def compute_concentration(
    weights: Dict[str, float],
    threshold_pct: float = 35.0,
) -> Dict[str, object]:
    """Single-name concentration on signed weights.

    Reports each name's weight, the alert flag at ``threshold_pct``, and
    the Herfindahl-Hirschman index of |w|² (a 0..10000 scale; HHI ≥ 2500
    is conventionally "highly concentrated").
    """
    weights = _normalize_weights(weights)
    if not weights:
        return {
            "total_gross": 0.0,
            "top_weight_pct": 0.0,
            "alert": False,
            "rows": [],
            "hhi": 0.0,
            "threshold_pct": threshold_pct,
        }
    gross = sum(abs(w) for w in weights.values())
    rows = sorted(
        (
            {
                "symbol": sym,
                "weight": round(float(w), 6),
                "weight_pct": round(float(w) * 100.0, 4),
                "abs_weight_pct": round(abs(float(w)) * 100.0, 4),
                "is_alert": bool(abs(float(w)) * 100.0 >= threshold_pct),
            }
            for sym, w in weights.items()
        ),
        key=lambda r: r["abs_weight_pct"],
        reverse=True,
    )
    top_weight_pct = rows[0]["abs_weight_pct"] if rows else 0.0
    hhi = sum((abs(float(w)) * 100.0) ** 2 for w in weights.values())
    return {
        "total_gross": round(float(gross), 6),
        "top_weight_pct": round(float(top_weight_pct), 4),
        "alert": bool(top_weight_pct >= threshold_pct),
        "rows": rows,
        "hhi": round(float(hhi), 4),
        "threshold_pct": threshold_pct,
    }


# =====================================================================
# Volatility / Drawdown
# =====================================================================

def compute_volatility(
    portfolio_daily: pd.Series,
) -> Dict[str, Optional[float]]:
    """Daily and annualized stdev of the portfolio's daily returns."""
    if portfolio_daily.empty or len(portfolio_daily) < 2:
        return {"daily": None, "annualized": None}
    daily = float(portfolio_daily.std(ddof=1))
    return {
        "daily": daily,
        "annualized": daily * math.sqrt(TRADING_DAYS_PER_YEAR),
    }


def compute_drawdown(
    portfolio_daily: pd.Series,
) -> Dict[str, Optional[float]]:
    """Peak-to-trough drawdown computed from cumulative returns
    (assumes initial NAV = 1.0)."""
    if portfolio_daily.empty or len(portfolio_daily) < 2:
        return {"max_drawdown": None, "current_drawdown": None}
    nav = (1.0 + portfolio_daily.fillna(0.0)).cumprod()
    running_peak = nav.cummax()
    dd = nav / running_peak - 1.0
    return {
        "max_drawdown": float(dd.min()),
        "current_drawdown": float(dd.iloc[-1]),
    }


# =====================================================================
# VaR / CVaR — historical empirical
# =====================================================================

def compute_historical_var(
    portfolio_daily: pd.Series,
    confidence: float = DEFAULT_VAR_CONFIDENCE,
) -> Optional[float]:
    """Historical 1-day VaR at ``confidence`` (e.g., 0.95).

    Returned as a NEGATIVE number representing the expected loss
    threshold: VaR(0.95) = -0.025 means "5% of days saw losses worse
    than 2.5%". Returns ``None`` for too-short series.
    """
    if portfolio_daily.empty or len(portfolio_daily) < 20:
        return None
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be in (0, 1)")
    quantile = 1.0 - confidence
    return float(np.quantile(portfolio_daily.dropna(), quantile))


def compute_historical_cvar(
    portfolio_daily: pd.Series,
    confidence: float = DEFAULT_VAR_CONFIDENCE,
) -> Optional[float]:
    """Historical 1-day CVaR (Expected Shortfall) — average of returns
    that fall below the VaR threshold. Same negative-number convention.
    """
    var = compute_historical_var(portfolio_daily, confidence)
    if var is None:
        return None
    losses = portfolio_daily.dropna()
    tail = losses[losses <= var]
    if tail.empty:
        return float(var)  # fallback to VaR if no observations beneath
    return float(tail.mean())


# =====================================================================
# Beta
# =====================================================================

def compute_beta(
    portfolio_daily: pd.Series,
    benchmark_daily: Optional[pd.Series],
) -> Tuple[Optional[float], str]:
    """Return ``(beta, status)``:

    - ``status="ok"`` and ``beta=float`` when both series align with ≥30
      paired observations and benchmark variance is non-zero.
    - ``status="not_supported"`` and ``beta=None`` otherwise.

    "not_supported" is intentional — beta requires a benchmark we may
    not have, so the API explicitly tells the caller "this metric
    couldn't be computed here", rather than producing a meaningless 0.
    """
    if portfolio_daily.empty or benchmark_daily is None or benchmark_daily.empty:
        return None, "not_supported"
    aligned = pd.concat(
        [portfolio_daily.rename("p"), benchmark_daily.rename("b")],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 30:
        return None, "not_supported"
    var_b = float(aligned["b"].var(ddof=1))
    if var_b <= 0:
        return None, "not_supported"
    cov = float(aligned[["p", "b"]].cov().iloc[0, 1])
    return cov / var_b, "ok"


# =====================================================================
# Top-level: pure-research risk evaluation
# =====================================================================

@dataclass
class ResearchRiskResult:
    """Standalone result — does not reference live portfolio state."""
    weights: Dict[str, float]
    daily_observation_count: int
    concentration: Dict[str, object]
    volatility: Dict[str, Optional[float]]
    drawdown: Dict[str, Optional[float]]
    var_confidence: float
    historical_var: Optional[float]
    historical_cvar: Optional[float]
    beta: Optional[float]
    beta_status: str
    sector_concentration_status: str = "not_supported"  # Phase 4: no sector data
    diagnostics: List[str] = field(default_factory=list)
    assumptions: Dict[str, object] = field(default_factory=dict)


def evaluate_research_risk(inputs: ResearchRiskInputs) -> ResearchRiskResult:
    """Run all metrics and bundle into a single result.

    Caller pre-validates input limits (this function trusts them).
    Diagnostics list non-fatal issues — caller should surface them in
    the API response so the user knows e.g. that beta wasn't computed.
    """
    weights = _normalize_weights(inputs.weights)
    diagnostics: List[str] = []

    portfolio_daily = _portfolio_daily_returns(weights, inputs.returns_panel)
    if portfolio_daily.empty:
        diagnostics.append(
            "No daily returns could be paired with the supplied weights; "
            "all risk metrics are unavailable."
        )

    concentration = compute_concentration(
        weights, inputs.concentration_threshold_pct,
    )
    vol = compute_volatility(portfolio_daily)
    dd = compute_drawdown(portfolio_daily)
    var = compute_historical_var(portfolio_daily, inputs.var_confidence)
    cvar = compute_historical_cvar(portfolio_daily, inputs.var_confidence)
    beta, beta_status = compute_beta(portfolio_daily, inputs.benchmark_returns)

    if var is None:
        diagnostics.append(
            "Historical VaR / CVaR need ≥ 20 daily observations; "
            f"got {len(portfolio_daily)}."
        )

    return ResearchRiskResult(
        weights=weights,
        daily_observation_count=int(len(portfolio_daily)),
        concentration=concentration,
        volatility=vol,
        drawdown=dd,
        var_confidence=float(inputs.var_confidence),
        historical_var=var,
        historical_cvar=cvar,
        beta=beta,
        beta_status=beta_status,
        sector_concentration_status="not_supported",
        diagnostics=diagnostics,
        assumptions={
            "trading_days_per_year": TRADING_DAYS_PER_YEAR,
            "var_method": "historical_empirical_quantile",
            "cvar_method": "historical_empirical_tail_mean",
            "beta_method": "ols_simple_regression",
            "min_var_observations": 20,
            "min_beta_observations": 30,
            "is_research_only": True,
            "trade_orders_emitted": False,
        },
    )
