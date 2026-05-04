# -*- coding: utf-8 -*-
"""Trading framework — Phase A (paper-only) foundation.

Contract (paraphrasing the Plan):

  * This package never imports ``firstrade.order`` or
    ``firstrade.trade``. The repo-wide CI grep guard is extended to
    forbid both, with two explicit allowlist entries:
      - ``src/trading/executors/live.py`` (Phase B placeholder; only
        contains ``raise NotImplementedError`` at construction)
      - ``tests/test_trading_invariant_guard.py`` (test fixture
        strings only)

  * The agent never auto-submits in Phase A. The ``propose_trade``
    tool emits an ``OrderRequest``-shaped dict; the user is the only
    entity that can call ``TradingExecutionService.submit``.

  * ``TRADING_MODE=disabled`` (default) keeps the entire feature
    dormant: API endpoints 503, WebUI panel hidden, agent tool
    unregistered. Setting it back to ``disabled`` is a hard stop.

Re-exports the top-level types so callers don't have to know which
sub-module owns each shape.
"""

from src.trading.types import (
    ExecutionMode,
    ExecutionStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    RiskAssessment,
    RiskFlag,
    RiskFlagCode,
    RiskSeverity,
    TimeInForce,
)

__all__ = [
    "ExecutionMode",
    "ExecutionStatus",
    "OrderRequest",
    "OrderResult",
    "OrderSide",
    "OrderType",
    "RiskAssessment",
    "RiskFlag",
    "RiskFlagCode",
    "RiskSeverity",
    "TimeInForce",
]
