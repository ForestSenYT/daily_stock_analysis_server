# -*- coding: utf-8 -*-
"""AI sandbox + training-label API endpoints.

All endpoints sit under ``/api/v1/ai-sandbox/*`` and ``/api/v1/ai-training/*``.
They're admin-gated via the existing auth middleware; the auth layer's
``SENSITIVE_API_PREFIXES`` list also includes these (added in this
commit).

Phase-A invariant preserved: nothing here calls ``firstrade.order`` or
any live-trading path. Sandbox always uses paper fills.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.ai_sandbox import (
    AISandboxStatusResponse,
    PnlComputeResponse,
    RunBatchRequest,
    RunOnceRequest,
    SandboxExecutionListResponse,
    SandboxMetricsResponse,
    SandboxSubmitRequest,
    TrainingLabelListResponse,
    TrainingLabelStatsResponse,
    TrainingLabelUpsertRequest,
)
from src.ai_sandbox.repo import (
    AITrainingLabelRepository,
    DuplicateSandboxRequestError,
)
from src.ai_sandbox.types import (
    AISandboxIntent,
    LabelKind,
)
from src.config import get_config
from src.services.ai_sandbox_pnl_service import AISandboxPnlService
from src.services.ai_sandbox_service import (
    SandboxDisabledError,
    get_ai_sandbox_service,
)
from src.trading.types import OrderSide, OrderType, TimeInForce

logger = logging.getLogger(__name__)

# Two routers under one module — saves a file.
sandbox_router = APIRouter()
training_router = APIRouter()


_GENERIC_ERR = "AI sandbox operation failed."


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =====================================================================
# Sandbox: status / submit / executions / metrics / pnl
# =====================================================================

@sandbox_router.get(
    "/status",
    response_model=AISandboxStatusResponse,
    summary="AI sandbox status + thresholds",
)
def sandbox_status() -> AISandboxStatusResponse:
    try:
        svc = get_ai_sandbox_service()
        return AISandboxStatusResponse(**svc.get_status())
    except Exception:
        logger.exception("[ai-sandbox] status surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_error", "message": _GENERIC_ERR},
        )


def _request_to_intent(body: SandboxSubmitRequest) -> AISandboxIntent:
    return AISandboxIntent(
        symbol=body.symbol.strip().upper(),
        side=OrderSide(body.side),
        quantity=float(body.quantity),
        order_type=OrderType(body.order_type),
        limit_price=body.limit_price,
        time_in_force=TimeInForce.DAY,
        market=body.market,
        currency=body.currency,
        note=body.note,
        agent_run_id=body.agent_run_id or "",
        prompt_version=body.prompt_version or "",
        confidence_score=float(body.confidence_score or 0.0),
        reasoning_text=body.reasoning_text or "",
        model_used=body.model_used or "",
        request_uid=body.request_uid.strip(),
    )


@sandbox_router.post(
    "/submit",
    summary="Submit one AI sandbox intent (manual / external callers)",
)
def sandbox_submit(body: SandboxSubmitRequest) -> dict:
    try:
        intent = _request_to_intent(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        )
    try:
        svc = get_ai_sandbox_service()
        result = svc.submit(intent)
    except SandboxDisabledError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": str(exc)},
        )
    except DuplicateSandboxRequestError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate_request_uid", "message": str(exc)},
        )
    except Exception:
        logger.exception("[ai-sandbox] submit surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_error", "message": _GENERIC_ERR},
        )
    if result.get("error_code") == "DUPLICATE_REQUEST_UID":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_request_uid",
                "message": result.get("error_message"),
            },
        )
    return result


@sandbox_router.post(
    "/run-once",
    summary='Trigger AI to make one decision and submit it (server-side LLM call)',
)
def sandbox_run_once(body: RunOnceRequest) -> dict:
    """Server-side: wakes the LLM for a single symbol, asks for a
    buy/sell/hold call, and submits if non-hold. Convenience wrapper
    over the daemon's per-symbol logic for manual invocation."""
    try:
        # Reuse the daemon's _decide_one_symbol so the prompt + parsing
        # logic stays in one place.
        from src.services.ai_sandbox_daemon import _decide_one_symbol
        from src.services.ai_sandbox_service import get_ai_sandbox_service
        cfg = get_config()
        svc = get_ai_sandbox_service()
        if not svc.is_enabled():
            raise HTTPException(
                status_code=503,
                detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
            )
        decision = _decide_one_symbol(body.symbol.strip().upper(), config=cfg)
    except HTTPException:
        raise
    except Exception:
        logger.exception("[ai-sandbox] run-once surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_error", "message": _GENERIC_ERR},
        )
    if decision is None:
        return {
            "status": "skipped",
            "message": "No quote / LLM response unavailable for symbol.",
            "symbol": body.symbol.strip().upper(),
        }
    if decision["decision"] == "hold":
        return {
            "status": "hold",
            "decision": decision,
            "symbol": body.symbol.strip().upper(),
        }
    if decision["quantity"] <= 0:
        return {
            "status": "skipped",
            "message": "LLM returned non-positive quantity.",
            "decision": decision,
            "symbol": body.symbol.strip().upper(),
        }

    intent = AISandboxIntent(
        symbol=body.symbol.strip().upper(),
        side=OrderSide.BUY if decision["decision"] == "buy" else OrderSide.SELL,
        quantity=float(decision["quantity"]),
        order_type=OrderType.MARKET,
        market=body.market,
        agent_run_id=f"manual-{uuid.uuid4().hex[:12]}",
        prompt_version=(
            body.prompt_version
            or getattr(get_config(), "ai_sandbox_default_prompt_version", "v1")
        ),
        confidence_score=float(decision["confidence"]),
        reasoning_text=decision["reasoning"],
        model_used=decision["model_used"],
        request_uid=f"manual-{uuid.uuid4().hex[:24]}",
    )
    try:
        result = svc.submit(intent)
    except DuplicateSandboxRequestError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate_request_uid", "message": str(exc)},
        )
    return {"status": "submitted", "result": result, "decision": decision}


