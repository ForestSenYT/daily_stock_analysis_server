# -*- coding: utf-8 -*-
"""Pydantic schemas for AI sandbox + training-label APIs."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class AISandboxStatusResponse(BaseModel):
    status: str  # disabled | ready
    message: Optional[str] = None
    max_position_value: Optional[float] = None
    max_position_pct: Optional[float] = None
    max_daily_turnover: Optional[float] = None
    symbol_allowlist: List[str] = Field(default_factory=list)
    paper_slippage_bps: Optional[int] = None
    daemon_enabled: Optional[bool] = None
    daemon_interval_minutes: Optional[int] = None
    daemon_watchlist: List[str] = Field(default_factory=list)


class SandboxSubmitRequest(BaseModel):
    """Direct submission — used by the manual "Run once" button or
    by external callers that already have an AI decision."""
    symbol: str = Field(..., min_length=1, max_length=16)
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = Field(None, gt=0)
    market: Optional[Literal["us", "cn", "hk"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    note: Optional[str] = Field(None, max_length=255)
    request_uid: str = Field(..., min_length=8, max_length=64)
    agent_run_id: Optional[str] = Field(None, max_length=100)
    prompt_version: Optional[str] = Field(None, max_length=64)
    confidence_score: Optional[float] = Field(None, ge=0, le=1)
    reasoning_text: Optional[str] = Field(None, max_length=2000)
    model_used: Optional[str] = Field(None, max_length=100)


class RunOnceRequest(BaseModel):
    """Server-side "let the AI decide one symbol now" trigger."""
    symbol: str = Field(..., min_length=1, max_length=16)
    market: Optional[Literal["us", "cn", "hk"]] = None
    prompt_version: Optional[str] = Field(None, max_length=64)


class RunBatchRequest(BaseModel):
    """Run AI decisions on a list of symbols (≤ 20 per call)."""
    symbols: List[str] = Field(..., min_length=1, max_length=20)
    market: Optional[Literal["us", "cn", "hk"]] = None
    prompt_version: Optional[str] = Field(None, max_length=64)


class SandboxExecutionItem(BaseModel):
    id: int
    request_uid: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: Optional[float] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    agent_run_id: Optional[str] = None
    prompt_version: Optional[str] = None
    confidence_score: Optional[float] = None
    reasoning_text: Optional[str] = None
    model_used: Optional[str] = None
    status: str
    risk_decision: Optional[str] = None
    risk_flags: List[Dict[str, Any]] = Field(default_factory=list)
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    fill_time: Optional[str] = None
    intent_payload: Dict[str, Any] = Field(default_factory=dict)
    result_payload: Optional[Dict[str, Any]] = None
    quote_payload: Optional[Dict[str, Any]] = None
    pnl_horizons: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    requested_at: Optional[str] = None
    pnl_computed_at: Optional[str] = None
    created_at: Optional[str] = None


class SandboxExecutionListResponse(BaseModel):
    items: List[SandboxExecutionItem] = Field(default_factory=list)
    count: int = 0


class SandboxMetricsResponse(BaseModel):
    total_executions: int
    filled_count: int
    with_pnl_count: int
    win_rate_1d: Optional[float] = None
    win_rate_7d: Optional[float] = None
    avg_pnl_1d_pct: Optional[float] = None
    avg_pnl_7d_pct: Optional[float] = None
    filters: Dict[str, Any] = Field(default_factory=dict)


class PnlComputeResponse(BaseModel):
    scanned: int
    computed: int
    skipped: int


# =====================================================================
# Training labels (Phase C)
# =====================================================================

class TrainingLabelUpsertRequest(BaseModel):
    source_kind: Literal["analysis_history", "ai_sandbox"]
    source_id: int = Field(..., ge=1)
    label: Literal["correct", "incorrect", "unclear"]
    outcome_text: Optional[str] = Field(None, max_length=2000)
    user_notes: Optional[str] = Field(None, max_length=1000)


class TrainingLabelItem(BaseModel):
    id: int
    source_kind: str
    source_id: int
    label: str
    outcome_text: Optional[str] = None
    user_notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class TrainingLabelListResponse(BaseModel):
    items: List[TrainingLabelItem] = Field(default_factory=list)
    count: int = 0


class TrainingLabelStatsResponse(BaseModel):
    total: int
    correct: int
    incorrect: int
    unclear: int
    from_analysis_history: int
    from_ai_sandbox: int
