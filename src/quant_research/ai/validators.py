# -*- coding: utf-8 -*-
"""Validation pipeline for AI-generated FactorSpec JSON.

Three layers, applied in order. Any layer rejecting → caller surfaces
a structured 400 (never a 500) and the LLM output is **discarded**:

1. ``parse_json_strict``
   Reject the response if it isn't a single JSON object. We are very
   strict about wrappers (Markdown fences, leading prose, multiple
   objects) because allowing them widens the prompt-injection
   surface — the system prompt explicitly forbids them.

2. ``validate_factor_spec_shape``
   JSON Schema-shaped check on the dict: required keys present, types
   correct, lengths bounded. We don't pull in jsonschema because we
   only need 9 fields and the rules are easier to inline.

3. ``validate_factor_spec_safety``
   Combined safety pass:
     a. ``expression`` is parsed by ``safe_expression.parse_safe_expression``
        — same AST whitelist that built-in expressions use, so an AI
        cannot smuggle a more permissive grammar in.
     b. Free-text fields (name / hypothesis / risk_notes /
        validation_plan) are scanned for forbidden marketing phrases
        ("guaranteed return" / "稳赚" / "auto-execute trade" etc.).

The result is a clean ``Dict[str, Any]`` ready to feed into the
existing ``FactorEvaluationRequest`` path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.quant_research.factors.safe_expression import (
    DEFAULT_ALLOWED_INPUTS,
    SafeExpressionSpec,
    UnsafeExpressionError,
    parse_safe_expression,
)


# =====================================================================
# Constants
# =====================================================================

REQUIRED_KEYS: FrozenSet[str] = frozenset({
    "name",
    "hypothesis",
    "inputs",
    "expression",
    "window",
    "expected_direction",
    "market_scope",
    "risk_notes",
    "validation_plan",
})

VALID_DIRECTIONS: FrozenSet[str] = frozenset({"positive", "negative", "unknown"})
VALID_MARKETS: FrozenSet[str] = frozenset({"cn", "hk", "us", "all"})

# Tight caps so a misbehaving / verbose LLM can't bloat the response.
MAX_NAME_LEN = 64
MAX_HYPOTHESIS_LEN = 500
MAX_NOTES = 8
MAX_NOTE_LEN = 240
MAX_INPUTS = 8
MAX_WINDOW = 365

# Phrases the assistant is forbidden from producing. Curated to catch
# guaranteed-return marketing, auto-trading talk, and a small Chinese
# set the prompt also bans. Match is case-insensitive.
DANGEROUS_PHRASES: Tuple[str, ...] = (
    "guaranteed return",
    "guaranteed profit",
    "no loss",
    "no-loss",
    "risk-free",
    "risk free",
    "稳赚",
    "保证收益",
    "保证盈利",
    "确保收益",
    "无风险",
    "稳定盈利",
    "auto execute",
    "auto-execute",
    "auto trade",
    "auto-trade",
    "place orders",
    "send orders",
    "broker api",
    "live trading",
    "实盘自动",
    "自动下单",
    "下单接口",
)

# Pre-compiled for faster repeat scans.
_DANGEROUS_RE = re.compile(
    "|".join(re.escape(p) for p in DANGEROUS_PHRASES),
    re.IGNORECASE,
)


# =====================================================================
# Errors
# =====================================================================

class FactorGenerationError(ValueError):
    """Raised when AI output fails any validation layer.

    ``code`` is machine-readable (used by the API to set the JSON
    ``error`` field); ``field`` points at the offending key when
    relevant.
    """

    def __init__(self, message: str, *, code: str, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass
class FactorSpecValidation:
    """What ``validate_factor_spec_*`` returns when validation passes."""
    spec: Dict[str, Any]
    flagged_phrases: List[str] = field(default_factory=list)
    expression_node_count: int = 0


# =====================================================================
# Layer 1: parse
# =====================================================================

def parse_json_strict(raw: str) -> Dict[str, Any]:
    """Reject anything that isn't a single JSON object.

    Specifically:
    - Strips any leading / trailing whitespace.
    - Rejects Markdown fences (triple-backtick wrappers).
    - Rejects multiple top-level documents.
    - Rejects non-object roots (lists, scalars).
    """
    if not isinstance(raw, str):
        raise FactorGenerationError(
            "LLM response was not a string.", code="invalid_response_type",
        )
    text = raw.strip()
    if not text:
        raise FactorGenerationError(
            "LLM response was empty.", code="empty_response",
        )
    if text.startswith("```") or text.endswith("```"):
        raise FactorGenerationError(
            "LLM response was wrapped in a Markdown code fence; the "
            "system prompt forbids that.",
            code="markdown_wrapper",
        )
    if not text.startswith("{") or not text.endswith("}"):
        raise FactorGenerationError(
            "LLM response was not a single JSON object (must start "
            "with '{' and end with '}').",
            code="not_a_json_object",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactorGenerationError(
            f"LLM response was not valid JSON: {exc.msg} at line {exc.lineno}",
            code="invalid_json",
        ) from exc
    if not isinstance(parsed, dict):
        raise FactorGenerationError(
            f"LLM response top-level type is {type(parsed).__name__}, "
            f"expected object.",
            code="wrong_top_level_type",
        )
    return parsed


# =====================================================================
# Layer 2: shape
# =====================================================================

def _ensure_str(value: Any, *, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise FactorGenerationError(
            f"{field!r} must be a string, got {type(value).__name__}.",
            code="wrong_field_type", field=field,
        )
    text = value.strip()
    if not text:
        raise FactorGenerationError(
            f"{field!r} must not be empty.",
            code="empty_field", field=field,
        )
    if len(text) > max_len:
        raise FactorGenerationError(
            f"{field!r} too long ({len(text)} > {max_len}).",
            code="field_too_long", field=field,
        )
    return text


def _ensure_int(value: Any, *, field: str, min_value: int, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        # bool is subclass of int — exclude explicitly.
        raise FactorGenerationError(
            f"{field!r} must be an integer.",
            code="wrong_field_type", field=field,
        )
    if value < min_value or value > max_value:
        raise FactorGenerationError(
            f"{field!r} out of range ({value} not in [{min_value}, {max_value}]).",
            code="field_out_of_range", field=field,
        )
    return value


def _ensure_string_list(
    value: Any, *, field: str, max_items: int, max_item_len: int,
    allowed: Optional[FrozenSet[str]] = None,
) -> List[str]:
    if not isinstance(value, list):
        raise FactorGenerationError(
            f"{field!r} must be a list, got {type(value).__name__}.",
            code="wrong_field_type", field=field,
        )
    if len(value) > max_items:
        raise FactorGenerationError(
            f"{field!r} too many entries ({len(value)} > {max_items}).",
            code="list_too_long", field=field,
        )
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise FactorGenerationError(
                f"{field!r}[{i}] must be a string.",
                code="wrong_field_type", field=field,
            )
        text = item.strip()
        if not text:
            continue  # silently drop empty entries
        if len(text) > max_item_len:
            raise FactorGenerationError(
                f"{field!r}[{i}] too long ({len(text)} > {max_item_len}).",
                code="list_item_too_long", field=field,
            )
        if allowed is not None and text not in allowed:
            raise FactorGenerationError(
                f"{field!r}[{i}]={text!r} not in whitelist {sorted(allowed)}.",
                code="value_not_allowed", field=field,
            )
        out.append(text)
    return out


def validate_factor_spec_shape(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Type / required / length checks. Returns a normalised copy."""
    # First check for the optional "cannot_generate" path.
    if set(spec.keys()) >= {"error", "reason"} and isinstance(spec.get("error"), str):
        raise FactorGenerationError(
            f"LLM declined to generate: {spec.get('reason', '(no reason)')}",
            code="cannot_generate",
        )

    missing = REQUIRED_KEYS - set(spec.keys())
    if missing:
        raise FactorGenerationError(
            f"Missing required keys: {sorted(missing)}",
            code="missing_keys",
        )

    out: Dict[str, Any] = {}
    out["name"] = _ensure_str(spec["name"], field="name", max_len=MAX_NAME_LEN)
    if not re.match(r"^[a-z0-9_]+$", out["name"]):
        raise FactorGenerationError(
            f"name must be snake_case ASCII (got {out['name']!r}).",
            code="invalid_name", field="name",
        )
    out["hypothesis"] = _ensure_str(
        spec["hypothesis"], field="hypothesis", max_len=MAX_HYPOTHESIS_LEN,
    )
    out["inputs"] = _ensure_string_list(
        spec["inputs"], field="inputs",
        max_items=MAX_INPUTS, max_item_len=24,
        allowed=DEFAULT_ALLOWED_INPUTS,
    )
    if not out["inputs"]:
        raise FactorGenerationError(
            "inputs must list at least one OHLCV column actually used "
            "by the expression.",
            code="empty_field", field="inputs",
        )
    out["expression"] = _ensure_str(
        spec["expression"], field="expression", max_len=512,
    )
    out["window"] = _ensure_int(
        spec["window"], field="window", min_value=1, max_value=MAX_WINDOW,
    )

    direction = _ensure_str(
        spec["expected_direction"],
        field="expected_direction", max_len=16,
    ).lower()
    if direction not in VALID_DIRECTIONS:
        raise FactorGenerationError(
            f"expected_direction must be one of {sorted(VALID_DIRECTIONS)}.",
            code="value_not_allowed", field="expected_direction",
        )
    out["expected_direction"] = direction

    market = _ensure_str(spec["market_scope"], field="market_scope", max_len=8).lower()
    if market not in VALID_MARKETS:
        raise FactorGenerationError(
            f"market_scope must be one of {sorted(VALID_MARKETS)}.",
            code="value_not_allowed", field="market_scope",
        )
    out["market_scope"] = market

    out["risk_notes"] = _ensure_string_list(
        spec["risk_notes"], field="risk_notes",
        max_items=MAX_NOTES, max_item_len=MAX_NOTE_LEN,
    )
    out["validation_plan"] = _ensure_string_list(
        spec["validation_plan"], field="validation_plan",
        max_items=MAX_NOTES, max_item_len=MAX_NOTE_LEN,
    )

    return out


