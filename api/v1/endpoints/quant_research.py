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

from src.quant_research.errors import QuantResearchError
from src.quant_research.schemas import (
    QuantResearchCapabilities,
    QuantResearchStatus,
)
from src.quant_research.service import QuantResearchService

logger = logging.getLogger(__name__)

router = APIRouter()


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
            detail={"error": "quant_research_error", "message": str(exc)},
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
            detail={"error": "quant_research_error", "message": str(exc)},
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
    return {"ok": True, "module": "quant_research", "phase": "phase-1-scaffold"}
