# -*- coding: utf-8 -*-
"""Pydantic schemas for the trading API.

Snake-case fields (the frontend's ``toCamelCase`` helper handles the
boundary). All numeric / string types match the underlying dataclass
shapes in ``src/trading/types.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TradingStatusResponse(BaseModel):
    status: str  # disabled | ready | error
    mode: str    # disabled | paper | live
    message: Optional[str] = None
    paper_account_id: Optional[int] = None
    max_position_value: Optional[float] = None
    max_position_pct: Optional[float] = None
    max_daily_turnover: Optional[float] = None
    symbol_allowlist: List[str] = Field(default_factory=list)
    symbol_denylist: List[str] = Field(default_factory=list)
    market_hours_strict: Optional[bool] = None
    notification_enabled: Optional[bool] = None


class OrderSubmitRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = Field(None, gt=0)
    time_in_force: Literal["day", "gtc"] = "day"
    account_id: Optional[int] = None
    market: Optional[Literal["us", "cn", "hk"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    note: Optional[str] = Field(None, max_length=255)
    request_uid: str = Field(..., min_length=8, max_length=64)
    source: Literal["ui", "agent", "strategy"] = "ui"
    agent_session_id: Optional[str] = Field(None, max_length=100)


class RiskFlagItem(BaseModel):
    code: str
    severity: str
    message: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RiskAssessmentItem(BaseModel):
    flags: List[RiskFlagItem] = Field(default_factory=list)
    decision: str
    evaluated_at: str
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)


class OrderResultItem(BaseModel):
    request: Dict[str, Any]
    status: str
    mode: str
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    fill_time: Optional[str] = None
    realised_fee: float = 0.0
    realised_tax: float = 0.0
    risk_assessment: Optional[RiskAssessmentItem] = None
    portfolio_trade_id: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    quote_payload: Optional[Dict[str, Any]] = None


class OrderSubmitResponse(OrderResultItem):
    pass


class RiskPreviewRequest(OrderSubmitRequest):
    """Same shape as a submit request — preview just runs the
    RiskEngine and returns the assessment without writing audit."""


class RiskPreviewResponse(BaseModel):
    decision: str
    flags: List[RiskFlagItem] = Field(default_factory=list)
    evaluated_at: str
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)


class TradeExecutionItem(BaseModel):
    id: int
    request_uid: str
    mode: str
    source: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: Optional[float] = None
    account_id: Optional[int] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    status: str
    risk_decision: Optional[str] = None
    risk_flags: List[RiskFlagItem] = Field(default_factory=list)
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    realised_fee: float = 0.0
    realised_tax: float = 0.0
    portfolio_trade_id: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    agent_session_id: Optional[str] = None
    requested_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: Optional[str] = None
    request_payload: Dict[str, Any] = Field(default_factory=dict)
    result_payload: Optional[Dict[str, Any]] = None


class TradeExecutionListResponse(BaseModel):
    items: List[TradeExecutionItem] = Field(default_factory=list)
    count: int = 0