# =====================================================================
# Layer 3: safety
# =====================================================================

def scan_dangerous_phrases(spec: Dict[str, Any]) -> List[str]:
    """Return a list of phrases found across all free-text fields.

    Empty list = clean. Caller raises ``FactorGenerationError`` only
    if non-empty (so we surface every hit at once, instead of one
    round-trip per phrase).
    """
    hits: List[str] = []
    text_fields = [
        spec.get("name", ""),
        spec.get("hypothesis", ""),
    ]
    for note in spec.get("risk_notes") or []:
        text_fields.append(note)
    for step in spec.get("validation_plan") or []:
        text_fields.append(step)
    for blob in text_fields:
        if not isinstance(blob, str):
            continue
        for match in _DANGEROUS_RE.findall(blob):
            hits.append(match.lower())
    # Dedup while preserving order.
    seen: set = set()
    deduped: List[str] = []
    for phrase in hits:
        if phrase not in seen:
            seen.add(phrase)
            deduped.append(phrase)
    return deduped


def validate_factor_spec_safety(spec: Dict[str, Any]) -> FactorSpecValidation:
    """Run ``safe_expression`` parse + dangerous-phrase scan.

    Caller is responsible for having already passed ``spec`` through
    ``validate_factor_spec_shape``.
    """
    flagged = scan_dangerous_phrases(spec)
    if flagged:
        raise FactorGenerationError(
            f"FactorSpec contains forbidden marketing phrases: {flagged}",
            code="dangerous_phrase",
        )

    sub_spec = SafeExpressionSpec(
        expression=spec["expression"],
        allowed_inputs=DEFAULT_ALLOWED_INPUTS,
    )
    try:
        tree = parse_safe_expression(sub_spec)
    except UnsafeExpressionError as exc:
        raise FactorGenerationError(
            f"Generated expression failed AST whitelist: {exc}",
            code="unsafe_expression", field="expression",
        ) from exc

    # Light sanity: the expression should mention at least one of the
    # symbols listed in ``inputs``. We use a substring match — the
    # full AST already guarantees only whitelist names appear, so this
    # only flags the rare case of "inputs vs expression" drift.
    expr_text = spec["expression"]
    if not any(col in expr_text for col in spec["inputs"]):
        raise FactorGenerationError(
            "expression does not reference any of the declared inputs.",
            code="inputs_expression_mismatch", field="inputs",
        )

    # Approximate node count for diagnostics — mirror the cap used in
    # the Phase-2 evaluator so callers can spot expressions that are
    # near the limit.
    import ast as _ast
    node_count = sum(1 for _ in _ast.walk(tree))

    return FactorSpecValidation(
        spec=spec,
        flagged_phrases=[],
        expression_node_count=node_count,
    )


# =====================================================================
# Public composite
# =====================================================================

def parse_and_validate(raw: str) -> FactorSpecValidation:
    """One-call helper. Goes through all three layers; raises
    ``FactorGenerationError`` on any failure with a stable ``code``."""
    parsed = parse_json_strict(raw)
    shaped = validate_factor_spec_shape(parsed)
    return validate_factor_spec_safety(shaped)
