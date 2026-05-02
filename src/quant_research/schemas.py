# -*- coding: utf-8 -*-
"""Pydantic schemas for the Quant Research Lab.

These shapes are what API clients see. They are deliberately stable across
phases:
- Phase 1 only emits ``QuantResearchStatus`` and ``QuantResearchCapabilities``.
- Phase 2 will add the factor-evaluation request/response schemas in this
  same module (or a sub-module ``factors/``).
- Phase 3 will add backtest request/response schemas.

We use ``pydantic.BaseModel`` (already a hard dep of FastAPI) and avoid
adding any new third-party schema lib.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# =====================================================================
# Common
# =====================================================================

class QuantResearchError(BaseModel):
    """Structured error body returned by every quant endpoint when it
    decides to fail gracefully (instead of raising a 500)."""

    error: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable explanation")
    field: Optional[str] = Field(
        default=None,
        description="When validation fails, the offending field name",
    )


# =====================================================================
# /api/v1/quant/status
# =====================================================================

class QuantResearchStatus(BaseModel):
    """High-level on/off + version info, safe to call without auth-uplift."""

    enabled: bool = Field(description="Master feature-flag value")
    status: str = Field(
        description=(
            "One of: ``not_enabled`` (flag off), ``ready`` (flag on, "
            "scaffold only — Phase 1), ``operational`` (later phases when "
            "real evaluation/backtest is available)."
        )
    )
    message: str = Field(description="Human-readable hint for the WebUI")
    phase: str = Field(
        default="phase-1-scaffold",
        description=(
            "Which milestone of the Quant Research Lab roadmap is live "
            "in this build (informational, drives WebUI hints)."
        ),
    )


# =====================================================================
# /api/v1/quant/capabilities
# =====================================================================

class QuantResearchCapability(BaseModel):
    """A single capability advertised by the Lab (factor lib, backtest, etc.)."""

    name: str = Field(description="Stable identifier, e.g. ``factor_evaluation``")
    title: str = Field(description="Human-readable title for UI")
    available: bool = Field(
        description="True if the endpoint accepts real requests in this build"
    )
    phase: str = Field(
        description="Which roadmap phase this capability lights up in"
    )
    description: str = Field(description="One-paragraph summary")
    endpoints: List[str] = Field(
        default_factory=list,
        description=(
            "Future endpoint paths attached to this capability. Listed even "
            "when ``available=False`` so the SPA can render placeholders."
        ),
    )
    requires_optional_deps: List[str] = Field(
        default_factory=list,
        description="Pip packages from requirements-quant.txt this needs",
    )


class QuantResearchCapabilities(BaseModel):
    """Capability inventory returned by ``GET /api/v1/quant/capabilities``."""

    enabled: bool
    capabilities: List[QuantResearchCapability]


# =====================================================================
# Run metadata (used by future endpoints; surfaced now for FE typing)
# =====================================================================

class QuantResearchRunMeta(BaseModel):
    """Metadata wrapper shared across factor-evaluation / backtest / opt
    runs. Kept in Phase 1 so FE can rely on a stable envelope shape."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    kind: str = Field(
        description="``factor_eval`` | ``backtest`` | ``portfolio_opt``"
    )
    created_at: str = Field(description="ISO-8601 UTC timestamp")
    config_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="The exact request that produced this run (for replay)",
    )
    diagnostics: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Human-readable warnings (data coverage, missing symbols, "
            "lookahead-bias guard status, etc.)."
        ),
    )


# =====================================================================
# Phase 2 — Factor Lab schemas
# =====================================================================

class FactorInputSchema(BaseModel):
    """Description of a built-in factor available for evaluation.

    Returned by ``GET /api/v1/quant/factors`` so the WebUI can render a
    selector. ``expected_direction`` is informative — the evaluator does
    NOT enforce sign agreement; it just records the hypothesis.
    """

    id: str = Field(description="Stable identifier, e.g. ``ma_ratio_5_20``")
    name: str = Field(description="Display name")
    description: str
    expected_direction: str = Field(
        description="``positive`` / ``negative`` / ``unknown``"
    )
    lookback_days: int = Field(
        description="Calendar-day buffer the evaluator needs before "
                    "the requested start_date for warm-up of rolling stats.",
    )


class FactorRegistryResponse(BaseModel):
    """Response for ``GET /api/v1/quant/factors``."""

    enabled: bool
    builtins: List[FactorInputSchema] = Field(default_factory=list)


