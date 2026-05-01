# -*- coding: utf-8 -*-
"""
===================================
Cloud Scheduler management endpoints
===================================

Lets the WebUI view / sync / pause / resume / run-now the daily-analysis
Cloud Scheduler job. All routes are admin-session protected by
``api/middlewares/auth.py``'s ``AuthMiddleware`` (because they live under
``/api/v1/system/schedule/*``).

Source of truth for cron / timezone is ``runtime.env`` (managed by the rest
of ``/api/v1/system/config``). This module only reflects those values into
GCP Cloud Scheduler.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException

from api.v1.schemas.schedule import (
    ScheduleActionResponse,
    ScheduleStatusResponse,
    ScheduleSyncResponse,
)
from src.config import setup_env
from src.services.cloud_scheduler_service import (
    CloudSchedulerError,
    CloudSchedulerNotConfigured,
    CloudSchedulerService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _service() -> CloudSchedulerService:
    return CloudSchedulerService()


def _read_schedule_settings() -> tuple[str, str, bool]:
    """Pull current cron / tz / enabled from process env (refreshed via
    ``setup_env`` so a recent WebUI save is reflected)."""
    setup_env(override=True)
    cron = (os.getenv("SCHEDULE_CRON") or "0 6 * * 2-6").strip()
    tz = (os.getenv("SCHEDULE_TIMEZONE") or "Asia/Shanghai").strip()
    enabled_raw = (os.getenv("SCHEDULE_ENABLED") or "false").strip().lower()
    return cron, tz, enabled_raw in {"1", "true", "yes", "on"}


def _to_status(payload: dict) -> ScheduleStatusResponse:
    return ScheduleStatusResponse(
        exists=bool(payload.get("exists", True)),
        job_name=payload.get("job_name"),
        schedule=payload.get("schedule"),
        time_zone=payload.get("time_zone"),
        state=payload.get("state"),
        last_attempt_time=payload.get("last_attempt_time"),
        next_run_time=payload.get("next_run_time"),
        user_update_time=payload.get("user_update_time"),
        project_id=payload.get("project_id"),
        region=payload.get("region"),
    )


@router.get(
    "/status",
    response_model=ScheduleStatusResponse,
    summary="Get current Cloud Scheduler job status",
)
def schedule_status() -> ScheduleStatusResponse:
    try:
        return _to_status(_service().status())
    except CloudSchedulerNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "scheduler_not_configured", "message": str(exc)},
        )
    except Exception as exc:  # pragma: no cover - GCP errors surface here
        logger.exception("schedule_status failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "scheduler_error", "message": str(exc)},
        )


@router.post(
    "/sync",
    response_model=ScheduleSyncResponse,
    summary="Create or update the Cloud Scheduler job from current settings",
)
def schedule_sync() -> ScheduleSyncResponse:
    cron, tz, enabled = _read_schedule_settings()
    try:
        result = _service().sync(cron=cron, timezone=tz, enabled=enabled)
        return ScheduleSyncResponse(
            ok=True,
            job=_to_status({**result, "exists": True}),
            cron=cron,
            time_zone=tz,
        )
    except CloudSchedulerNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "scheduler_not_configured", "message": str(exc)},
        )
    except CloudSchedulerError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "scheduler_error", "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("schedule_sync failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "scheduler_error", "message": str(exc)},
        )


@router.post(
    "/run-now",
    response_model=ScheduleActionResponse,
    summary="Trigger the scheduler job out-of-band immediately",
)
def schedule_run_now() -> ScheduleActionResponse:
    try:
        _service().run_now()
        return ScheduleActionResponse(ok=True, message="Triggered")
    except CloudSchedulerNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "scheduler_not_configured", "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("schedule_run_now failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "scheduler_error", "message": str(exc)},
        )


@router.post(
    "/pause",
    response_model=ScheduleActionResponse,
    summary="Pause the scheduler job",
)
def schedule_pause() -> ScheduleActionResponse:
    try:
        result = _service().pause()
        return ScheduleActionResponse(ok=True, job=_to_status({**result, "exists": True}))
    except CloudSchedulerNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "scheduler_not_configured", "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("schedule_pause failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "scheduler_error", "message": str(exc)},
        )


@router.post(
    "/resume",
    response_model=ScheduleActionResponse,
    summary="Resume the scheduler job",
)
def schedule_resume() -> ScheduleActionResponse:
    try:
        result = _service().resume()
        return ScheduleActionResponse(ok=True, job=_to_status({**result, "exists": True}))
    except CloudSchedulerNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "scheduler_not_configured", "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("schedule_resume failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "scheduler_error", "message": str(exc)},
        )
