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
_CURRENT_PHASE = "phase-4-portfolio-lab"


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
                "Quant Research Lab is live. Phase 2 (Factor Lab), "
                "Phase 3 (Research Backtest Lab), and Phase 4 "
                "(Portfolio Optimizer + Risk Research) are operational. "
                "Phase 4 endpoints: POST /api/v1/quant/portfolio/optimize "
                "suggests target weights from a returns window with "
                "long-only / floor / ceiling / cash / turnover constraints; "
                "POST /api/v1/quant/risk/evaluate reports concentration / "
                "VaR / CVaR / drawdown / volatility / (optional) beta on "
                "hypothetical weights; GET /api/v1/quant/portfolio/"
                "current-risk wraps the live PortfolioRiskService. "
                "AI factor generation and Agent integration are still "
                "pending in later phases."
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
                available=True,
                phase="phase-3",
                description=(
                    "Research-grade strategy backtest with structural "
                    "1-day signal lag (lookahead-bias guard). Supports "
                    "factor top-k long-only, quantile long-short "
                    "(simulated, no real shorting), equal-weight "
                    "baseline, optional benchmark compare. Distinct "
                    "from /api/v1/backtest/* (which is the "
                    "after-the-fact AI decision validator)."
                ),
                endpoints=[
                    "POST /api/v1/quant/backtests/run",
                    "GET  /api/v1/quant/backtests/{run_id}",
                ],
                requires_optional_deps=[],
            ),
            QuantResearchCapability(
                name="portfolio_optimization",
                title="Portfolio Optimization & Risk Research",
                available=True,
                phase="phase-4",
                description=(
                    "Five lightweight optimizers (equal_weight, "
                    "inverse_volatility, max_sharpe_simplified, "
                    "min_variance_simplified, risk_budget_placeholder) "
                    "with constraint pipeline (long_only / weight floor "
                    "/ ceiling / cash / turnover). Standalone research "
                    "risk on hypothetical weights: concentration, VaR, "
                    "CVaR, drawdown, volatility, beta. The "
                    "current-risk endpoint delegates to the existing "
                    "PortfolioRiskService for live-portfolio dashboards. "
                    "All output is research-only — no orders are emitted."
                ),
                endpoints=[
                    "POST /api/v1/quant/portfolio/optimize",
                    "POST /api/v1/quant/risk/evaluate",
                    "GET  /api/v1/quant/portfolio/current-risk",
                ],
                requires_optional_deps=[],
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

    # =================================================================
    # Phase 3 — Research Backtest
    # =================================================================
    #
    # Persistence intentionally omitted in P3: results live in a
    # bounded in-memory cache on this service class so
    # ``GET /api/v1/quant/backtests/{run_id}`` works for the rest of the
    # session. With Cloud Run max-instances=1 this is "good enough" —
    # Phase 4+ may add a ``quant_backtest_results`` table if
    # cross-instance retention matters.

    _backtest_cache: Optional[object] = None  # type: ignore[assignment]
    _BACKTEST_CACHE_MAX = 32

    def _cache(self):
        from collections import OrderedDict
        cls = type(self)
        if cls._backtest_cache is None:
            cls._backtest_cache = OrderedDict()
        return cls._backtest_cache

    def run_backtest(self, request):
        """Run a research backtest. ``request`` is a
        ``ResearchBacktestRequest`` Pydantic model.

        Endpoint catches ``QuantResearchValidationError`` → 400 and
        ``QuantResearchDisabledError`` → 503; any unexpected exception
        bubbles up and the endpoint returns a generic 500 (this matches
        the pattern used by ``evaluate_factor``).
        """
        if not self.enabled:
            raise QuantResearchDisabledError(
                "Quant Research Lab is disabled. "
                "Set QUANT_RESEARCH_ENABLED=true to enable it."
            )

        from src.quant_research.backtest import (
            BacktestInputs,
            CostModel,
            MAX_LOOKBACK_DAYS as _BT_MAX_DAYS,
            MAX_STOCKS as _BT_MAX_STOCKS,
            VALID_REBALANCE,
            VALID_STRATEGIES,
            run_backtest as _run,
        )
        from src.quant_research.schemas import (
            ResearchBacktestDiagnostics,
            ResearchBacktestMetrics,
            ResearchBacktestPositionSnapshot,
            ResearchBacktestResult,
        )

        # ----- Input validation ---------------------------------------
        if request.strategy not in VALID_STRATEGIES:
            raise QuantResearchValidationError(
                f"Unknown strategy {request.strategy!r}. "
                f"Allowed: {list(VALID_STRATEGIES)}",
                field="strategy",
            )
        if request.rebalance_frequency not in VALID_REBALANCE:
            raise QuantResearchValidationError(
                f"Unknown rebalance_frequency {request.rebalance_frequency!r}. "
                f"Allowed: {list(VALID_REBALANCE)}",
                field="rebalance_frequency",
            )
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
        if (end - start).days > _BT_MAX_DAYS:
            raise QuantResearchValidationError(
                f"Backtest window too large: max {_BT_MAX_DAYS} calendar days.",
                field="start_date/end_date",
            )
        if len(request.stocks) > _BT_MAX_STOCKS:
            raise QuantResearchValidationError(
                f"Too many stocks: max {_BT_MAX_STOCKS}.",
                field="stocks",
            )
        if request.strategy != "equal_weight_baseline":
            both = bool(request.builtin_factor_id) and bool(request.expression)
            none = not request.builtin_factor_id and not request.expression
            if both or none:
                raise QuantResearchValidationError(
                    "Strategy requires exactly one of `builtin_factor_id` "
                    "or `expression`.",
                    field="builtin_factor_id/expression",
                )

        try:
            cost_model = CostModel.validated(
                commission_bps=request.commission_bps,
                slippage_bps=request.slippage_bps,
            )
        except ValueError as exc:
            raise QuantResearchValidationError(
                str(exc), field="commission_bps/slippage_bps"
            ) from exc

        # ----- Run -----------------------------------------------------
        try:
            outputs = _run(
                BacktestInputs(
                    strategy=request.strategy,
                    stocks=list(request.stocks),
                    start_date=start,
                    end_date=end,
                    rebalance_frequency=request.rebalance_frequency,
                    builtin_factor_id=request.builtin_factor_id,
                    expression=request.expression,
                    factor_name=request.factor_name,
                    top_k=request.top_k,
                    quantile_count=request.quantile_count,
                    initial_cash=request.initial_cash,
                    cost_model=cost_model,
                    min_holding_days=request.min_holding_days,
                    benchmark=request.benchmark,
                )
            )
        except ValueError as exc:
            raise QuantResearchValidationError(
                str(exc), field="factor"
            ) from exc

        # ----- Map engine output -> Pydantic schema -------------------
        result = ResearchBacktestResult(
            enabled=True,
            run_id=outputs.run_id,
            strategy=outputs.strategy,
            factor_kind=outputs.factor_kind,
            factor_id=outputs.factor_id,
            expression=outputs.expression,
            stock_pool=outputs.stock_pool,
            start_date=outputs.start_date.isoformat(),
            end_date=outputs.end_date.isoformat(),
            rebalance_frequency=outputs.rebalance_frequency,
            nav_curve=outputs.nav_curve,
            metrics=ResearchBacktestMetrics(
                total_return=outputs.metrics.total_return,
                annualized_return=outputs.metrics.annualized_return,
                annualized_volatility=outputs.metrics.annualized_volatility,
                sharpe=outputs.metrics.sharpe,
                sortino=outputs.metrics.sortino,
                calmar=outputs.metrics.calmar,
                max_drawdown=outputs.metrics.max_drawdown,
                win_rate=outputs.metrics.win_rate,
                turnover=outputs.metrics.turnover,
                cost_drag=outputs.metrics.cost_drag,
                benchmark_return=outputs.metrics.benchmark_return,
                excess_return=outputs.metrics.excess_return,
                information_ratio=outputs.metrics.information_ratio,
            ),
            diagnostics=ResearchBacktestDiagnostics(
                data_coverage=outputs.diagnostics.data_coverage,
                missing_symbols=outputs.diagnostics.missing_symbols,
                insufficient_history_symbols=outputs.diagnostics.insufficient_history_symbols,
                rebalance_count=outputs.diagnostics.rebalance_count,
                lookahead_bias_guard=outputs.diagnostics.lookahead_bias_guard,
                assumptions=outputs.diagnostics.assumptions,
            ),
            positions=[
                ResearchBacktestPositionSnapshot(
                    date=p.date.isoformat(),
                    weights=p.weights,
                    nav=p.nav,
                    cash_reserve=p.cash_reserve,
                    cost_deducted=p.cost_deducted,
                )
                for p in outputs.positions
            ],
            created_at=outputs.created_at,
        )

        # ----- Cache (LRU) -------------------------------------------
        cache = self._cache()
        cache[result.run_id] = result
        cache.move_to_end(result.run_id)
        while len(cache) > self._BACKTEST_CACHE_MAX:
            cache.popitem(last=False)
        return result

    def get_backtest(self, run_id: str):
        """Lookup a previously-run backtest by run_id. Returns None if
        not in the in-memory cache (no DB persistence in Phase 3)."""
        if not self.enabled:
            raise QuantResearchDisabledError(
                "Quant Research Lab is disabled."
            )
        return self._cache().get(run_id)

    # ==================================================================
    # Phase 4 — Portfolio Optimizer + Research Risk
    # ==================================================================
    #
    # Research-only methods. They never touch ``portfolio_trades``,
    # never emit orders, and never modify the live portfolio. The
    # ``current_risk()`` adapter delegates to the existing
    # ``PortfolioRiskService`` for live-portfolio dashboards but does
    # so via a read-only snapshot.

    def optimize_portfolio(self, request):
        """Run the lightweight optimizer on a stock pool over the
        supplied returns window. Returns a Pydantic
        ``PortfolioOptimizationResult``."""
        from datetime import date as _date

        from src.quant_research.portfolio import (
            PortfolioOptimizerInputs,
            optimize_portfolio as _optimize,
        )
        from src.quant_research.schemas import PortfolioOptimizationResult

        if not self.enabled:
            raise QuantResearchDisabledError("Quant Research Lab is disabled.")

        # Defensive validation.
        symbols = list(dict.fromkeys(request.symbols or []))  # dedup, preserve order
        if not symbols:
            raise QuantResearchValidationError(
                "symbols must not be empty.", field="symbols",
            )
        if len(symbols) > 50:
            raise QuantResearchValidationError(
                "symbols too long (max 50).", field="symbols",
            )
        try:
            start = _date.fromisoformat(request.start_date)
            end = _date.fromisoformat(request.end_date)
        except Exception as exc:
            raise QuantResearchValidationError(
                f"start_date/end_date must be ISO YYYY-MM-DD: {exc}",
                field="start_date" if "start" in str(exc).lower() else "end_date",
            )
        if start > end:
            raise QuantResearchValidationError(
                "start_date must be ≤ end_date.", field="start_date",
            )
        if (end - start).days > 730:
            raise QuantResearchValidationError(
                "(end_date - start_date) must be ≤ 730 days.", field="end_date",
            )
        if request.min_weight_per_symbol > request.max_weight_per_symbol:
            raise QuantResearchValidationError(
                "min_weight_per_symbol must be ≤ max_weight_per_symbol.",
                field="min_weight_per_symbol",
            )

        # Load returns matrix (reuse the same loader as the backtest engine).
        returns_panel = self._build_returns_panel(symbols, start, end)
        inputs = PortfolioOptimizerInputs(
            objective=request.objective,
            symbols=symbols,
            returns_panel=returns_panel,
            long_only=request.long_only,
            min_weight_per_symbol=request.min_weight_per_symbol,
            max_weight_per_symbol=request.max_weight_per_symbol,
            cash_weight=request.cash_weight,
            max_turnover=request.max_turnover,
            current_weights=request.current_weights,
            sector_exposure_limit=request.sector_exposure_limit,
        )
        try:
            output = _optimize(inputs)
        except ValueError as exc:
            raise QuantResearchValidationError(str(exc), field="objective")

        return PortfolioOptimizationResult(
            enabled=True,
            status=output.status,
            objective=output.objective,
            symbols=symbols,
            weights=output.weights,
            cash_weight=output.cash_weight,
            expected_annual_return=output.expected_annual_return,
            expected_annual_volatility=output.expected_annual_volatility,
            diagnostics=list(output.diagnostics),
            assumptions=dict(output.assumptions),
            is_research_only=True,
            trade_orders_emitted=False,
        )

    def evaluate_research_risk(self, request):
        """Run standalone risk evaluation on user-supplied weights +
        a returns window. Returns a Pydantic
        ``PortfolioRiskResearchResult``."""
        from datetime import date as _date

        from src.quant_research.portfolio import (
            ResearchRiskInputs,
            evaluate_research_risk as _evaluate,
        )
        from src.quant_research.schemas import PortfolioRiskResearchResult

        if not self.enabled:
            raise QuantResearchDisabledError("Quant Research Lab is disabled.")

        if not request.weights:
            raise QuantResearchValidationError(
                "weights must not be empty.", field="weights",
            )
        if len(request.weights) > 50:
            raise QuantResearchValidationError(
                "weights too long (max 50 symbols).", field="weights",
            )
        try:
            start = _date.fromisoformat(request.start_date)
            end = _date.fromisoformat(request.end_date)
        except Exception as exc:
            raise QuantResearchValidationError(
                f"start_date/end_date must be ISO YYYY-MM-DD: {exc}",
            )
        if start > end:
            raise QuantResearchValidationError(
                "start_date must be ≤ end_date.", field="start_date",
            )
        if (end - start).days > 730:
            raise QuantResearchValidationError(
                "(end_date - start_date) must be ≤ 730 days.", field="end_date",
            )

        symbols = list(request.weights.keys())
        returns_panel = self._build_returns_panel(symbols, start, end)
        bench_returns = None
        if request.benchmark_symbol:
            bench_panel = self._build_returns_panel(
                [request.benchmark_symbol], start, end,
            )
            if request.benchmark_symbol in bench_panel.columns:
                bench_returns = bench_panel[request.benchmark_symbol].dropna()

        inputs = ResearchRiskInputs(
            weights=request.weights,
            returns_panel=returns_panel,
            benchmark_returns=bench_returns,
            var_confidence=request.var_confidence,
            concentration_threshold_pct=request.concentration_threshold_pct,
        )
        result = _evaluate(inputs)
        return PortfolioRiskResearchResult(
            enabled=True,
            weights=dict(result.weights),
            daily_observation_count=result.daily_observation_count,
            concentration=dict(result.concentration),
            sector_concentration_status=result.sector_concentration_status,
            volatility=dict(result.volatility),
            drawdown=dict(result.drawdown),
            var_confidence=result.var_confidence,
            historical_var=result.historical_var,
            historical_cvar=result.historical_cvar,
            beta=result.beta,
            beta_status=result.beta_status,
            diagnostics=list(result.diagnostics),
            assumptions=dict(result.assumptions),
            is_research_only=True,
            trade_orders_emitted=False,
        )

    def current_risk(self):
        """Live-portfolio research view. Delegates to the existing
        ``PortfolioRiskService.get_risk_report()`` so the dashboard
        shares one source of truth.

        When no live portfolio exists (no accounts), returns
        ``has_live_portfolio=False`` instead of erroring — the SPA
        renders an "import a portfolio first" hint."""
        from src.quant_research.schemas import PortfolioCurrentRiskResult

        if not self.enabled:
            raise QuantResearchDisabledError("Quant Research Lab is disabled.")

        try:
            from src.services.portfolio_service import PortfolioService
            from src.services.portfolio_risk_service import PortfolioRiskService

            portfolio_svc = PortfolioService()
            accounts = portfolio_svc.list_accounts(include_inactive=False)
            if not accounts:
                return PortfolioCurrentRiskResult(
                    enabled=True,
                    has_live_portfolio=False,
                    risk_report=None,
                    diagnostics=[
                        "No active portfolio accounts; "
                        "import via /api/v1/portfolio/* first."
                    ],
                )
            risk_svc = PortfolioRiskService(portfolio_svc)
            report = risk_svc.get_risk_report()
            return PortfolioCurrentRiskResult(
                enabled=True,
                has_live_portfolio=True,
                risk_report=report,
                diagnostics=[],
            )
        except Exception as exc:
            logger.exception("current_risk delegation failed: %s", exc)
            return PortfolioCurrentRiskResult(
                enabled=True,
                has_live_portfolio=False,
                risk_report=None,
                diagnostics=[
                    f"PortfolioRiskService unavailable: {type(exc).__name__}"
                ],
            )

    # ------------------------------------------------------------------
    # Phase 4 helpers
    # ------------------------------------------------------------------

    def _build_returns_panel(self, symbols, start, end) -> "pd.DataFrame":  # type: ignore[name-defined]
        """Build a daily returns panel (rows=date, cols=symbol).

        Reuses the same DB-first / fetcher-fallback loader the rest of
        the lab uses; symbols with no usable history simply don't
        appear in the panel (caller surfaces them via diagnostics).
        """
        import pandas as pd
        from src.services.history_loader import load_history_df

        span = (end - start).days + 60  # buffer so daily pct_change has data
        cols = {}
        for code in symbols:
            try:
                df, _ = load_history_df(code, days=span, target_date=end)
            except Exception as exc:
                logger.warning("optimizer: load_history_df(%s) raised: %s", code, exc)
                continue
            if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.sort_values("date").reset_index(drop=True)
            close = pd.Series(
                df["close"].astype(float).to_numpy(),
                index=df["date"].to_numpy(),
                dtype=float,
            )
            ret = close.pct_change()
            mask = (ret.index >= start) & (ret.index <= end)
            cols[code] = ret[mask].dropna()
        if not cols:
            return pd.DataFrame()
        return pd.DataFrame(cols).sort_index()
