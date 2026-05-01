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
