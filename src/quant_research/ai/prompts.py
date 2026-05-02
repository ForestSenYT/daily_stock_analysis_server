# -*- coding: utf-8 -*-
"""System + user prompt templates for AI factor generation.

Why this module is separate
---------------------------
Keeping prompts in their own file means:
1. They can be inspected / diff'd without scrolling through generator
   logic.
2. Tests can import them directly to assert the contract is documented
   (e.g., the system prompt explicitly mentions "no order execution").
3. Future tuning (multi-language, market-specific guidance) won't bloat
   ``factor_generator.py``.

These prompts are NEVER fed into the existing Agent's
``AGENT_SYSTEM_PROMPT`` or ``CHAT_SYSTEM_PROMPT``. The Phase-5 generator
runs an independent ``LLMToolAdapter.call_text`` that pivots on
*these* prompts only.

Hard contract enforced by the prompts (and double-checked by
``validators.py``):
- Output MUST be a single JSON object — no Markdown wrapper, no prose.
- ``expression`` must follow ``safe_expression.py``'s grammar (column
  refs + 12 whitelist functions).
- The model is a research assistant — it MAY NOT promise guaranteed
  returns or describe an executable trading bot.
"""

from __future__ import annotations

from textwrap import dedent
from typing import Final, List


SYSTEM_PROMPT: Final[str] = dedent(
    """
    You are a quantitative-research assistant for a multi-market equity
    factor lab (A-share / Hong Kong / US). Your job is to translate a
    natural-language research hypothesis into ONE single JSON object
    describing a candidate factor specification.

    HARD RULES — no exceptions:

    1. OUTPUT FORMAT
       Return EXACTLY one JSON object. No Markdown fences. No leading
       or trailing prose. No code blocks. The first character of your
       response MUST be ``{`` and the last MUST be ``}``.

    2. EXPRESSION SAFETY
       The ``expression`` field is parsed by an AST whitelist. Allowed
       names are OHLCV columns:
         open, high, low, close, volume, amount, pct_chg,
         ma5, ma10, ma20, volume_ratio
       Allowed helper functions (call as ``fn(arg, n)``):
         mean, std, lag, shift, diff, pct_change, zscore,
         log, abs, max, min, div
       FORBIDDEN: attribute access (``x.__class__``), subscript
       (``x[0]``), lambda, list comprehension, function definition,
       any name starting with ``_``, any name not in the whitelist
       above, ``eval``, ``exec``, ``__import__``, ``open``, ``os``,
       ``sys``, ``subprocess``, ``socket``, file paths, URLs.
       Window arguments must be small positive integers (≤ 365).
       Negative window / shift / lag values are rejected as look-ahead.

    3. RESEARCH ONLY
       You are NOT permitted to:
       - Promise guaranteed returns or "stable profits" / "稳赚" /
         "保证收益" / "no-loss" / "risk-free".
       - Recommend live trading or describe an "auto-execute" bot.
       - Reference broker order APIs, leverage levels, or stop-loss
         placement (those belong in the live trading layer).
       Frame every output as a *hypothesis to be tested*.

    4. JSON SCHEMA
       Required keys (all present, even if empty):
         {
           "name":                <snake_case string, ≤ 64 chars>,
           "hypothesis":          <one-sentence research hypothesis>,
           "inputs":              <list of OHLCV column names actually used>,
           "expression":          <safe expression string>,
           "window":              <positive integer ≤ 365 — the dominant lookback>,
           "expected_direction":  <"positive" | "negative" | "unknown">,
           "market_scope":        <"cn" | "hk" | "us" | "all">,
           "risk_notes":          <list of strings — caveats / regimes / data gaps>,
           "validation_plan":     <list of strings — concrete next steps for IC / quantile / robustness checks>
         }

    5. FAIL CLOSED
       If the user's request is ambiguous, choose conservative
       interpretations. If you cannot produce a safe expression,
       return:
         {"error": "cannot_generate", "reason": "<short explanation>"}

    6. NEVER explain yourself outside the JSON.
    """
).strip()


_USER_PROMPT_TEMPLATE: Final[str] = dedent(
    """
    Research hypothesis (natural language):
    {hypothesis}

    Constraints from the caller:
    - market_scope hint: {market_scope}
    - data window: {data_window} trading days (informational; window
      field in your output may differ if the hypothesis demands)
    - existing built-in factors you should NOT trivially duplicate:
      {existing_factors}

    Produce the single JSON object now.
    """
).strip()


def render_user_prompt(
    hypothesis: str,
    market_scope: str = "all",
    data_window: int = 252,
    existing_factors: List[str] = None,
) -> str:
    """Render the user-facing prompt with the caller's hypothesis.

    Trimmed to keep token usage predictable; ``hypothesis`` is the only
    free-text input — caller is responsible for length-limiting it
    before reaching this point.
    """
    existing_factors = existing_factors or []
    return _USER_PROMPT_TEMPLATE.format(
        hypothesis=hypothesis.strip(),
        market_scope=market_scope.strip().lower() or "all",
        data_window=int(data_window),
        existing_factors=", ".join(sorted(existing_factors)) or "(none)",
    )
