# -*- coding: utf-8 -*-
"""Factor Lab — factor registry, builtins, safe expression, evaluator.

Phase 2 of the Quant Research Lab. Provides:

- ``registry.list_builtin_factors()`` — for ``GET /api/v1/quant/factors``
- ``evaluator.evaluate_factor(...)`` — for ``POST /api/v1/quant/factors/evaluate``

The factor functions in ``builtins`` are pure pandas / numpy. The
``safe_expression`` module is the only place where a user-supplied
formula string is allowed to become executable logic; it does so via an
AST whitelist, never via ``eval`` / ``exec``.
"""

from src.quant_research.factors.evaluator import (
    FactorEvalInputs,
    FactorEvalOutputs,
    MAX_FORWARD_WINDOW,
    MAX_LOOKBACK_DAYS,
    MAX_STOCKS,
    MIN_STOCKS_PER_DAY_FOR_IC,
    evaluate_factor,
)
from src.quant_research.factors.registry import (
    BuiltinFactorEntry,
    get_builtin_factor_function,
    get_builtin_factor_meta,
    list_builtin_factors,
)
from src.quant_research.factors.safe_expression import (
    DEFAULT_ALLOWED_INPUTS,
    SAFE_FUNCTIONS,
    SafeExpressionSpec,
    UnsafeExpressionError,
    compile_safe_expression,
    parse_safe_expression,
)

__all__ = [
    # registry
    "BuiltinFactorEntry",
    "list_builtin_factors",
    "get_builtin_factor_function",
    "get_builtin_factor_meta",
    # safe_expression
    "DEFAULT_ALLOWED_INPUTS",
    "SAFE_FUNCTIONS",
    "SafeExpressionSpec",
    "UnsafeExpressionError",
    "compile_safe_expression",
    "parse_safe_expression",
    # evaluator
    "FactorEvalInputs",
    "FactorEvalOutputs",
    "MAX_FORWARD_WINDOW",
    "MAX_LOOKBACK_DAYS",
    "MAX_STOCKS",
    "MIN_STOCKS_PER_DAY_FOR_IC",
    "evaluate_factor",
]
