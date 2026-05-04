# -*- coding: utf-8 -*-
"""
TechnicalAgent — technical & price analysis specialist.

Responsible for:
- Fetching realtime quotes and historical K-line data
- Running technical indicators (trend, MA, volume, pattern)
- Producing a structured opinion on trend/momentum/support-resistance
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class TechnicalAgent(BaseAgent):
    agent_name = "technical"
    max_steps = 6
    tool_names = [
        "get_realtime_quote",
        "get_daily_history",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "analyze_pattern",
        "get_chip_distribution",
        "get_analysis_context",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        skills = ""
        if self.skill_instructions:
            skills = f"\n## Active Trading Skills\n\n{self.skill_instructions}\n"
        baseline = ""
        if self.technical_skill_policy:
            baseline = f"\n{self.technical_skill_policy}\n"

        return f"""\
You are a **Technical Analysis Agent** specialising in Chinese A-shares, \
Hong Kong stocks, and US equities.

Your task: perform a thorough technical analysis of the given stock and \
output a structured JSON opinion.

## Workflow (execute stages in order)
1. Fetch realtime quote + daily history (if not already provided)
2. Run trend analysis (MA alignment, MACD, RSI)
3. Analyse volume and chip distribution
4. Identify chart patterns

## Quant factor snapshot (when provided)
The user message may include a ``[Quant factor snapshot]`` JSON block \
listing pre-computed builtin factors (id, value, expected_direction, \
description). When present:
- Your ``reasoning`` MUST cite **at least 2 distinct factor values** by \
  id+value (e.g. ``rsi_14=64.8``, ``ma_ratio_5_20=+1.8%``, \
  ``volatility_20=0.018``) and align the bullish / bearish read with \
  each factor's ``expected_direction``.
- Prefer factors that are NOT already redundant with MA / RSI \
  (``volatility_20``, ``volume_zscore_20``, ``turnover_or_volume_proxy``, \
  ``macd_histogram``, ``return_5d``) so the analysis adds signal beyond \
  what the trend tools already produce.
- Treat factor values as observational evidence, not forecasts. Don't \
  fabricate IDs or values — if a factor isn't in the block, don't \
  reference it.

## Cross-sectional rank (when provided) — MANDATORY when block present
The user message may also include a ``[Cross-sectional quant rank]`` \
JSON block showing how this stock RANKS within its market peer pool \
on each builtin factor (``percentile`` 0..100, ``interpretation`` tags \
the bullish / bearish read).

**When this block is present, your `reasoning` MUST include the \
literal substring ``percentile`` (English) or ``分位`` (Chinese), citing \
at least 2 distinct factor ranks** in this exact format:
- English: ``"<stock> ranks <N>th percentile on <factor_id> (<interpretation>)"``
- Chinese: ``"<股票>在<因子>上排同业<N>分位（<interpretation>）"``

Examples (fill in real values from the block — do NOT invent):
- ``"AAPL ranks 92nd percentile on rsi_14 (high — factor signals bearish)"``
- ``"AAPL在volatility_20上排同业 25 分位（low — factor signals bullish）"``

**Why this rule is hard**: peer-relative context is the only signal \
that distinguishes "AAPL RSI=70" (looks high) from "AAPL is mid-pack \
on RSI vs peers" (means nothing standalone) — the absolute value alone \
is not enough.

Pool size is small (~20). Treat ranks as peer-relative context, not \
statistically rigorous quantiles. If the block is absent, ignore this \
section entirely.

{baseline}
{skills}
## Output Format
Return **only** a JSON object (no markdown fences):
{{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary that cites ≥2 quant factor id+value pairs when the snapshot was provided",
  "key_levels": {{
    "support": <float>,
    "resistance": <float>,
    "stop_loss": <float>
  }},
  "trend_score": 0-100,
  "ma_alignment": "bullish|neutral|bearish",
  "volume_status": "heavy|normal|light",
  "pattern": "<detected pattern or none>"
}}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Perform technical analysis on stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"
        # Inject the pipeline-computed quant factor snapshot so the
        # technical agent's reasoning can cite specific factor values
        # (rsi_14, volatility_20, etc.) rather than re-deriving them
        # via tools. The orchestrator pre-populates this from
        # ``initial_context["quant_signals"]``.
        quant_signals = ctx.get_data("quant_signals")
        if quant_signals:
            parts.append(
                "\n[Quant factor snapshot — each row carries `id`, `value`, "
                "`expected_direction` (positive/negative/unknown for forward "
                "returns), and `description`. Cite at least 2 factor values "
                "in your `reasoning`.]\n"
                f"{json.dumps(quant_signals, ensure_ascii=False)}"
            )
        # Cross-sectional context: this stock's rank within the
        # market peer pool. Lets the agent reason about peer-relative
        # positioning ("AAPL ranks 92nd percentile on rsi_14") rather
        # than just absolute factor values.
        quant_rank = ctx.get_data("quant_research_context")
        if quant_rank:
            parts.append(
                "\n[Cross-sectional quant rank — `percentile` is this "
                "stock's rank (0..100) within a baseline peer pool of "
                "~20 same-market stocks. `interpretation` tags whether "
                "the rank reads bullish/bearish/mid given the factor's "
                "expected direction. Reference at least 1 percentile in "
                "your `reasoning`.]\n"
                f"{json.dumps(quant_rank, ensure_ascii=False)}"
            )
        parts.append("Use your tools to fetch any missing data, then output the JSON opinion.")
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        """Parse the JSON opinion from the LLM response."""
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[TechnicalAgent] failed to parse opinion JSON")
            return None

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed.get("signal", "hold"),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=parsed.get("reasoning", ""),
            key_levels={
                k: float(v) for k, v in parsed.get("key_levels", {}).items()
                if isinstance(v, (int, float))
            },
            raw_data=parsed,
        )

