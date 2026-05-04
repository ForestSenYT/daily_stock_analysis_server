# -*- coding: utf-8 -*-
"""LiveExecutor — Phase B placeholder.

This file exists ONLY so the executor factory in
``src/trading/executors/__init__.py`` has a clean target to wire when
Phase B (real-money execution) is unlocked in a separate session.
Phase A invariants (paraphrasing the Plan):

  * ``from firstrade import order`` stays banned package-wide. The
    CI grep guard explicitly allowlists this file because the comments
    below mention the words "place_order" / "live execution" — but
    the body MUST stay a single ``raise NotImplementedError``.

  * Construction fails fast. ``TradingExecutionService.submit`` will
    catch the exception and produce a structured FAILED audit row; the
    user sees a 503 with ``error_code='LIVE_NOT_IMPLEMENTED'``.

  * No ``submit`` body. There is no real-money execution code here.
    Adding any in Phase A is an immediate review-blocker.
"""

from __future__ import annotations

from typing import Any, Optional

from src.trading.executors.base import BaseExecutor
from src.trading.types import OrderRequest, OrderResult, RiskAssessment


class LiveExecutor(BaseExecutor):
    """Stub. Real implementation deferred to Phase B."""

    def __init__(self, config: Any = None) -> None:
        super().__init__(config=config)
        raise NotImplementedError(
            "Live trading execution is not unlocked in Phase A. "
            "Set TRADING_MODE=paper for the simulated executor, or "
            "wait for Phase B which will implement real order placement "
            "with hard guardrails (confirm-token + dual approval + "
            "kill-switch + broker session liveness check)."
        )

    def submit(
        self,
        request: OrderRequest,
        risk_assessment: Optional[RiskAssessment] = None,
    ) -> OrderResult:  # pragma: no cover - unreachable
        raise NotImplementedError("LiveExecutor.submit is not implemented yet.")
