# -*- coding: utf-8 -*-
"""Quant Research Lab — portfolio sub-package.

Phase 4: research-only optimizer + risk evaluator. No live trading.

Module layout:
    - ``optimizer.py`` : 5 lightweight target-weight algorithms +
                         constraint pipeline (long_only / floor /
                         ceiling / cash / turnover)
    - ``risk.py``      : standalone returns-matrix risk metrics
                         (concentration / VaR / CVaR / drawdown /
                         volatility / beta)

The ``current-risk`` endpoint (live portfolio adapter) does NOT live
here; it stays in ``service.py`` because it delegates to the existing
``PortfolioRiskService`` rather than implementing fresh math.
"""

from src.quant_research.portfolio.optimizer import (
    PortfolioOptimizerInputs,
    PortfolioOptimizerOutput,
    VALID_OBJECTIVES,
    optimize_portfolio,
)
from src.quant_research.portfolio.risk import (
    DEFAULT_VAR_CONFIDENCE,
    ResearchRiskInputs,
    ResearchRiskResult,
    compute_beta,
    compute_concentration,
    compute_drawdown,
    compute_historical_cvar,
    compute_historical_var,
    compute_volatility,
    evaluate_research_risk,
)

__all__ = [
    "PortfolioOptimizerInputs",
    "PortfolioOptimizerOutput",
    "VALID_OBJECTIVES",
    "optimize_portfolio",
    "DEFAULT_VAR_CONFIDENCE",
    "ResearchRiskInputs",
    "ResearchRiskResult",
    "compute_beta",
    "compute_concentration",
    "compute_drawdown",
    "compute_historical_cvar",
    "compute_historical_var",
    "compute_volatility",
    "evaluate_research_risk",
]
