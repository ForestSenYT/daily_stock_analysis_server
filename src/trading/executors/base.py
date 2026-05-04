# -*- coding: utf-8 -*-
"""Abstract executor.

Every concrete executor MUST keep the read-only invariant intact:
no ``from firstrade import order`` / ``firstrade.trade`` / order /
cancel modules. ``PaperExecutor`` simulates fills entirely from
quote data + ``PortfolioService.record_trade()``. ``LiveExecutor``
(Phase B) is the ONLY place where the invariant gets re-evaluated.

The ``submit`` contract is intentionally narrow:
  * Input: an immutable ``OrderRequest`` plus a frozen
    ``RiskAssessment`` from the engine.
  * Output: an immutable ``OrderResult``.
  * Side effects: at most one ``record_trade`` call (paper);
    no broker mutation in Phase A.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from src.trading.types import OrderRequest, OrderResult, RiskAssessment


class BaseExecutor(ABC):
    """Executor contract — exactly one ``submit`` method."""

    def __init__(self, config: Any = None) -> None:
        self._config = config

    @abstractmethod
    def submit(
        self,
        request: OrderRequest,
        risk_assessment: Optional[RiskAssessment] = None,
    ) -> OrderResult:
        """Execute (or simulate execution of) ``request``.

        Implementers must return an ``OrderResult`` with the appropriate
        ``status`` and ``mode`` fields. Exceptions thrown here are
        caught by ``TradingExecutionService.submit`` and converted
        into a ``FAILED`` audit row — but defensive implementations
        should prefer returning a ``FAILED`` ``OrderResult`` themselves
        so the error_code is structured (e.g. ``QUOTE_UNAVAILABLE``).
        """
        raise NotImplementedError
