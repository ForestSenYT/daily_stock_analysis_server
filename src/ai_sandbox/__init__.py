# -*- coding: utf-8 -*-
"""AI Sandbox — Phase A+C foundation.

Two layers:

  **A. Forward simulation sandbox** — `AISandboxIntent`+
  `AISandboxResult` types, `AISandboxRepository`, `AISandboxService`.
  AI agent submits trade intents into an isolated audit stream
  (`ai_sandbox_executions` table) — never touches `portfolio_trades`,
  never touches `trade_executions`. Reuses `RiskEngine` and
  `PaperExecutor`'s quote/fill-price logic via composition.

  **C. Labeling for fine-tune dataset** — `AITrainingLabel` type +
  ORM table + endpoints. Users label historical /analyze reports
  AND ai_sandbox executions as correct / incorrect / unclear, with
  free-text "actual outcome" notes. Export endpoint produces JSONL
  ready for fine-tuning.

Read-only invariant preserved: NO ``firstrade.order`` /
``firstrade.trade`` import in this package. AI Sandbox uses paper
fills only — even when the wider trading framework's TRADING_MODE
is set to ``live``, the sandbox path stays paper-only.
"""

from src.ai_sandbox.types import (
    AISandboxIntent,
    AISandboxResult,
    AITrainingLabel,
    LabelKind,
    SandboxMode,
)

__all__ = [
    "AISandboxIntent",
    "AISandboxResult",
    "AITrainingLabel",
    "LabelKind",
    "SandboxMode",
]