class FactorSpec(BaseModel):
    """Describes the factor under evaluation.

    Either ``builtin_id`` *or* ``expression`` (not both) must be
    supplied. The endpoint enforces this; the schema accepts either.
    """

    name: Optional[str] = Field(
        default=None,
        description="Display name. Falls back to the builtin's name or "
                    "``\"custom expression\"`` for free-form formulas.",
    )
    builtin_id: Optional[str] = Field(
        default=None,
        description="If set, identifies a built-in factor (see "
                    "``GET /api/v1/quant/factors``).",
    )
    expression: Optional[str] = Field(
        default=None,
        description="Free-form factor formula. Parsed with the AST "
                    "whitelist; ``eval``/``exec`` are NEVER used.",
    )


class FactorEvaluationRequest(BaseModel):
    """Request body for ``POST /api/v1/quant/factors/evaluate``."""

    factor: FactorSpec
    stocks: List[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Stock pool. Capped at 50 to bound Cloud Run memory.",
    )
    start_date: str = Field(
        description="ISO date YYYY-MM-DD inclusive (signal range start)"
    )
    end_date: str = Field(
        description="ISO date YYYY-MM-DD inclusive (signal range end)"
    )
    forward_window: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Days into the future to compute return for the IC pairing.",
    )
    quantile_count: int = Field(
        default=5,
        ge=2,
        le=10,
        description="How many quantile buckets when computing per-bucket "
                    "forward returns and long-short spread.",
    )


class FactorCoverageReport(BaseModel):
    """Coverage diagnostics for a factor evaluation run."""

    requested_stocks: List[str]
    covered_stocks: List[str]
    missing_stocks: List[str]
    requested_days: int
    total_observations: int
    missing_observations: int
    missing_rate: Optional[float] = Field(
        default=None,
        description="``missing_observations / (days * stocks)``, "
                    "``None`` when the panel is empty.",
    )


class FactorMetricSummary(BaseModel):
    """Aggregated cross-sectional metrics for a factor evaluation."""

    # Daily IC series (kept for diagnostics; can grow with date range).
    ic: List[Optional[float]] = Field(default_factory=list)
    rank_ic: List[Optional[float]] = Field(default_factory=list)
    daily_ic_count: int = 0
    daily_rank_ic_count: int = 0

    ic_mean: Optional[float] = None
    ic_std: Optional[float] = None
    icir: Optional[float] = Field(
        default=None,
        description="ic_mean / ic_std (annualization left to consumer).",
    )
    rank_ic_mean: Optional[float] = None

    quantile_count: int
    quantile_returns: Dict[int, Optional[float]] = Field(
        default_factory=dict,
        description="Per-quantile mean forward return (1-indexed).",
    )
    long_short_spread: Optional[float] = Field(
        default=None,
        description="Top-quantile mean return minus bottom-quantile mean return.",
    )

    factor_turnover: Optional[float] = Field(
        default=None,
        description="Mean fraction of stocks switching quantile day-over-day.",
    )
    autocorrelation: Optional[float] = Field(
        default=None,
        description="Average per-stock lag-1 autocorrelation of the raw factor.",
    )


class FactorEvaluationResult(BaseModel):
    """Response body for ``POST /api/v1/quant/factors/evaluate``."""

    enabled: bool = True
    run_id: str
    factor: FactorSpec
    factor_kind: str = Field(description="``builtin`` | ``expression``")
    stock_pool: List[str]
    start_date: str
    end_date: str
    forward_window: int
    quantile_count: int
    coverage: FactorCoverageReport
    metrics: FactorMetricSummary
    diagnostics: List[str] = Field(default_factory=list)
    assumptions: Dict[str, Any] = Field(default_factory=dict)


# =====================================================================
# Phase 3 — Research Backtest Lab
# =====================================================================
#
# Independent of the existing AI-decision validation backtest under
# ``/api/v1/backtest/*``. These shapes describe a factor-driven trading
# *simulation*: target weights, NAV curve, performance metrics. They
# never describe live trades.

