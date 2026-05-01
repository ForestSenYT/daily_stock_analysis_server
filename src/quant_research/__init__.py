# -*- coding: utf-8 -*-
"""
===================================
Quant Research Lab
===================================

Research-grade quantitative module — independent from the AI-decision
validation backtest under ``/api/v1/backtest/*``.

Phase 1 (this file): scaffolding only. Public surface is intentionally
small; later phases (P2 Factor Lab, P3 Research Backtest, P4 Portfolio
Optimizer, P5 AI FactorSpec generation, P6 Agent integration) will add
sub-packages without changing the existing main analysis / backtest /
portfolio paths.

Feature flag: ``QUANT_RESEARCH_ENABLED`` (default ``false``).
When disabled, every ``/api/v1/quant/*`` endpoint returns a structured
``not_enabled`` response — never a 500.

Public re-exports kept minimal to avoid leaking internals before they
stabilize.
"""

from src.quant_research.errors import (
    QuantResearchError,
    QuantResearchDisabledError,
    QuantResearchNotImplementedError,
    QuantResearchValidationError,
)
from src.quant_research.metrics import (
    SUPPORTED_FACTOR_METRICS,
    SUPPORTED_BACKTEST_METRICS,
)
from src.quant_research.schemas import (
    QuantResearchStatus,
    QuantResearchCapability,
    QuantResearchCapabilities,
    QuantResearchRunMeta,
)
from src.quant_research.service import QuantResearchService

__all__ = [
    "QuantResearchError",
    "QuantResearchDisabledError",
    "QuantResearchNotImplementedError",
    "QuantResearchValidationError",
    "SUPPORTED_FACTOR_METRICS",
    "SUPPORTED_BACKTEST_METRICS",
    "QuantResearchStatus",
    "QuantResearchCapability",
    "QuantResearchCapabilities",
    "QuantResearchRunMeta",
    "QuantResearchService",
]
