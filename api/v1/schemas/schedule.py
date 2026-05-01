# -*- coding: utf-8 -*-
"""Pydantic schemas for the Cloud Scheduler integration endpoints."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ScheduleStatusResponse(BaseModel):
    """Current state of the managed Cloud Scheduler job."""

    exists: bool = Field(description="Whether the job exists in Cloud Scheduler")
    job_name: Optional[str] = None
    schedule: Optional[str] = Field(default=None, description="Current cron expression")
    time_zone: Optional[str] = None
    state: Optional[str] = Field(
        default=None,
        description="ENABLED / PAUSED / UPDATE_FAILED / DISABLED",
    )
    last_attempt_time: Optional[str] = None
    next_run_time: Optional[str] = None
    user_update_time: Optional[str] = None
    project_id: Optional[str] = None
    region: Optional[str] = None


class ScheduleSyncResponse(BaseModel):
    """Result of a sync operation (idempotent create-or-update)."""

    ok: bool = True
    job: ScheduleStatusResponse
    cron: str
    time_zone: str


class ScheduleActionResponse(BaseModel):
    """Generic response for run-now / pause / resume / delete."""

    ok: bool = True
    job: Optional[ScheduleStatusResponse] = None
    message: Optional[str] = None
