# -*- coding: utf-8 -*-
"""Trading API endpoints — Phase A.

Endpoints:
  GET  /api/v1/trading/status         master status + thresholds
  POST /api/v1/trading/submit         submit an OrderRequest (paper)
  POST /api/v1/trading/risk/preview   RiskEngine.evaluate without persisting
  GET  /api/v1/trading/executions     recent audit rows

All endpoints return 503 with a structured payload when
``TRADING_MODE=disabled`` so the frontend can hide the panel cleanly.

Idempotency: POST /submit accepts a ``request_uid`` (≥8 chars). Repeat
submission with the same UID returns 409 instead of double-filling.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.trading import (
    OrderSubmitRequest,
    OrderSubmitResponse,
    RiskPreviewRequest,
    RiskPreviewResponse,
    TradeExecutionListResponse,
    TradingStatusResponse,
)
from src.config import get_config
from src.services.trading_service import (
    TradingDisabledError,
    get_trading_service,
)
from src.trading.types import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_GENERIC_TRADING_ERROR = "Trading operation failed."


def _request_to_dataclass(body: OrderSubmitRequest) -> OrderRequest:
    """Translate a Pydantic submit body into an immutable
    ``OrderRequest`` dataclass for the service layer."""
    return OrderRequest(
        symbol=body.symbol.strip().upper(),
        side=OrderSide(body.side),
        quantity=float(body.quantity),
        order_type=OrderType(body.order_type),
        limit_price=body.limit_price,
        time_in_force=TimeInForce(body.time_in_force),
        account_id=body.account_id,
        market=body.market,
        currency=body.currency,
        note=body.note,
        source=body.source,
        agent_session_id=body.agent_session_id,
        request_uid=body.request_uid.strip(),
    )


@router.get(
    "/status",
    response_model=TradingStatusResponse,
    summary="Trading framework status",
    description=(
        "Returns the active mode (``disabled`` / ``paper`` / ``live``) "
        "and the risk thresholds. Safe to poll — never 5xx for a "
        "healthy service."
    ),
)
def trading_status() -> TradingStatusResponse:
    try:
        svc = get_trading_service()
        payload = svc.get_status()
        return TradingStatusResponse(**payload)
    except Exception:
        logger.exception("[trading] status endpoint surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_error", "message": _GENERIC_TRADING_ERROR},
        )


@router.post(
    "/submit",
    response_model=OrderSubmitResponse,
    summary="Submit an order (paper-mode only in Phase A)",
    description=(
        "Submits an OrderRequest. Pipeline: audit row (pending) → "
        "RiskEngine → executor (paper / live) → audit row (final) → "
        "notification. Idempotency via ``request_uid``: duplicates "
        "return 409 instead of double-filling. Phase A's ``live`` "
        "mode raises ``LIVE_NOT_IMPLEMENTED`` and 503s."
    ),
    responses={
        200: {"description": "Submission processed (status field disambiguates fill / block / fail)."},
        409: {"description": "Duplicate request_uid — original audit row wins."},
        503: {"description": "Feature disabled or live not implemented."},
    },
)
def trading_submit(body: OrderSubmitRequest) -> OrderSubmitResponse:
    try:
        request = _request_to_dataclass(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        )
    try:
        svc = get_trading_service()
        result_dict = svc.submit(request)
    except TradingDisabledError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_disabled", "message": str(exc)},
        )
    except Exception:
        logger.exception("[trading] submit endpoint surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_error", "message": _GENERIC_TRADING_ERROR},
        )
    # Idempotency violation — orchestrator returns FAILED with
    # ``error_code='DUPLICATE_REQUEST_UID'``. Map to 409.
    if result_dict.get("error_code") == "DUPLICATE_REQUEST_UID":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_request_uid",
                "message": result_dict.get("error_message")
                or "request_uid already submitted",
            },
        )
    return OrderSubmitResponse(**result_dict)


@router.post(
    "/risk/preview",
    response_model=RiskPreviewResponse,
    summary="Preview RiskEngine assessment without persisting an audit row",
    description=(
        "Runs the same risk checks as ``/submit`` but does NOT write "
        "an audit row, doesn't dispatch to an executor, and doesn't "
        "fire notifications. Useful for the WebUI's preview button."
    ),
)
def trading_risk_preview(body: RiskPreviewRequest) -> RiskPreviewResponse:
    try:
        request = _request_to_dataclass(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        )
    try:
        svc = get_trading_service()
        assessment = svc.preview_risk(request)
    except TradingDisabledError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_disabled", "message": str(exc)},
        )
    except Exception:
        logger.exception("[trading] preview endpoint surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_error", "message": _GENERIC_TRADING_ERROR},
        )
    return RiskPreviewResponse(**assessment)


@router.get(
    "/executions",
    response_model=TradeExecutionListResponse,
    summary="List recent trade execution audit rows",
)
def trading_executions(
    mode: Optional[str] = Query(default=None, regex="^(paper|live)$"),
    account_id: Optional[int] = Query(default=None, ge=1),
    symbol: Optional[str] = Query(default=None, max_length=16),
    status: Optional[str] = Query(
        default=None,
        regex="^(pending|filled|blocked|failed)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> TradeExecutionListResponse:
    cfg = get_config()
    if getattr(cfg, "trading_mode", "disabled") == "disabled":
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_disabled",
                    "message": "Trading framework is disabled."},
        )
    try:
        svc = get_trading_service()
        payload = svc.list_recent_executions(
            mode=mode, account_id=account_id, symbol=symbol,
            status=status, limit=limit,
        )
        return TradeExecutionListResponse(**payload)
    except Exception:
        logger.exception("[trading] executions listing surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "trading_error", "message": _GENERIC_TRADING_ERROR},
        )
