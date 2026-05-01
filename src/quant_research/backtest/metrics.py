# -*- coding: utf-8 -*-
"""Performance metric primitives for the Research Backtest engine.

Each function takes a daily-frequency Series (NAV or returns) and
returns a deterministic float (or None when the input is too short for
the metric to be meaningful). No side effects, no logger, no I/O — keep
this module testable as pure math.

Conventions:
- All "annualized" metrics use 252 trading days/year (US market default).
  A future config knob can override this; for now it's hard-coded so the
  numbers remain comparable across runs.
- Returns are arithmetic daily returns ``r_t = nav_t / nav_{t-1} - 1``,
  not log returns. This matches what most equity research papers report.
- "Drawdown" uses peak-to-trough on the *NAV* curve, not on returns.
- All callers must pre-clean NaN — these helpers fail-fast on NaN inputs
  rather than silently returning weird numbers.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

# Hard-coded; expose later if/when we support intraday or non-equity data.
TRADING_DAYS_PER_YEAR = 252


def _validate_series(series: pd.Series, *, label: str) -> pd.Series:
    """Cheap input check used by every metric. Returns a clean float series."""
    if not isinstance(series, pd.Series):
        raise TypeError(f"{label} must be pandas.Series")
    if series.isna().any():
        raise ValueError(f"{label} must not contain NaN (caller is responsible for cleaning)")
    return series.astype(float)


# ---------------------------------------------------------------------
# NAV-derived
# ---------------------------------------------------------------------

def total_return(nav: pd.Series) -> float:
    """Cumulative return over the full NAV series."""
    nav = _validate_series(nav, label="nav")
    if len(nav) < 2:
        return 0.0
    return float(nav.iloc[-1] / nav.iloc[0] - 1.0)


def annualized_return(nav: pd.Series) -> Optional[float]:
    """Geometric annualized return derived from NAV. Needs ≥ 2 points."""
    nav = _validate_series(nav, label="nav")
    if len(nav) < 2:
        return None
    tr = float(nav.iloc[-1] / nav.iloc[0])
    if tr <= 0:
        # Negative NAV makes geometric return ill-defined; report None
        # rather than raise so the rest of the report still ships.
        return None
    n_periods = len(nav) - 1
    return float(tr ** (TRADING_DAYS_PER_YEAR / n_periods) - 1.0)


def annualized_volatility(daily_returns: pd.Series) -> Optional[float]:
    """Sample stdev of daily returns × √252."""
    daily_returns = _validate_series(daily_returns, label="daily_returns")
    if len(daily_returns) < 2:
        return None
    return float(daily_returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def sharpe_ratio(daily_returns: pd.Series, risk_free_daily: float = 0.0) -> Optional[float]:
    """Annualized Sharpe = (mean(daily_excess) / std(daily_excess)) × √252.

    ``risk_free_daily`` defaults to 0; pass a non-zero value if you want
    an excess-return Sharpe. Most research reports 0-rf Sharpe, which is
    fine for relative comparison between strategies.
    """
    daily_returns = _validate_series(daily_returns, label="daily_returns")
    if len(daily_returns) < 2:
        return None
    excess = daily_returns - risk_free_daily
    std = excess.std(ddof=1)
    if std == 0:
        return None
    return float(excess.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino_ratio(daily_returns: pd.Series, risk_free_daily: float = 0.0) -> Optional[float]:
    """Like Sharpe but only penalizing downside volatility.

    Returns None when there are no down-days (downside std = 0); that's
    a feature, not a bug — caller can render "—" instead of inf.
    """
    daily_returns = _validate_series(daily_returns, label="daily_returns")
    if len(daily_returns) < 2:
        return None
    excess = daily_returns - risk_free_daily
    downside = excess[excess < 0]
    if len(downside) < 2:
        return None
    dstd = downside.std(ddof=1)
    if dstd == 0:
        return None
    return float(excess.mean() / dstd * math.sqrt(TRADING_DAYS_PER_YEAR))


def max_drawdown(nav: pd.Series) -> Optional[float]:
    """Peak-to-trough NAV drawdown, returned as a negative fraction.

    Example: ``-0.25`` means a 25% drawdown.
    """
    nav = _validate_series(nav, label="nav")
    if len(nav) < 2:
        return None
    running_peak = nav.cummax()
    dd = nav / running_peak - 1.0
    return float(dd.min())


def calmar_ratio(nav: pd.Series, daily_returns: pd.Series) -> Optional[float]:
    """Annualized return / |max drawdown|. Uses the same NAV for both legs."""
    ar = annualized_return(nav)
    mdd = max_drawdown(nav)
    if ar is None or mdd is None or mdd == 0:
        return None
    return float(ar / abs(mdd))


def win_rate(daily_returns: pd.Series) -> Optional[float]:
    """% of trading days with strictly-positive return."""
    daily_returns = _validate_series(daily_returns, label="daily_returns")
    if daily_returns.empty:
        return None
    return float((daily_returns > 0).mean())


# ---------------------------------------------------------------------
# Trading-mechanic-derived
# ---------------------------------------------------------------------

def turnover(weights_panel: pd.DataFrame) -> Optional[float]:
    """Average daily turnover = ½ × Σ|w_t − w_{t−1}|, averaged over time.

    Halving accounts for the fact that selling X% of one stock and buying
    X% of another counts as X% turnover, not 2X% (one-sided).
    """
    if not isinstance(weights_panel, pd.DataFrame):
        raise TypeError("weights_panel must be a DataFrame")
    if len(weights_panel) < 2:
        return None
    diffs = weights_panel.fillna(0.0).diff().abs().sum(axis=1) / 2.0
    diffs = diffs.iloc[1:]  # first row is undefined
    if diffs.empty:
        return None
    return float(diffs.mean())


def information_ratio(
    strategy_daily: pd.Series,
    benchmark_daily: pd.Series,
) -> Optional[float]:
    """Mean(active return) / std(active return) × √252 — measures
    consistency of strategy outperformance vs a benchmark.

    Returns None when the benchmark has too few aligned days or the
    active-return std is zero.
    """
    strategy_daily = _validate_series(strategy_daily, label="strategy_daily")
    benchmark_daily = _validate_series(benchmark_daily, label="benchmark_daily")
    aligned = pd.concat([strategy_daily, benchmark_daily], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        return None
    active = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    std = active.std(ddof=1)
    if std == 0:
        return None
    return float(active.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))
