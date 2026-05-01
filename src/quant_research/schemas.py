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
