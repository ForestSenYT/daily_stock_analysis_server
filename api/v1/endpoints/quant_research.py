# -*- coding: utf-8 -*-
"""
===================================
Quant Research Lab API
===================================

Phase-1 endpoints — only ``/status`` and ``/capabilities``. Both are
admin-session protected by the existing ``AuthMiddleware`` because they
live under ``/api/v1/quant/*``. Both are safe to call when the feature
flag is off — they return a structured payload describing the lab as
``not_enabled`` rather than raising 5xx.

Future phases attach more routes (``/factors``, ``/backtests``,
``/portfolio``, ``/risk``) on the SAME router; this file should remain
the single mounting point so the API surface is easy to audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.quant_research.errors import (
    QuantResearchDisabledError,
    QuantResearchError,
    QuantResearchValidationError,
)
from src.quant_research.schemas import (
    FactorEvaluationRequest,
    FactorEvaluationResult,
    FactorRegistryResponse,
    QuantResearchCapabilities,
    QuantResearchStatus,
    ResearchBacktestRequest,
    ResearchBacktestResult,
)
from src.quant_research.service import QuantResearchService

logger = logging.getLogger(__name__)

router = APIRouter()
_QUANT_DISABLED_MESSAGE = "Quant Research Lab is disabled."
_QUANT_VALIDATION_MESSAGE = "Invalid quant research request."
_QUANT_ERROR_MESSAGE = "Quant Research operation failed."


def _service() -> QuantResearchService:
    """Build a fresh service per request so config changes (e.g., a
    runtime flip via /api/v1/system/config) take effect immediately.

    The constructor is cheap — no DB hits.
    """
    return QuantResearchService()


@router.get(
    "/status",
    response_model=QuantResearchStatus,
    summary="Quant Research Lab status",
    description=(
        "Returns the master feature-flag value and a hint about which "
        "roadmap phase is live in this build. Safe to call when the flag "
        "is off — never returns 5xx for a healthy service."
    ),
)
def quant_status() -> QuantResearchStatus:
    try:
        return _service().status()
    except QuantResearchError as exc:
        logger.warning("Quant Research status surfaced a domain error: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )


@router.get(
    "/capabilities",
    response_model=QuantResearchCapabilities,
    summary="Quant Research Lab capability inventory",
    description=(
        "Lists every capability the Lab plans to expose, marking which "
        "are ``available=True`` in this build vs. which are placeholders "
        "for later phases. The SPA uses this to render disabled cards "
        "with explanatory text."
    ),
)
def quant_capabilities() -> QuantResearchCapabilities:
    try:
        return _service().capabilities()
    except QuantResearchError as exc:
        logger.warning("Quant Research capabilities surfaced a domain error: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )


@router.get(
    "/healthcheck",
    summary="Quant Research Lab healthcheck (debug only)",
    description=(
        "Cheap ``{ok: true}`` ping so deploy verification / curl tests "
        "can confirm the router is mounted without exercising any "
        "service logic. Always 200 when the app is up."
    ),
)
def quant_healthcheck() -> Dict[str, Any]:
    return {"ok": True, "module": "quant_research", "phase": "phase-2-factor-lab"}


# =====================================================================
# Phase 2 — Factor Lab
# =====================================================================

@router.get(
    "/factors",
    response_model=FactorRegistryResponse,
    summary="List built-in factors available for evaluation",
    description=(
        "Returns the registry of built-in factors (id, name, "
        "description, expected direction, lookback days). When the "
        "feature flag is off the response is "
        "``{enabled: false, builtins: []}`` — never 5xx."
    ),
)
def quant_list_factors() -> FactorRegistryResponse:
    try:
        return _service().list_factors()
    except QuantResearchError as exc:
        logger.warning("Quant Research list_factors error: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )


@router.post(
    "/factors/evaluate",
    response_model=FactorEvaluationResult,
    summary="Evaluate a factor on a stock pool",
    description=(
        "Run cross-sectional factor evaluation: IC / RankIC / ICIR, "
        "quantile mean returns, long-short spread, factor turnover, "
        "lag-1 autocorrelation. Pass either ``factor.builtin_id`` "
        "(see ``GET /factors``) or ``factor.expression`` (free-form, "
        "AST-whitelist-validated) — never both. ``stocks`` capped at "
        "50, ``forward_window`` ≤ 60 days, date range ≤ 365 days. "
        "All returned metrics are computed without look-ahead: factor "
        "signal at date *t* uses only data up to *t*; forward return "
        "uses *t+window*."
    ),
    responses={
        200: {"description": "Evaluation finished (may include diagnostics for partial coverage)"},
        400: {"description": "Validation error (bad factor / dates / pool)"},
        503: {"description": "Quant Research Lab disabled"},
    },
)
def quant_evaluate_factor(request: FactorEvaluationRequest) -> FactorEvaluationResult:
    try:
        return _service().evaluate_factor(request)
    except QuantResearchDisabledError:
        # Disabled flag — surface as a structured 503 so SPA can render
        # the "enable in settings" hint instead of a generic error.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "quant_research_disabled",
                "message": _QUANT_DISABLED_MESSAGE,
            },
        )
    except QuantResearchValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "quant_research_validation",
                "message": _QUANT_VALIDATION_MESSAGE,
                "field": exc.field,
            },
        )
    except QuantResearchError:
        logger.exception("Quant Research evaluate_factor failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )


# =====================================================================
# Phase 3 — Research Backtest endpoints
# =====================================================================
#
# Mirror the same admin-session protection + structured error mapping
# as the factor endpoints. ``run_backtest`` is synchronous; with the
# Cloud Run 30-min request timeout that's enough for the input limits
# we enforce in the service (≤ 50 stocks, ≤ 366 days). A future async
# variant can reuse the same service method via background task pattern
# already used by ``/analyze/async``.

@router.post(
    "/backtests/run",
    response_model=ResearchBacktestResult,
    summary="Run a research backtest",
    description=(
        "Simulate a factor-driven trading strategy on the supplied stock "
        "pool. Independent from ``/api/v1/backtest/*`` (which validates "
        "AI historical decisions). Returns NAV curve, daily metrics, "
        "and rebalance-day position snapshots. Caches the result in "
        "memory for follow-up ``GET /backtests/{run_id}`` calls during "
        "the same instance lifetime."
    ),
    responses={
        200: {"description": "Backtest finished (check diagnostics for coverage gaps)"},
        400: {"description": "Validation error (bad strategy / dates / factor / costs)"},
        503: {"description": "Quant Research Lab disabled"},
    },
)
def quant_run_backtest(request: ResearchBacktestRequest) -> ResearchBacktestResult:
    try:
        return _service().run_backtest(request)
    except QuantResearchDisabledError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "quant_research_disabled",
                "message": _QUANT_DISABLED_MESSAGE,
            },
        )
    except QuantResearchValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "quant_research_validation",
                "message": _QUANT_VALIDATION_MESSAGE,
                "field": exc.field,
            },
        )
    except QuantResearchError:
        logger.exception("Quant Research run_backtest failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )


@router.get(
    "/backtests/{run_id}",
    response_model=ResearchBacktestResult,
    summary="Fetch a previously-run backtest",
    description=(
        "Look up a backtest result by ``run_id`` returned from "
        "``POST /backtests/run``. Phase 3 keeps results in an in-memory "
        "cache (≤32 most recent on this instance); a 404 is returned if "
        "the run has aged out or the instance restarted. Phase 4+ may "
        "add a database-backed history."
    ),
    responses={
        200: {"description": "Found"},
        404: {"description": "Not in cache (expired or different instance)"},
        503: {"description": "Quant Research Lab disabled"},
    },
)
def quant_get_backtest(run_id: str) -> ResearchBacktestResult:
    try:
        result = _service().get_backtest(run_id)
    except QuantResearchDisabledError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "quant_research_disabled",
                "message": _QUANT_DISABLED_MESSAGE,
            },
        )
    except QuantResearchError:
        logger.exception("Quant Research get_backtest failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "quant_research_error", "message": _QUANT_ERROR_MESSAGE},
        )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "quant_research_not_found",
                "message": "Backtest run_id not found in cache.",
            },
        )
    return result
