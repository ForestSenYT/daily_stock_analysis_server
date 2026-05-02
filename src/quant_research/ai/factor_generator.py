# -*- coding: utf-8 -*-
"""LLM-driven FactorSpec generator.

This is the only entry point that lets a natural-language hypothesis
become a FactorSpec the rest of the lab can run. The contract is
narrow on purpose:

- Inputs: a free-text hypothesis, an optional market scope hint, and
  the list of built-in factor ids the LLM should not trivially copy.
- Output: either a fully-validated FactorSpec dict (already stripped
  of unsafe expressions / dangerous marketing phrases) or a
  ``FactorGenerationError`` with a stable machine-readable code.

Why we reuse ``LLMToolAdapter`` instead of calling provider SDKs
directly: the existing adapter already handles LiteLLM Router fallback,
custom pricing, thinking-mode opt-in, and a bunch of provider quirks.
Spinning up a parallel client would diverge under maintenance.

The generator never touches the existing AGENT prompts; it builds its
own ``system`` + ``user`` pair from ``prompts.py``. The agent's chat
history / skill state is not loaded here — Phase 5 is intentionally
stateless.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.llm_adapter import LLMResponse, LLMToolAdapter
from src.config import get_config
from src.quant_research.ai.prompts import SYSTEM_PROMPT, render_user_prompt
from src.quant_research.ai.validators import (
    FactorGenerationError,
    FactorSpecValidation,
    parse_and_validate,
)

logger = logging.getLogger(__name__)


# Tight knobs for Phase 5. The factor JSON is small (≤ ~1.2 KB) so we do
# NOT need a large completion budget; a tight ceiling keeps the worst-case
# bill bounded even if the LLM goes verbose.
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_HYPOTHESIS_LEN = 1000
MAX_EXISTING_FACTORS = 32


# =====================================================================
# Result type
# =====================================================================

@dataclass
class FactorGenerationOutcome:
    """What the API layer ultimately serialises.

    ``spec`` is the validated FactorSpec dict (snake_case name,
    AST-clean expression, scrubbed of forbidden marketing phrases).
    ``raw_response`` is kept only when ``include_raw=True`` — useful for
    debugging from the SPA without polluting the normal response.
    """
    spec: Dict[str, Any]
    model: str
    provider: str
    usage: Dict[str, Any] = field(default_factory=dict)
    expression_node_count: int = 0
    elapsed_ms: float = 0.0
    raw_response: Optional[str] = None


# =====================================================================
# Generator
# =====================================================================

class FactorGenerator:
    """LLM → FactorSpec pipeline. Holds an ``LLMToolAdapter`` so repeated
    calls within one request (e.g. ``generate_and_evaluate``) reuse the
    initialised Router instead of re-handshaking on every call."""

    def __init__(self, adapter: Optional[LLMToolAdapter] = None) -> None:
        self._adapter = adapter

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def generate(
        self,
        hypothesis: str,
        *,
        market_scope: str = "all",
        data_window: int = 252,
        existing_factors: Optional[List[str]] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        include_raw: bool = False,
    ) -> FactorGenerationOutcome:
        """Run the LLM, parse its response, and validate the FactorSpec.

        Raises ``FactorGenerationError`` for any soft failure (LLM
        unavailable, non-JSON response, dangerous expression, ...) so
        the API layer can map ``error.code`` → 400 / 503 deterministically.
        """
        text = self._coerce_hypothesis(hypothesis)
        existing = self._coerce_existing(existing_factors)

        adapter = self._get_adapter()
        if not adapter.is_available:
            # The endpoint translates "llm_unavailable" → 503 so the FE
            # can render "configure an LLM in settings first".
            raise FactorGenerationError(
                "No LLM is configured for the Quant Research Lab. "
                "Configure LITELLM_MODEL / LLM_CHANNELS / provider keys.",
                code="llm_unavailable",
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": render_user_prompt(
                    hypothesis=text,
                    market_scope=market_scope,
                    data_window=data_window,
                    existing_factors=existing,
                ),
            },
        ]

        started = time.monotonic()
        try:
            response: LLMResponse = adapter.call_text(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as exc:
            logger.exception("FactorGenerator LLM call raised: %s", exc)
            raise FactorGenerationError(
                f"LLM call failed: {type(exc).__name__}",
                code="llm_call_failed",
            ) from exc
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if response.provider == "error":
            # LLMToolAdapter already logs the underlying provider error;
            # we fold it into a single user-visible code.
            raise FactorGenerationError(
                "All configured LLM models failed to respond. "
                "Check the server logs for the provider error.",
                code="llm_call_failed",
            )

        raw = (response.content or "").strip()
        if not raw:
            raise FactorGenerationError(
                "LLM returned an empty response.",
                code="empty_response",
            )

        # validators.parse_and_validate is the single, authoritative
        # gate. It raises FactorGenerationError on any layer that fails;
        # we propagate as-is so the API can echo the stable ``code``.
        validation: FactorSpecValidation = parse_and_validate(raw)

        return FactorGenerationOutcome(
            spec=validation.spec,
            model=response.model or "",
            provider=response.provider or "",
            usage=dict(response.usage or {}),
            expression_node_count=validation.expression_node_count,
            elapsed_ms=elapsed_ms,
            raw_response=raw if include_raw else None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_adapter(self) -> LLMToolAdapter:
        if self._adapter is None:
            # Construct lazily so importing this module doesn't try to
            # touch a Config that may not be initialised yet (e.g. in
            # unit-test imports).
            self._adapter = LLMToolAdapter(get_config())
        return self._adapter

    @staticmethod
    def _coerce_hypothesis(hypothesis: str) -> str:
        if not isinstance(hypothesis, str):
            raise FactorGenerationError(
                "hypothesis must be a string.",
                code="wrong_field_type", field="hypothesis",
            )
        text = hypothesis.strip()
        if not text:
            raise FactorGenerationError(
                "hypothesis must not be empty.",
                code="empty_field", field="hypothesis",
            )
        if len(text) > MAX_HYPOTHESIS_LEN:
            raise FactorGenerationError(
                f"hypothesis too long ({len(text)} > {MAX_HYPOTHESIS_LEN}).",
                code="field_too_long", field="hypothesis",
            )
        return text

    @staticmethod
    def _coerce_existing(existing_factors: Optional[List[str]]) -> List[str]:
        if not existing_factors:
            return []
        if not isinstance(existing_factors, list):
            raise FactorGenerationError(
                "existing_factors must be a list of strings.",
                code="wrong_field_type", field="existing_factors",
            )
        if len(existing_factors) > MAX_EXISTING_FACTORS:
            raise FactorGenerationError(
                f"existing_factors too long "
                f"({len(existing_factors)} > {MAX_EXISTING_FACTORS}).",
                code="list_too_long", field="existing_factors",
            )
        cleaned: List[str] = []
        for item in existing_factors:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text:
                cleaned.append(text)
        return cleaned
