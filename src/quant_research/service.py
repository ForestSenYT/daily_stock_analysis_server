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

from datetime import date

from src.config import Config, get_config
from src.quant_research.errors import (  # noqa: F401  (re-exported for callers)
    QuantResearchDisabledError,
    QuantResearchValidationError,
)
from src.quant_research.repositories import QuantResearchRepository
from src.quant_research.schemas import (
    FactorCoverageReport,
    FactorEvaluationRequest,
    FactorEvaluationResult,
    FactorInputSchema,
    FactorMetricSummary,
    FactorRegistryResponse,
    FactorSpec,
    QuantResearchCapabilities,
    QuantResearchCapability,
    QuantResearchStatus,
)

logger = logging.getLogger(__name__)

# Roadmap phase the current build implements; updated by later phases.
_CURRENT_PHASE = "phase-2-factor-lab"


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
                "Quant Research Lab is live. Phase 2 — Factor Lab — is "
                "operational: GET /api/v1/quant/factors lists built-in "
                "factors, POST /api/v1/quant/factors/evaluate runs IC / "
                "RankIC / quantile-return analysis. Strategy backtest, "
                "portfolio optimization, AI factor generation, and Agent "
                "integration are still pending in later phases."
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
                available=True,
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

    # ------------------------------------------------------------------
    # Phase 2 — Factor Lab
    # ------------------------------------------------------------------

    def list_factors(self) -> FactorRegistryResponse:
        """Inventory of built-in factors available for evaluation.

        Returns an ``enabled=False`` empty list when the master flag is
        off so the WebUI can render the page even with the lab disabled.
        """
        from src.quant_research.factors.registry import list_builtin_factors

        if not self.enabled:
            return FactorRegistryResponse(enabled=False, builtins=[])
        builtins = [
            FactorInputSchema(
                id=entry.id,
                name=entry.name,
                description=entry.description,
                expected_direction=entry.expected_direction,
                lookback_days=entry.lookback_days,
            )
            for entry in list_builtin_factors()
        ]
        return FactorRegistryResponse(enabled=True, builtins=builtins)

    def evaluate_factor(self, request: FactorEvaluationRequest) -> FactorEvaluationResult:
        """Run cross-sectional evaluation on the requested stock pool.

        Validation responsibility: this method enforces field combinations
        (mutual-exclusivity of builtin_id / expression, parseable dates,
        list lengths) and delegates compute to
        ``factors.evaluator.evaluate_factor``.

        Raises ``QuantResearchDisabledError`` when the master flag is off
        — the endpoint translates that into a structured 503-style payload.
        """
        if not self.enabled:
            raise QuantResearchDisabledError(
                "Quant Research Lab is disabled. "
                "Set QUANT_RESEARCH_ENABLED=true to enable it."
            )

        from src.quant_research.factors.evaluator import (
            FactorEvalInputs,
            MAX_FORWARD_WINDOW,
            MAX_LOOKBACK_DAYS,
            MAX_STOCKS,
            evaluate_factor as _run,
        )

        spec = request.factor
        if bool(spec.builtin_id) == bool(spec.expression):
            raise QuantResearchValidationError(
                "Provide exactly one of `factor.builtin_id` or `factor.expression`.",
                field="factor",
            )

        # Date parsing — Pydantic's ``date`` would also work but we
        # keep the request schema as ``str`` so OpenAPI is unambiguous.
        try:
            start = date.fromisoformat(request.start_date)
            end = date.fromisoformat(request.end_date)
        except ValueError as exc:
            raise QuantResearchValidationError(
                f"Invalid date format (use YYYY-MM-DD): {exc}",
                field="start_date/end_date",
            )
        if start > end:
            raise QuantResearchValidationError(
                "start_date must be on or before end_date.",
                field="start_date",
            )
        if (end - start).days > MAX_LOOKBACK_DAYS:
            raise QuantResearchValidationError(
                f"Window too large: max {MAX_LOOKBACK_DAYS} calendar days.",
                field="start_date/end_date",
            )
        if request.forward_window > MAX_FORWARD_WINDOW:
            raise QuantResearchValidationError(
                f"forward_window must be ≤ {MAX_FORWARD_WINDOW}.",
                field="forward_window",
            )
        if len(request.stocks) > MAX_STOCKS:
            raise QuantResearchValidationError(
                f"Too many stocks: max {MAX_STOCKS}.",
                field="stocks",
            )

        try:
            outputs = _run(
                FactorEvalInputs(
                    builtin_id=spec.builtin_id,
                    expression=spec.expression,
                    factor_name=spec.name,
                    stocks=list(request.stocks),
                    start_date=start,
                    end_date=end,
                    forward_window=request.forward_window,
                    quantile_count=request.quantile_count,
                )
            )
        except ValueError as exc:
            # Evaluator raises ValueError on bad factor inputs (unknown
            # builtin id, mutually-exclusive flags). Surface as 400.
            raise QuantResearchValidationError(str(exc), field="factor") from exc

        # Map evaluator output → API schema.
        return FactorEvaluationResult(
            enabled=True,
            run_id=outputs.run_id,
            factor=FactorSpec(
                name=outputs.factor_name,
                builtin_id=outputs.factor_id,
                expression=outputs.expression,
            ),
            factor_kind=outputs.factor_kind,
            stock_pool=outputs.stock_pool,
            start_date=outputs.start_date.isoformat(),
            end_date=outputs.end_date.isoformat(),
            forward_window=outputs.forward_window,
            quantile_count=outputs.quantile_count,
            coverage=FactorCoverageReport(**outputs.coverage),  # type: ignore[arg-type]
            metrics=FactorMetricSummary(**outputs.metrics),  # type: ignore[arg-type]
            diagnostics=outputs.diagnostics,
            assumptions=outputs.assumptions,
        )
