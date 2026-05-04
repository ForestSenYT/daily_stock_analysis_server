# -*- coding: utf-8 -*-
"""AI Sandbox + Training data types.

Frozen dataclasses + str enums. Reuses ``OrderSide`` / ``OrderType``
from the trading framework so RiskEngine can evaluate the underlying
intent unchanged.

Key design decisions:
  * ``AISandboxIntent`` wraps a normal ``OrderRequest`` AND adds
    AI-specific metadata (agent_run_id / prompt_version /
    confidence_score / reasoning_text). The OrderRequest the engine
    sees is unchanged; the AI metadata is parallel context for audit.
  * Sandbox always runs in PAPER fill mode regardless of global
    ``TRADING_MODE`` —— even if the user enables live trading, the
    sandbox path stays simulated. This is a hard invariant.
  * P&L horizons (1d/3d/7d/30d) are filled in *after the fact* by a
    separate computation pass. The ``AISandboxResult`` returned at
    fill time has ``pnl_horizons=None``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.trading.types import (
    ExecutionStatus,
    OrderRequest,
    OrderSide,
    OrderType,
    RiskAssessment,
    TimeInForce,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SandboxMode(str, Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class LabelKind(str, Enum):
    """Outcome label for an AI prediction.

    ``correct`` and ``incorrect`` align with the recommendation: a
    BUY recommendation that subsequently rallied is correct;
    ``unclear`` is the explicit "ambiguous" bucket so users don't
    feel forced to label binary."""
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCLEAR = "unclear"


# =====================================================================
# AI Sandbox
# =====================================================================

@dataclass(frozen=True)
class AISandboxIntent:
    """One AI-generated trade intent submitted to the sandbox."""

    # Trade core (reused by RiskEngine via _to_order_request)
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    market: Optional[str] = None
    currency: Optional[str] = None

    # AI metadata
    agent_run_id: str = ""           # groups intents from one batch / single run
    prompt_version: str = ""         # which agent prompt produced this
    confidence_score: float = 0.0    # LLM-self-reported confidence 0..1
    reasoning_text: str = ""         # full LLM rationale (truncated for audit)
    model_used: str = ""             # e.g. 'openai/gpt-4o'

    # Provenance
    request_uid: str = ""            # idempotency anchor (UNIQUE in DB)
    requested_at: str = field(default_factory=_utc_now_iso)
    note: Optional[str] = None

    def to_order_request(self) -> OrderRequest:
        """Translate to the OrderRequest the RiskEngine expects.

        ``source='agent_sandbox'`` flags this as a sandbox intent so
        any downstream module that filters by source can identify it.
        ``account_id`` is left None — the sandbox doesn't bind to a
        portfolio account (forward-sim, isolated)."""
        return OrderRequest(
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            account_id=None,
            market=self.market,
            currency=self.currency,
            note=self.note,
            source="agent_sandbox",
            agent_session_id=self.agent_run_id,
            request_uid=self.request_uid,
            requested_at=self.requested_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for key in ("side", "order_type", "time_in_force"):
            value = getattr(self, key)
            d[key] = value.value if isinstance(value, Enum) else value
        return d


@dataclass(frozen=True)
class PnlHorizons:
    """Forward returns measured at multiple horizons after fill."""
    horizon_1d: Optional[float] = None
    horizon_3d: Optional[float] = None
    horizon_7d: Optional[float] = None
    horizon_30d: Optional[float] = None
    computed_at: Optional[str] = None
    horizon_1d_price: Optional[float] = None
    horizon_3d_price: Optional[float] = None
    horizon_7d_price: Optional[float] = None
    horizon_30d_price: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AISandboxResult:
    """Outcome of a sandbox submission. Mirrors OrderResult but
    streamlined (no portfolio_trade_id — sandbox doesn't write
    portfolio_trades)."""

    intent: AISandboxIntent
    status: ExecutionStatus
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    fill_time: Optional[str] = None
    risk_assessment: Optional[RiskAssessment] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    quote_payload: Optional[Dict[str, Any]] = None
    pnl_horizons: Optional[PnlHorizons] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "status": self.status.value,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "fill_time": self.fill_time,
            "risk_assessment": (
                self.risk_assessment.to_dict() if self.risk_assessment else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "quote_payload": dict(self.quote_payload) if self.quote_payload else None,
            "pnl_horizons": self.pnl_horizons.to_dict() if self.pnl_horizons else None,
        }


# =====================================================================
# Training labels (Phase C)
# =====================================================================

@dataclass(frozen=True)
class AITrainingLabel:
    """Human-supplied outcome label for an AI prediction.

    Source kinds:
      * ``analysis_history`` — the analysis report from /analyze
      * ``ai_sandbox`` — a sandbox execution outcome
    """

    source_kind: str       # "analysis_history" | "ai_sandbox"
    source_id: int         # FK to source row id
    label: LabelKind
    outcome_text: Optional[str] = None     # free-text "what actually happened"
    user_notes: Optional[str] = None       # any additional context
    created_by: Optional[str] = None       # admin username or "system"
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if isinstance(self.label, Enum):
            d["label"] = self.label.value
        return d
