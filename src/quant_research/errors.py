# -*- coding: utf-8 -*-
"""Error hierarchy for Quant Research Lab.

Kept deliberately small in Phase 1 — later phases (factor evaluator,
backtest engine, optimizer) will add domain-specific subclasses without
breaking this base contract.
"""

from __future__ import annotations


class QuantResearchError(Exception):
    """Base class for all Quant Research Lab errors.

    Endpoints catch this and translate to a structured JSON body
    (see ``api/v1/endpoints/quant_research.py``); they should never let
    a generic Exception bubble to the FastAPI default handler.
    """


class QuantResearchDisabledError(QuantResearchError):
    """Raised when an action is attempted while the feature flag is off."""


class QuantResearchNotImplementedError(QuantResearchError):
    """Raised when a capability is declared but not yet implemented in
    the current phase (e.g., advanced solver requires optional deps)."""


class QuantResearchValidationError(QuantResearchError):
    """Raised when a request body / FactorSpec / cron etc. fails validation."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field