@sandbox_router.post(
    "/run-batch",
    summary="Run AI decisions across multiple symbols (≤ 20)",
)
def sandbox_run_batch(body: RunBatchRequest) -> dict:
    submitted = []
    skipped = []
    held = []
    for sym in body.symbols:
        try:
            row = sandbox_run_once(RunOnceRequest(
                symbol=sym, market=body.market,
                prompt_version=body.prompt_version,
            ))
            status = row.get("status")
            if status == "submitted":
                submitted.append(sym.upper())
            elif status == "hold":
                held.append(sym.upper())
            else:
                skipped.append(sym.upper())
        except HTTPException as exc:
            skipped.append(sym.upper())
            logger.debug("[ai-sandbox] batch skip %s: %s", sym, exc.detail)
    return {
        "submitted": submitted,
        "held": held,
        "skipped": skipped,
        "total": len(body.symbols),
    }


@sandbox_router.get(
    "/executions",
    response_model=SandboxExecutionListResponse,
    summary="List recent sandbox executions",
)
def sandbox_executions(
    agent_run_id: Optional[str] = Query(default=None, max_length=100),
    symbol: Optional[str] = Query(default=None, max_length=16),
    status: Optional[str] = Query(
        default=None, regex="^(pending|filled|blocked|failed)$",
    ),
    prompt_version: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
) -> SandboxExecutionListResponse:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    svc = get_ai_sandbox_service()
    payload = svc.list_recent(
        agent_run_id=agent_run_id, symbol=symbol, status=status,
        prompt_version=prompt_version, limit=limit,
    )
    return SandboxExecutionListResponse(**payload)


@sandbox_router.get(
    "/metrics",
    response_model=SandboxMetricsResponse,
    summary="Aggregate sandbox metrics (win rate / avg P&L)",
)
def sandbox_metrics(
    since_days: Optional[int] = Query(default=None, ge=1, le=365),
    prompt_version: Optional[str] = Query(default=None, max_length=64),
    symbol: Optional[str] = Query(default=None, max_length=16),
) -> SandboxMetricsResponse:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    svc = get_ai_sandbox_service()
    return SandboxMetricsResponse(**svc.metrics(
        since_days=since_days, prompt_version=prompt_version, symbol=symbol,
    ))


@sandbox_router.post(
    "/pnl/compute",
    response_model=PnlComputeResponse,
    summary="Manually trigger P&L horizon rollup for filled rows",
)
def sandbox_pnl_compute(
    limit: int = Query(default=50, ge=1, le=500),
) -> PnlComputeResponse:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    counts = AISandboxPnlService().compute_pnl_for_pending(limit=limit)
    return PnlComputeResponse(**counts)


# =====================================================================
# Training labels (Phase C)
# =====================================================================

@training_router.post(
    "/labels",
    response_model=Optional[dict],
    summary="Upsert outcome label for AI prediction (analysis report or sandbox row)",
)
def label_upsert(body: TrainingLabelUpsertRequest) -> dict:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    repo = AITrainingLabelRepository()
    return repo.upsert_label(
        source_kind=body.source_kind,
        source_id=body.source_id,
        label=LabelKind(body.label),
        outcome_text=body.outcome_text,
        user_notes=body.user_notes,
    )


@training_router.delete(
    "/labels",
    summary="Delete a label by (source_kind, source_id)",
)
def label_delete(
    source_kind: str = Query(..., regex="^(analysis_history|ai_sandbox)$"),
    source_id: int = Query(..., ge=1),
) -> dict:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    repo = AITrainingLabelRepository()
    deleted = repo.delete_label(source_kind=source_kind, source_id=source_id)
    return {"deleted": bool(deleted)}


@training_router.get(
    "/labels",
    response_model=TrainingLabelListResponse,
    summary="List labels",
)
def label_list(
    source_kind: Optional[str] = Query(
        default=None, regex="^(analysis_history|ai_sandbox)$",
    ),
    label: Optional[str] = Query(
        default=None, regex="^(correct|incorrect|unclear)$",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
) -> TrainingLabelListResponse:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    repo = AITrainingLabelRepository()
    rows = repo.list_labels(source_kind=source_kind, label=label, limit=limit)
    return TrainingLabelListResponse(items=rows, count=len(rows))


@training_router.get(
    "/labels/stats",
    response_model=TrainingLabelStatsResponse,
    summary="Label dataset stats",
)
def label_stats() -> TrainingLabelStatsResponse:
    cfg = get_config()
    if not bool(getattr(cfg, "ai_sandbox_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_sandbox_disabled", "message": "Sandbox disabled."},
        )
    repo = AITrainingLabelRepository()
    return TrainingLabelStatsResponse(**repo.stats())