class ResearchBacktestRequest(BaseModel):
    """Request body for ``POST /api/v1/quant/backtests/run``.

    Either ``builtin_factor_id`` or ``expression`` is required for
    factor strategies; ``equal_weight_baseline`` ignores both.
    """

    strategy: str = Field(
        description=(
            "Strategy type: ``top_k_long_only``, ``quantile_long_short`` "
            "(simulated — no real shorting/borrow), or "
            "``equal_weight_baseline`` (factor-free baseline)."
        ),
    )
    stocks: List[str] = Field(description="Stock pool. Hard cap = 50.")
    start_date: str = Field(description="Inclusive ISO date.")
    end_date: str = Field(description="Inclusive ISO date.")
    rebalance_frequency: str = Field(
        default="weekly",
        description="``daily`` | ``weekly`` | ``monthly``.",
    )

    builtin_factor_id: Optional[str] = Field(
        default=None,
        description="Factor id from ``GET /api/v1/quant/factors``.",
    )
    expression: Optional[str] = Field(
        default=None,
        description=(
            "Free-form factor expression (parsed by the AST whitelist; "
            "see ``factors/safe_expression.py``). Mutually exclusive "
            "with ``builtin_factor_id``."
        ),
    )
    factor_name: Optional[str] = Field(default=None, description="Display name.")

    top_k: Optional[int] = Field(
        default=None,
        description="Used by ``top_k_long_only``. Default = ⅕ of pool.",
    )
    quantile_count: int = Field(
        default=5,
        ge=2, le=10,
        description="Used by ``quantile_long_short``.",
    )

    initial_cash: float = Field(default=1_000_000.0, gt=0)
    commission_bps: float = Field(default=10.0, ge=0, le=1000)
    slippage_bps: float = Field(default=5.0, ge=0, le=1000)

    min_holding_days: Optional[int] = Field(
        default=None,
        ge=0,
        description="Skip rebalances when a position is younger than this.",
    )
    benchmark: Optional[str] = Field(
        default=None,
        description="Optional benchmark ticker, e.g. ``SPY`` / ``QQQ``.",
    )


class ResearchBacktestMetrics(BaseModel):
    """All metrics in one block. Every field is Optional because short
    or degenerate runs can legitimately fail to compute some of them."""

    total_return: Optional[float] = None
    annualized_return: Optional[float] = None
    annualized_volatility: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    turnover: Optional[float] = None
    cost_drag: Optional[float] = None
    benchmark_return: Optional[float] = None
    excess_return: Optional[float] = None
    information_ratio: Optional[float] = None


class ResearchBacktestPositionSnapshot(BaseModel):
    """One rebalance day's position state."""

    date: str = Field(description="ISO date of the rebalance.")
    weights: Dict[str, float] = Field(
        description="Stock code → weight. Negative = short (simulated)."
    )
    nav: float
    cash_reserve: float = 0.0  # Phase 3 always 0 (fully invested)
    cost_deducted: float = Field(
        default=0.0,
        description="Dollar transaction cost charged on this rebalance.",
    )


class ResearchBacktestDiagnostics(BaseModel):
    """Everything a researcher needs to interpret the result."""

    data_coverage: Dict[str, Any] = Field(default_factory=dict)
    missing_symbols: List[str] = Field(default_factory=list)
    insufficient_history_symbols: List[str] = Field(default_factory=list)
    rebalance_count: int = 0
    lookahead_bias_guard: bool = True
    assumptions: Dict[str, Any] = Field(default_factory=dict)


class ResearchBacktestResult(BaseModel):
    """Top-level response for ``POST /api/v1/quant/backtests/run`` and
    ``GET /api/v1/quant/backtests/{run_id}``."""

    enabled: bool = True
    run_id: str
    strategy: str
    factor_kind: str = Field(description="``builtin`` | ``expression`` | ``n/a``")
    factor_id: Optional[str] = None
    expression: Optional[str] = None
    stock_pool: List[str]
    start_date: str
    end_date: str
    rebalance_frequency: str
    nav_curve: List[Dict[str, Any]] = Field(
        description="[{\"date\": iso, \"nav\": float}, ...]",
    )
    metrics: ResearchBacktestMetrics
    diagnostics: ResearchBacktestDiagnostics
    positions: List[ResearchBacktestPositionSnapshot]
    created_at: str


# =====================================================================
# Phase 4: Portfolio Optimizer + Research Risk
# =====================================================================

class RiskBudgetConstraint(BaseModel):
    """Optional risk-budget allocation per name. Phase 4 ships this
    schema for future use; the optimizer currently routes any
    ``risk_budget_placeholder`` request to ``not_supported``.
    """
    symbol: str = Field(description="Stock code")
    target_risk_share_pct: float = Field(
        ge=0.0, le=100.0,
        description="Fraction of portfolio variance this name should contribute (%)",
    )


