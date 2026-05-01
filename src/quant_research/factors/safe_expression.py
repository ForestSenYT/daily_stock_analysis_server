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
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Mapping, Optional

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

DEFAULT_MAX_NODES = 80
DEFAULT_MAX_DEPTH = 18
DEFAULT_MAX_ABS_CONSTANT = 1_000_000
DEFAULT_MAX_POW_EXPONENT = 8
DEFAULT_MAX_WINDOW = 365
DEFAULT_MAX_CALL_ARGS = 5


def _safe_log(x):
    return np.log(x.replace(0, np.nan)) if isinstance(x, pd.Series) else np.log(x)


def _safe_div(a, b):
    if isinstance(b, pd.Series):
        return a / b.replace(0, np.nan)
    return a / b if b != 0 else float("nan")


def _coerce_window(n, *, min_value: int = 1) -> int:
    if isinstance(n, bool):
        raise UnsafeExpressionError("Window argument must be an integer, not bool.")
    if isinstance(n, float):
        if not n.is_integer():
            raise UnsafeExpressionError("Window argument must be an integer.")
        value = int(n)
    else:
        try:
            value = int(n)
        except (TypeError, ValueError) as exc:
            raise UnsafeExpressionError("Window argument must be an integer.") from exc
    if value < min_value or value > DEFAULT_MAX_WINDOW:
        raise UnsafeExpressionError(
            f"Window argument out of range: {value} "
            f"(allowed {min_value}..{DEFAULT_MAX_WINDOW})."
        )
    return value


def _rolling_mean(x: pd.Series, n: int) -> pd.Series:
    window = _coerce_window(n, min_value=1)
    return x.rolling(window, min_periods=max(window // 2, 1)).mean()


def _rolling_std(x: pd.Series, n: int) -> pd.Series:
    window = _coerce_window(n, min_value=1)
    return x.rolling(window, min_periods=max(window // 2, 1)).std()


def _shift(x: pd.Series, n: int) -> pd.Series:
    return x.shift(_coerce_window(n, min_value=0))


def _diff(x: pd.Series, n: int = 1) -> pd.Series:
    return x.diff(_coerce_window(n, min_value=0))


def _pct_change(x: pd.Series, n: int = 1) -> pd.Series:
    return x.pct_change(_coerce_window(n, min_value=0))


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
    max_nodes: int = DEFAULT_MAX_NODES
    max_depth: int = DEFAULT_MAX_DEPTH
    max_abs_constant: float = DEFAULT_MAX_ABS_CONSTANT
    max_pow_exponent: int = DEFAULT_MAX_POW_EXPONENT
    max_window: int = DEFAULT_MAX_WINDOW
    max_call_args: int = DEFAULT_MAX_CALL_ARGS


def _node_count(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _node_depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    if not children:
        return 1
    return 1 + max(_node_depth(child) for child in children)


def _static_number(node: ast.AST) -> Optional[float]:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _static_number(node.operand)
        if operand is None:
            return None
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)
    ):
        left = _static_number(node.left)
        right = _static_number(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            return None
        return left // right
    return None


def _static_int(node: ast.AST) -> Optional[int]:
    value = _static_number(node)
    if value is None or not math.isfinite(value) or not float(value).is_integer():
        return None
    return int(value)


def _validate_constant(node: ast.Constant, spec: SafeExpressionSpec) -> None:
    value = node.value
    if value is None or isinstance(value, bool):
        return
    if not isinstance(value, (int, float)):
        raise UnsafeExpressionError(
            f"Forbidden constant type: {type(value).__name__}. "
            "Only finite numbers, booleans, and None are allowed."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise UnsafeExpressionError("Numeric constants must be finite.")
    if abs(float(value)) > spec.max_abs_constant:
        raise UnsafeExpressionError(
            f"Numeric constant too large: {value!r} "
            f"(max abs {spec.max_abs_constant})."
        )


def _validate_call_shape(node: ast.Call, spec: SafeExpressionSpec) -> None:
    func = node.func
    if not isinstance(func, ast.Name):
        return
    name = func.id

    fixed_arity = {
        "mean": 2,
        "std": 2,
        "lag": 2,
        "shift": 2,
        "zscore": 2,
        "log": 1,
        "abs": 1,
        "div": 2,
    }
    if name in fixed_arity and len(node.args) != fixed_arity[name]:
        raise UnsafeExpressionError(
            f"Function {name!r} expects {fixed_arity[name]} positional arguments."
        )
    if name in {"diff", "pct_change"} and len(node.args) not in {1, 2}:
        raise UnsafeExpressionError(
            f"Function {name!r} expects 1 or 2 positional arguments."
        )
    if name in {"max", "min"} and not (1 <= len(node.args) <= spec.max_call_args):
        raise UnsafeExpressionError(
            f"Function {name!r} expects 1..{spec.max_call_args} arguments."
        )

    rolling_windows = {"mean", "std", "zscore"}
    causal_windows = {"lag", "shift", "diff", "pct_change"}
    if name in rolling_windows or (name in causal_windows and len(node.args) >= 2):
        window_node = node.args[1]
        window = _static_int(window_node)
        min_value = 1 if name in rolling_windows else 0
        if window is None:
            raise UnsafeExpressionError(
                f"Function {name!r} window must be a static integer."
            )
        if window < min_value or window > spec.max_window:
            raise UnsafeExpressionError(
                f"Function {name!r} window out of range: {window} "
                f"(allowed {min_value}..{spec.max_window})."
            )


def _validate_node(node: ast.AST, spec: SafeExpressionSpec) -> None:
    """Recursively walk the AST and reject anything not whitelisted."""
    if not isinstance(node, ALLOWED_NODES):
        raise UnsafeExpressionError(
            f"Forbidden AST node: {type(node).__name__}. "
            f"Only basic arithmetic, comparisons, identifiers, and "
            f"whitelisted function calls are allowed."
        )

    if isinstance(node, ast.Constant):
        _validate_constant(node, spec)

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

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        exponent = _static_int(node.right)
        if exponent is None:
            raise UnsafeExpressionError("Power exponent must be a static integer.")
        if exponent < 0 or exponent > spec.max_pow_exponent:
            raise UnsafeExpressionError(
                f"Power exponent out of range: {exponent} "
                f"(allowed 0..{spec.max_pow_exponent})."
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
        _validate_call_shape(node, spec)

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

    node_count = _node_count(tree)
    if node_count > spec.max_nodes:
        raise UnsafeExpressionError(
            f"Expression has too many AST nodes ({node_count} > {spec.max_nodes})."
        )
    depth = _node_depth(tree)
    if depth > spec.max_depth:
        raise UnsafeExpressionError(
            f"Expression AST is too deep ({depth} > {spec.max_depth})."
        )

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

    def _safe_pow(a, b):
        if isinstance(b, pd.Series):
            raise UnsafeExpressionError("Power exponent cannot be a Series.")
        exponent = _coerce_window(b, min_value=0)
        if exponent > spec.max_pow_exponent:
            raise UnsafeExpressionError(
                f"Power exponent out of range at runtime: {exponent} "
                f"(allowed 0..{spec.max_pow_exponent})."
            )
        return a ** exponent

    # Pre-build the binop / unaryop / compare lookups — keeps the
    # interpreter code below short and free of dynamic ``getattr``.
    BINOPS = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: _safe_div(a, b),
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: _safe_pow,
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
