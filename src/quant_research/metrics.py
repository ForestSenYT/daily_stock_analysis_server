# -*- coding: utf-8 -*-
"""Metric registries for Quant Research Lab.

Phase 1 just declares the *names* the future evaluator/backtest engines
will compute — nothing is calculated here. Keeping the registry separate
lets the ``/capabilities`` endpoint advertise what's coming without
shipping the implementation.
"""

from __future__ import annotations

from typing import Final, Tuple


# Factor evaluation metrics (P2 will compute these).
SUPPORTED_FACTOR_METRICS: Final[Tuple[str, ...]] = (
    "coverage",
    "missing_rate",
    "ic",
    "rank_ic",
    "ic_mean",
    "ic_std",
    "icir",
    "quantile_returns",
    "long_short_spread",
    "turnover",
    "autocorrelation",
)

# Strategy backtest metrics (P3 will compute these).
SUPPORTED_BACKTEST_METRICS: Final[Tuple[str, ...]] = (
    "total_return",
    "annualized_return",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "calmar",
    "max_drawdown",
    "win_rate",
    "turnover",
    "cost_drag",
    "benchmark_return",
    "excess_return",
    "information_ratio",
)

# Portfolio risk metrics (P4 will compute these; reuses portions of
# the existing PortfolioRiskService where possible).
SUPPORTED_PORTFOLIO_RISK_METRICS: Final[Tuple[str, ...]] = (
    "concentration",
    "sector_concentration",
    "historical_var",
    "historical_cvar",
    "max_drawdown",
    "volatility",
    "beta",  # may surface as not_supported when benchmark data missing
)