class PortfolioOptimizationRequest(BaseModel):
    """Body for ``POST /api/v1/quant/portfolio/optimize``.

    The endpoint loads each symbol's daily history via
    ``load_history_df``, builds a returns matrix on the
    [start_date, end_date] window, and dispatches to the requested
    objective. ``current_weights`` lets the engine respect a
    ``max_turnover`` ceiling vs. the user's existing book.
    """
    objective: str = Field(
        description=(
            "One of: equal_weight | inverse_volatility | "
            "max_sharpe_simplified | min_variance_simplified | "
            "risk_budget_placeholder."
        ),
    )
    symbols: List[str] = Field(
        min_length=1, max_length=50,
        description="Investment universe — endpoint enforces ≤ 50.",
    )
    start_date: str = Field(description="ISO date for returns window start")
    end_date: str = Field(description="ISO date for returns window end")
    long_only: bool = Field(default=True)
    min_weight_per_symbol: float = Field(default=0.0, ge=0.0, le=1.0)
    max_weight_per_symbol: float = Field(default=1.0, ge=0.0, le=1.0)
    cash_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    max_turnover: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    current_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Current weights for max_turnover blending (research only).",
    )
    sector_exposure_limit: Optional[Dict[str, float]] = Field(
        default=None,
        description=(
            "Per-sector cap (e.g. {'tech': 0.4}). Phase 4 returns "
            "partial_coverage because no sector taxonomy is shipped."
        ),
    )
    risk_budget: Optional[List[RiskBudgetConstraint]] = Field(
        default=None,
        description="Reserved for future risk-parity solver.",
    )


class PortfolioOptimizationResult(BaseModel):
    """Response for ``POST /api/v1/quant/portfolio/optimize``."""
    enabled: bool = True
    status: str = Field(
        description=(
            "ok | not_supported | insufficient_data | infeasible_constraints"
        ),
    )
    objective: str
    symbols: List[str]
    weights: Dict[str, float] = Field(
        description="Target weights per symbol (research only — never sent as orders).",
    )
    cash_weight: float
    expected_annual_return: Optional[float] = None
    expected_annual_volatility: Optional[float] = None
    diagnostics: List[str] = Field(default_factory=list)
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    is_research_only: bool = True
    trade_orders_emitted: bool = False


class PortfolioRiskResearchRequest(BaseModel):
    """Body for ``POST /api/v1/quant/risk/evaluate``.

    The caller supplies a hypothetical portfolio (weights) and a stock
    pool over [start_date, end_date]; the endpoint loads daily returns
    for each symbol and computes concentration / VaR / CVaR / drawdown /
    volatility / (optional) beta.
    """
    weights: Dict[str, float] = Field(
        description="Symbol → target weight (signed for short legs).",
    )
    start_date: str = Field(description="ISO date for returns window start")
    end_date: str = Field(description="ISO date for returns window end")
    benchmark_symbol: Optional[str] = Field(
        default=None,
        description="Optional benchmark ticker for beta computation.",
    )
    var_confidence: float = Field(
        default=0.95, gt=0.0, lt=1.0,
        description="Confidence level for historical VaR / CVaR.",
    )
    concentration_threshold_pct: float = Field(
        default=35.0, ge=0.0, le=100.0,
        description="Single-name weight % above which a symbol triggers an alert.",
    )


class PortfolioRiskResearchResult(BaseModel):
    """Response for ``POST /api/v1/quant/risk/evaluate``."""
    enabled: bool = True
    weights: Dict[str, float]
    daily_observation_count: int
    concentration: Dict[str, Any]
    sector_concentration_status: str = Field(
        default="not_supported",
        description="Phase 4 has no sector taxonomy; field reserved.",
    )
    volatility: Dict[str, Optional[float]]
    drawdown: Dict[str, Optional[float]]
    var_confidence: float
    historical_var: Optional[float]
    historical_cvar: Optional[float]
    beta: Optional[float]
    beta_status: str
    diagnostics: List[str] = Field(default_factory=list)
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    is_research_only: bool = True
    trade_orders_emitted: bool = False


