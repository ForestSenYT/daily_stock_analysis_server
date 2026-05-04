# -*- coding: utf-8 -*-
"""Trading framework data classes — Phase A.

Designed to mirror VNPy's *concept* (clear separation of intent,
assessment, and result) without importing or reproducing VNPy code.

All enums are str-based so JSON serialisation in the audit table
(``trade_executions.request_payload_json`` / ``result_payload_json``)
is a one-liner via ``OrderRequest.to_dict()`` / ``OrderResult.to_dict()``.

Frozen dataclasses (``OrderRequest`` / ``RiskFlag`` / ``RiskAssessment``
/ ``OrderResult``) make accidental in-flight mutation impossible — once
a request leaves the API boundary, only the executor's ``OrderResult``
is allowed to vary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExecutionMode(str, Enum):
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"  # Phase B unlock; raises NotImplementedError today


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    BLOCKED = "blocked"  # killed by RiskEngine
    FAILED = "failed"  # downstream exception (quote unavailable, oversell, etc.)


class RiskFlagCode(str, Enum):
    OK = "ok"
    POSITION_SIZE_EXCEEDED = "position_size_exceeded"
    POSITION_PCT_EXCEEDED = "position_pct_exceeded"
    DAILY_TURNOVER_EXCEEDED = "daily_turnover_exceeded"
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    SYMBOL_DENYLISTED = "symbol_denylisted"
    MARKET_CLOSED = "market_closed"
    BROKER_NOT_LOGGED_IN = "broker_not_logged_in"  # info-only in paper; hard block in live
    QUOTE_UNAVAILABLE = "quote_unavailable"
    INVALID_PARAMETERS = "invalid_parameters"
    OVERSELL = "oversell"


class RiskSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCK = "block"


# =====================================================================
# OrderRequest
# =====================================================================

@dataclass(frozen=True)
class OrderRequest:
    """Trade intent. Built by the user (UI) or the agent
    (``propose_trade`` emit-only tool). Never built inside the
    executor — that role is reserved for the originator.

    Frozen so callers can't mutate fields between RiskEngine evaluation
    and Executor.submit (which would otherwise let stale validation
    decisions slip through).
    """

    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    account_id: Optional[int] = None  # PortfolioAccount.id
    market: Optional[str] = None  # "us" | "cn" | "hk"
    currency: Optional[str] = None
    note: Optional[str] = None
    # Provenance — required for audit:
    source: str = "ui"  # "ui" | "agent" | "strategy"
    agent_session_id: Optional[str] = None
    request_uid: str = ""  # idempotency anchor; reused as PortfolioTrade.trade_uid
    requested_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Coerce enums → str so this is JSON-serialisable as-is.
        for key in ("side", "order_type", "time_in_force"):
            value = getattr(self, key)
            d[key] = value.value if isinstance(value, Enum) else value
        return d


# =====================================================================
# Risk
# =====================================================================

@dataclass(frozen=True)
class RiskFlag:
    code: RiskFlagCode
    severity: RiskSeverity
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class RiskAssessment:
    flags: List[RiskFlag]
    decision: str  # "allow" | "block"
    evaluated_at: str
    # Frozen snapshot of the active thresholds — lets future audits
    # replay decisions without having to know what env was loaded
    # at the moment the request was evaluated.
    config_snapshot: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flags": [f.to_dict() for f in self.flags],
            "decision": self.decision,
            "evaluated_at": self.evaluated_at,
            "config_snapshot": dict(self.config_snapshot),
        }


# =====================================================================
# OrderResult
# =====================================================================

@dataclass(frozen=True)
class OrderResult:
    """Authoritative outcome — created by the executor or the risk
    engine (block path). Frozen so the audit-write and the API
    response always see exactly the same payload."""

    request: OrderRequest
    status: ExecutionStatus
    mode: ExecutionMode
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    fill_time: Optional[str] = None
    realised_fee: float = 0.0
    realised_tax: float = 0.0
    risk_assessment: Optional[RiskAssessment] = None
    portfolio_trade_id: Optional[int] = None  # set when paper trade was persisted
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    quote_payload: Optional[Dict[str, Any]] = None  # snapshot used for the simulated fill

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "mode": self.mode.value,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "fill_time": self.fill_time,
            "realised_fee": self.realised_fee,
            "realised_tax": self.realised_tax,
            "risk_assessment": (
                self.risk_assessment.to_dict() if self.risk_assessment else None
            ),
            "portfolio_trade_id": self.portfolio_trade_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "quote_payload": dict(self.quote_payload) if self.quote_payload else None,
        }
