# -*- coding: utf-8 -*-
"""Research-grade backtest engine for the Quant Research Lab.

Independent from ``src/core/backtest_engine.py`` (which validates AI
historical buy/hold/sell calls). This package answers the different
question "does a factor-driven trading rule have edge?" via a
close-to-close simulation with explicit transaction costs and a
1-trading-day signal lag.

Module layout:
    - ``costs.py``    : ``CostModel`` + ``cost_for_turnover``
    - ``metrics.py``  : pure functions for Sharpe / Sortino / drawdown / IR / etc.
    - ``engine.py``   : ``run_backtest(inputs) -> BacktestResult``
"""

from src.quant_research.backtest.costs import (
    CostModel,
    DEFAULT_COMMISSION_BPS,
    DEFAULT_SLIPPAGE_BPS,
    MAX_BPS,
    cost_for_turnover,
)
from src.quant_research.backtest.engine import (
    BacktestDiagnostics,
    BacktestInputs,
    BacktestMetricsBundle,
    BacktestPositionSnapshot,
    BacktestResult,
    MAX_LOOKBACK_DAYS,
    MAX_STOCKS,
    VALID_REBALANCE,
    VALID_STRATEGIES,
    run_backtest,
)
from src.quant_research.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    information_ratio,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    turnover,
    win_rate,
)

__all__ = [
    # costs
    "CostModel",
    "DEFAULT_COMMISSION_BPS",
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_BPS",
    "cost_for_turnover",
    # engine
    "BacktestDiagnostics",
    "BacktestInputs",
    "BacktestMetricsBundle",
    "BacktestPositionSnapshot",
    "BacktestResult",
    "MAX_LOOKBACK_DAYS",
    "MAX_STOCKS",
    "VALID_REBALANCE",
    "VALID_STRATEGIES",
    "run_backtest",
    # metrics
    "TRADING_DAYS_PER_YEAR",
    "annualized_return",
    "annualized_volatility",
    "calmar_ratio",
    "information_ratio",
    "max_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
    "turnover",
    "win_rate",
]
