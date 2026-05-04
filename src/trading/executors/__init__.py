# -*- coding: utf-8 -*-
"""Executor layer for the trading framework.

Provides a single entry point :func:`get_executor` that returns the
right executor for the active trading mode. Phase A unlocks ``paper``
(simulated fills against the latest live quote, persisted via
``PortfolioService.record_trade(source='paper')``); ``live`` raises
``NotImplementedError`` at construction so any accidental flip is
caught at the moment the orchestrator tries to instantiate it, not
later when it's about to send a real order.

Read-only invariant: NO module in this package imports ``firstrade.order``
or ``firstrade.trade``. The CI grep guard is extended to enforce this.
``live.py`` exists in Phase A but its ``__init__`` body is just
``raise NotImplementedError(...)``.
"""

from __future__ import annotations

from typing import Any

from src.trading.executors.base import BaseExecutor
from src.trading.types import ExecutionMode


def get_executor(mode: ExecutionMode, *, config: Any = None) -> BaseExecutor:
    """Return the executor for ``mode``. Raises for unsupported modes."""
    if mode == ExecutionMode.PAPER:
        from src.trading.executors.paper import PaperExecutor
        return PaperExecutor(config=config)
    if mode == ExecutionMode.LIVE:
        from src.trading.executors.live import LiveExecutor
        return LiveExecutor(config=config)
    raise ValueError(f"Unsupported execution mode: {mode!r}")


__all__ = ["BaseExecutor", "get_executor"]
