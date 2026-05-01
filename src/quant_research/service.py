# -*- coding: utf-8 -*-
"""Service-layer entry point for the Quant Research Lab.

Phase 1 only exposes ``status()`` and ``capabilities()`` — both are pure
functions of (feature flag, build version), no DB access required.
Later phases add ``evaluate_factor``, ``run_backtest``, ``optimize_portfolio``
on this same class.

Design notes:
- The service constructor takes its dependencies (config, repository)
  as arguments, so endpoint code can pass the live singleton in production
  while tests inject fakes.
- All public methods MUST be safe to call when the feature flag is off —
  they return a structured ``not_enabled`` response rather than raising.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.config import Config, get_config
from src.quant_research.errors import QuantResearchDisabledError  # noqa: F401  (re-exported for callers)
from src.quant_research.repositories import QuantResearchRepository
from src.quant_research.schemas import (
    QuantResearchCapabilities,
    QuantResearchCapability,
    QuantResearchStatus,
)

logger = logging.getLogger(__name__)

# Roadmap phase the current build implements; updated by later phases.
_CURRENT_PHASE = "phase-1-scaffold"


class QuantResearchService:
    """Read/Write entry point for the Quant Research Lab.

    Construct it with ``QuantResearchService()`` to use live singletons,
    or inject test doubles via the kwargs.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        repository: Optional[QuantResearchRepository] = None,
    ) -> None:
        self._config = config or get_config()
        self._repository = repository or QuantResearchRepository()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Feature flag value. Re-read live each call so a runtime config
        save (via ``/api/v1/system/config``) takes effect without a
        restart."""
        return bool(getattr(self._config, "quant_research_enabled", False))

    def status(self) -> QuantResearchStatus:
        """Lightweight on/off snapshot for the WebUI status badge."""
        if not self.enabled:
            return QuantResearchStatus(
                enabled=False,
                status="not_enabled",
                message=(
                    "Quant Research Lab is disabled. "
                    "Set QUANT_RESEARCH_ENABLED=true to enable it."
                ),
                phase=_CURRENT_PHASE,
            )
        return QuantResearchStatus(
            enabled=True,
            status="ready",
            message=(
                "Quant Research Lab feature flag is on, but only the "
                "Phase 1 scaffold is live in this build. Factor "
                "evaluation, backtest and portfolio optimization will "
                "be added in subsequent phases."
            ),
            phase=_CURRENT_PHASE,
        )

    def capabilities(self) -> QuantResearchCapabilities:
        """Inventory of Lab capabilities + which phase enables each.

        Listed unconditionally so the FE can render placeholder cards
        even before each phase is implemented.
        """
        capabilities = [
            QuantResearchCapability(
                name="factor_evaluation",
                title="Factor Evaluation",
                available=False,
                phase="phase-2",
                description=(
                    "Evaluate built-in or AI-generated factors on a stock "
                    "pool: coverage, IC/RankIC, ICIR, quantile returns, "
                    "long-short spread, factor turnover."
                ),
                endpoints=[
                    "GET  /api/v1/quant/factors",
                    "POST /api/v1/quant/factors/evaluate",
                ],
                requires_optional_deps=[],
            ),
            QuantResearchCapability(
                name="strategy_backtest",
                title="Strategy Backtest",
                available=False,
                phase="phase-3",
                description=(
                    "Research-grade strategy backtest with explicit "
                    "lookahead-bias guard. Supports factor top-k long-only, "
                    "quantile long-short (simulated), benchmark compare. "
                    "Distinct from /api/v1/backtest/* (which is the "
                    "after-the-fact AI decision validator)."
                ),
                endpoints=[
                    "POST /api/v1/quant/backtests/run",
                    "GET  /api/v1/quant/backtests/{run_id}",
                ],
                requires_optional_deps=["vectorbt (optional)"],
            ),
            QuantResearchCapability(
                name="portfolio_optimization",
                title="Portfolio Optimization & Risk Research",
                available=False,
                phase="phase-4",
                description=(
                    "Suggest target weights via equal-weight, "
                    "inverse-volatility, simplified max-sharpe / min-variance. "
                    "Reuses the existing PortfolioRiskService for "
                    "concentration and drawdown computations."
                ),
                endpoints=[
                    "POST /api/v1/quant/portfolio/optimize",
                    "POST /api/v1/quant/risk/evaluate",
                ],
                requires_optional_deps=["riskfolio-lib (optional)", "PyPortfolioOpt (optional)"],
            ),
            QuantResearchCapability(
                name="ai_factor_generation",
                title="AI FactorSpec Generation",
                available=False,
                phase="phase-5",
                description=(
                    "Translate a natural-language hypothesis into a "
                    "FactorSpec JSON, validated by safe_expression before "
                    "the evaluator runs it. AI never emits or executes "
                    "Python code directly."
                ),
                endpoints=[
                    "POST /api/v1/quant/factors/generate",
                ],
                requires_optional_deps=[],
            ),
            QuantResearchCapability(
                name="agent_integration",
                title="Existing Agent Integration",
                available=False,
                phase="phase-6",
                description=(
                    "Plugs Quant Research Lab into the existing Agent "
                    "ToolRegistry as opt-in tools. Default skill set is "
                    "unchanged — users must explicitly select the "
                    "`quant_research` skill in /api/v1/agent/chat."
                ),
                endpoints=[
                    "GET  /api/v1/agent/skills (existing)",
                    "POST /api/v1/agent/chat   (existing)",
                ],
                requires_optional_deps=[],
            ),
        ]
        return QuantResearchCapabilities(
            enabled=self.enabled,
            capabilities=capabilities,
        )
