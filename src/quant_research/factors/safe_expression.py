# -*- coding: utf-8 -*-
"""AST-based whitelist evaluator for user / AI-supplied factor expressions.

Why this exists
---------------
A FactorSpec may carry an ``expression`` (string of Python-like math) so
researchers / the LLM can sketch new factors without writing Python files.
``eval(expression)`` would let a malicious string break out of the
container; we never use ``eval`` / ``exec`` / ``compile``. Instead this
module:

1. Parses the string into an AST (``ast.parse(..., mode="eval")``).
2. Walks every node and rejects anything not on a strict whitelist:
   - Allowed: ``BinOp`` (+ - * / // % **), ``UnaryOp`` (+ - not),
     ``BoolOp`` (and / or), ``Compare`` (< <= > >= == != ),
     ``Constant`` (numbers / True / False / None),
     ``Name`` (only whitelist identifiers — column names + helper
     functions), ``Call`` (only to whitelist helpers).
   - Rejected: ``Attribute`` (no ``x.__import__``),
     ``Subscript``, ``Lambda``, comprehensions, ``Starred``, ``Slice``,
     ``Assign``, ``With``, ``Try``, ``Yield``, ``Await``, ``Import``,
     dunder names (``__anything__``), unknown function calls.
3. Compiles the AST into a regular Python callable that operates on a
   dict of ``pandas.Series`` (one per allowed column) plus a fixed set
   of helper functions (rolling mean / std / shift / diff / log / abs).

This is the only place in the project where a string is allowed to
become executable logic — keep its surface very small and very well
tested.

Usage
-----
::

    spec = SafeExpressionSpec(
        expression="(close / mean(close, 20)) - 1",
        allowed_inputs={"close", "high", "low", "open", "volume"},
    )
    fn = compile_safe_expression(spec)
    series = fn({"close": df["close"], ...})
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Mapping, Set

import numpy as np
import pandas as pd


# =====================================================================
# Whitelists
# =====================================================================

# Default OHLCV columns the evaluator will expose. Sub-callers can pass a
# narrower set via ``SafeExpressionSpec.allowed_inputs``.
DEFAULT_ALLOWED_INPUTS: FrozenSet[str] = frozenset(
    {"open", "high", "low", "close", "volume", "amount", "pct_chg",
     "ma5", "ma10", "ma20", "volume_ratio"}
)


def _safe_log(x):
    return np.log(x.replace(0, np.nan)) if isinstance(x, pd.Series) else np.log(x)


def _safe_div(a, b):
    if isinstance(b, pd.Series):
        return a / b.replace(0, np.nan)
    return a / b if b != 0 else float("nan")


def _rolling_mean(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(int(n), min_periods=max(int(n) // 2, 1)).mean()


def _rolling_std(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(int(n), min_periods=max(int(n) // 2, 1)).std()


def _shift(x: pd.Series, n: int) -> pd.Series:
    return x.shift(int(n))


def _diff(x: pd.Series, n: int = 1) -> pd.Series:
    return x.diff(int(n))


def _pct_change(x: pd.Series, n: int = 1) -> pd.Series:
    return x.pct_change(int(n))


def _zscore(x: pd.Series, n: int) -> pd.Series:
    m = _rolling_mean(x, n)
    s = _rolling_std(x, n)
    return (x - m) / s.replace(0, np.nan)


# Whitelisted helpers callable from expressions.
SAFE_FUNCTIONS: Dict[str, Callable] = {
    "mean": _rolling_mean,
    "std": _rolling_std,
    "lag": _shift,
    "shift": _shift,
    "diff": _diff,
    "pct_change": _pct_change,
    "zscore": _zscore,
    "log": _safe_log,
    "abs": (lambda x: x.abs() if isinstance(x, pd.Series) else abs(x)),
    "max": (lambda *args: pd.concat(list(args), axis=1).max(axis=1) if all(isinstance(a, pd.Series) for a in args) else max(*args)),
    "min": (lambda *args: pd.concat(list(args), axis=1).min(axis=1) if all(isinstance(a, pd.Series) for a in args) else min(*args)),
    "div": _safe_div,
}


# AST node types we accept. Anything else raises.
ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Constant,
    ast.Name,
    ast.Call,
    ast.Load,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


# =====================================================================
# Exceptions
# =====================================================================

class UnsafeExpressionError(ValueError):
    """Raised when a factor expression contains forbidden constructs.

    The error message intentionally describes *what* was rejected and
    *which node*, so AI-assisted authors can iterate.
    """


# =====================================================================
# Spec + compile
# =====================================================================

@dataclass(frozen=True)
class SafeExpressionSpec:
    """Inputs needed to compile a safe expression.

    Attributes:
        expression: The user-supplied formula string.
        allowed_inputs: Names referencable as bare identifiers in
            ``expression`` (typically OHLCV columns).
        max_length: Reject expressions longer than this many chars.
            Cheap defense against absurdly long inputs.
    """
    expression: str
    allowed_inputs: FrozenSet[str] = field(default=DEFAULT_ALLOWED_INPUTS)
    max_length: int = 512


def _validate_node(node: ast.AST, spec: SafeExpressionSpec) -> None:
    """Recursively walk the AST and reject anything not whitelisted."""
    if not isinstance(node, ALLOWED_NODES):
        raise UnsafeExpressionError(
            f"Forbidden AST node: {type(node).__name__}. "
            f"Only basic arithmetic, comparisons, identifiers, and "
            f"whitelisted function calls are allowed."
        )

    if isinstance(node, ast.Name):
        # Reject dunder identifiers and anything not on the whitelist.
        if node.id.startswith("_"):
            raise UnsafeExpressionError(
                f"Forbidden identifier: {node.id!r} (leading underscore)"
            )
        allowed = set(spec.allowed_inputs) | set(SAFE_FUNCTIONS.keys())
        if node.id not in allowed:
            raise UnsafeExpressionError(
                f"Unknown identifier: {node.id!r}. "
                f"Only OHLCV columns ({sorted(spec.allowed_inputs)}) and "
                f"whitelisted helpers ({sorted(SAFE_FUNCTIONS.keys())}) "
                f"may be referenced."
            )

    if isinstance(node, ast.Call):
        # Calls must target a bare Name in SAFE_FUNCTIONS — never an attribute,
        # subscript, or dynamically-resolved expression.
        func = node.func
        if not isinstance(func, ast.Name):
            raise UnsafeExpressionError(
                "Forbidden call target: only direct calls to whitelisted "
                "functions are allowed (no attribute or subscript calls)."
            )
        if func.id not in SAFE_FUNCTIONS:
            raise UnsafeExpressionError(
                f"Forbidden function: {func.id!r}. "
                f"Allowed helpers: {sorted(SAFE_FUNCTIONS.keys())}"
            )
        # No keyword arguments allowed (kwargs could smuggle attribute access).
        if node.keywords:
            raise UnsafeExpressionError(
                "Keyword arguments are not allowed in safe expressions."
            )

    for child in ast.iter_child_nodes(node):
        _validate_node(child, spec)


def parse_safe_expression(spec: SafeExpressionSpec) -> ast.Expression:
    """Parse + validate; return the AST or raise ``UnsafeExpressionError``."""
    text = spec.expression.strip()
    if not text:
        raise UnsafeExpressionError("Expression is empty.")
    if len(text) > spec.max_length:
        raise UnsafeExpressionError(
            f"Expression too long ({len(text)} > {spec.max_length} chars)."
        )

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"Syntax error: {exc.msg}") from exc

    _validate_node(tree, spec)
    return tree


def compile_safe_expression(spec: SafeExpressionSpec) -> Callable[[Mapping[str, pd.Series]], pd.Series]:
    """Compile to a callable: ``f(columns) -> Series``.

    ``columns`` is a dict like ``{"close": series, "volume": series, ...}``
    keyed by names from ``spec.allowed_inputs``. Helper functions
    (``mean``, ``std``, ...) are injected automatically.

    The compiled callable does NOT use ``eval``: we walk the AST and
    interpret each node with a tiny dispatch.
    """
    tree = parse_safe_expression(spec)

    # Pre-build the binop / unaryop / compare lookups — keeps the
    # interpreter code below short and free of dynamic ``getattr``.
    BINOPS = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: _safe_div(a, b),
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a ** b,
    }
    UNOPS = {
        ast.UAdd: lambda x: +x,
        ast.USub: lambda x: -x,
        ast.Not: lambda x: ~x if isinstance(x, pd.Series) else (not x),
    }
    CMPOPS = {
        ast.Eq: lambda a, b: a == b,
        ast.NotEq: lambda a, b: a != b,
        ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b,
        ast.Gt: lambda a, b: a > b,
        ast.GtE: lambda a, b: a >= b,
    }

    def _eval(node: ast.AST, env: Mapping[str, object]):
        if isinstance(node, ast.Expression):
            return _eval(node.body, env)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in SAFE_FUNCTIONS:
                return SAFE_FUNCTIONS[node.id]
            # Should already have been caught in validation.
            raise UnsafeExpressionError(f"Unknown identifier at runtime: {node.id!r}")
        if isinstance(node, ast.BinOp):
            return BINOPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
        if isinstance(node, ast.UnaryOp):
            return UNOPS[type(node.op)](_eval(node.operand, env))
        if isinstance(node, ast.BoolOp):
            values = [_eval(v, env) for v in node.values]
            if isinstance(node.op, ast.And):
                result = values[0]
                for v in values[1:]:
                    result = result & v
                return result
            else:  # Or
                result = values[0]
                for v in values[1:]:
                    result = result | v
                return result
        if isinstance(node, ast.Compare):
            left = _eval(node.left, env)
            result = None
            current = left
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator, env)
                step = CMPOPS[type(op)](current, right)
                result = step if result is None else (result & step)
                current = right
            return result
        if isinstance(node, ast.Call):
            fn = SAFE_FUNCTIONS[node.func.id]  # type: ignore[union-attr]
            args = [_eval(a, env) for a in node.args]
            return fn(*args)
        raise UnsafeExpressionError(
            f"Unhandled AST node at runtime: {type(node).__name__}"
        )

    def runner(columns: Mapping[str, pd.Series]) -> pd.Series:
        # Reject callers passing inputs we never validated — narrow surface.
        unexpected = set(columns) - set(spec.allowed_inputs)
        if unexpected:
            raise UnsafeExpressionError(
                f"Disallowed inputs supplied at runtime: {sorted(unexpected)}"
            )
        env: Dict[str, object] = dict(columns)
        out = _eval(tree, env)
        if isinstance(out, pd.Series):
            return out
        # Scalar / array results are unusual but we allow them — caller
        # may broadcast as needed.
        return pd.Series([out])

    return runner