class GeneratedFactorSpec(BaseModel):
    """Validated FactorSpec returned by ``POST /factors/generate``.

    Mirror of the schema enforced in ``ai/validators.py`` — kept here so
    OpenAPI / TypeScript clients see the contract. Any change here
    must be matched in ``REQUIRED_KEYS`` of ``validators.py``.
    """
    name: str
    hypothesis: str
    inputs: List[str]
    expression: str
    window: int
    expected_direction: str
    market_scope: str
    risk_notes: List[str] = Field(default_factory=list)
    validation_plan: List[str] = Field(default_factory=list)


class FactorGenerationRequest(BaseModel):
    """Body for ``POST /api/v1/quant/factors/generate``.

    The endpoint forwards ``hypothesis`` to the LLM (via the existing
    ``LLMToolAdapter``); ``existing_factors`` is a soft anti-duplication
    hint — the LLM is free to ignore it but the prompt nudges away from
    trivial copies. ``include_raw=true`` echoes the LLM's raw text in
    the response (debug only; off by default to keep the surface tight).
    """
    hypothesis: str = Field(
        ...,
        min_length=1, max_length=1000,
        description="Natural-language research hypothesis.",
    )
    market_scope: str = Field(
        default="all",
        description="Hint to the LLM: ``cn`` | ``hk`` | ``us`` | ``all``.",
    )
    data_window: int = Field(
        default=252, ge=20, le=2520,
        description="Informational hint (trading days). Does not bind the LLM.",
    )
    existing_factors: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of existing built-in factor ids to avoid trivially "
            "duplicating. ``None`` lets the service fall back to the "
            "current ``BUILTIN_FACTORS`` keys."
        ),
    )
    include_raw: bool = Field(
        default=False,
        description="Echo the raw LLM string for debugging (off by default).",
    )


class FactorGenerationResponse(BaseModel):
    """Response for ``POST /api/v1/quant/factors/generate``."""
    enabled: bool = True
    spec: GeneratedFactorSpec
    model: str = Field(description="LLM model that produced the spec.")
    provider: str = Field(description="LiteLLM provider namespace.")
    usage: Dict[str, Any] = Field(
        default_factory=dict,
        description="Token usage from the underlying provider, if available.",
    )
    expression_node_count: int = Field(
        description="AST node count of the generated expression (diagnostic).",
    )
    elapsed_ms: float = Field(
        description="Wall-clock LLM call latency in milliseconds.",
    )
    raw_response: Optional[str] = Field(
        default=None,
        description="Raw LLM output; populated only when include_raw=true.",
    )
    is_research_only: bool = True


class FactorGenerateAndEvaluateRequest(FactorGenerationRequest):
    """Body for ``POST /api/v1/quant/factors/generate-and-evaluate``.

    Reuses every field of ``FactorGenerationRequest`` and adds the
    ``stocks`` / date / window knobs needed for the immediate
    evaluation. The endpoint runs the LLM once, validates the spec,
    and (only if validation passes) feeds the AST-checked expression
    into the existing factor evaluator.
    """
    stocks: List[str] = Field(
        ..., min_length=1, max_length=50,
        description="Stock pool to evaluate the generated factor on.",
    )
    start_date: str = Field(description="ISO date YYYY-MM-DD inclusive.")
    end_date: str = Field(description="ISO date YYYY-MM-DD inclusive.")
    forward_window: int = Field(default=5, ge=1, le=60)
    quantile_count: int = Field(default=5, ge=2, le=10)


class FactorGenerateAndEvaluateResponse(BaseModel):
    """Response wraps both the generated spec and its evaluation result.

    Either both fields are populated (happy path) or ``evaluation`` is
    ``None`` and ``diagnostics`` carries the reason — typically a
    coverage / data-load issue that the spec validation alone wouldn't
    have caught.
    """
    enabled: bool = True
    generation: FactorGenerationResponse
    evaluation: Optional[FactorEvaluationResult] = None
    diagnostics: List[str] = Field(default_factory=list)


class PortfolioCurrentRiskResult(BaseModel):
    """Thin adapter response for ``GET /api/v1/quant/portfolio/current-risk``.

    Delegates to the existing ``PortfolioRiskService.get_risk_report()``
    and surfaces it under the quant-research namespace so the SPA can
    render live + research views in one page.
    """
    enabled: bool = True
    has_live_portfolio: bool
    risk_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Pass-through of PortfolioRiskService.get_risk_report() output.",
    )
    diagnostics: List[str] = Field(default_factory=list)
