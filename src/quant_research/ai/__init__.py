# -*- coding: utf-8 -*-
"""AI-driven FactorSpec generation (Phase 5).

The package exposes the public types the service layer needs and keeps
the LLM-call / prompt / validation modules separately importable so
tests can pin each layer independently.

Imports are kept light here: nothing at top level should pull in
``litellm`` or any other heavy dep, because Quant Research-disabled
deployments must still be able to ``import src.quant_research.ai`` for
schema discovery without paying that cost.
"""

from src.quant_research.ai.factor_generator import (
    FactorGenerationOutcome,
    FactorGenerator,
)
from src.quant_research.ai.validators import (
    FactorGenerationError,
    FactorSpecValidation,
    parse_and_validate,
    parse_json_strict,
    validate_factor_spec_safety,
    validate_factor_spec_shape,
)

__all__ = [
    "FactorGenerationError",
    "FactorGenerationOutcome",
    "FactorGenerator",
    "FactorSpecValidation",
    "parse_and_validate",
    "parse_json_strict",
    "validate_factor_spec_safety",
    "validate_factor_spec_shape",
]
