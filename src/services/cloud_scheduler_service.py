# -*- coding: utf-8 -*-
"""
===================================
Cloud Scheduler Service
===================================

Thin wrapper around ``google.cloud.scheduler_v1`` so the WebUI can manage a
single daily-analysis job (create / update / pause / resume / run-now /
status) without leaving the application.

Design choices:
- **One job per service**: deterministic name ``dsa-watchlist`` (configurable
  via ``SCHEDULE_JOB_NAME``). Multi-job per service is out of scope.
- **OIDC auth** between Cloud Scheduler and Cloud Run: the job carries an
  ``OidcToken`` whose ``service_account_email`` is the Cloud Run runtime SA
  and ``audience`` is the Cloud Run service URL. ``server.py`` validates
  these tokens on ``/analyze`` (see ``_require_api_token``).
- **Source of truth = ``runtime.env``**: the sync method reads the current
  cron / timezone from project config (not request body), so the WebUI form
  and the live scheduler stay in lockstep via the same persistence flow.
- **Auto-detection**: project_id, region, runtime SA, and Cloud Run URL are
  all derived from the GCP metadata server when env vars don't override
  them. This keeps the service zero-config on Cloud Run.

Public methods of ``CloudSchedulerService``:
    sync(cron, timezone, enabled, body) -> Dict   # idempotent create/update
    status() -> Dict                              # current state of the job
    run_now() -> None                             # forceRun the job
    pause() -> None
    resume() -> None
    delete() -> None
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Default values – override via env vars if needed.
_DEFAULT_JOB_NAME = "dsa-watchlist"
_DEFAULT_TARGET_PATH = "/analyze"
_DEFAULT_BODY: Dict[str, Any] = {"notify": True}
_DEFAULT_ATTEMPT_DEADLINE_SECONDS = 1800  # 30m, Cloud Scheduler HTTP target max

_METADATA_HEADERS = {"Metadata-Flavor": "Google"}
_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"


class CloudSchedulerNotConfigured(RuntimeError):
    """Raised when the service can't locate enough context to talk to GCP."""


class CloudSchedulerError(RuntimeError):
    """Generic wrapper for failures returned by the Scheduler API."""


def _metadata_get(path: str, default: Optional[str] = None) -> Optional[str]:
    """Fetch one metadata-server field; return ``default`` if not on GCP."""
    try:
        resp = requests.get(
            f"{_METADATA_BASE}/{path}",
            headers=_METADATA_HEADERS,
            timeout=2,
        )
        if resp.status_code == 200:
            return resp.text.strip()
    except requests.RequestException:
        pass
    return default


def _detect_project_id() -> Optional[str]:
    return os.getenv("GCP_PROJECT_ID") or _metadata_get("project/project-id")


def _detect_region() -> Optional[str]:
    """
    Cloud Run injects ``K_REGION`` for gen2 services. Fallback to metadata.
    """
    return (
        os.getenv("GCP_REGION")
        or os.getenv("K_REGION")
        or _metadata_get("instance/region", default="").rsplit("/", 1)[-1] or None
    )


def _detect_runtime_sa() -> Optional[str]:
    """The service account this Cloud Run revision runs as."""
    return os.getenv("CLOUD_RUN_RUNTIME_SA") or _metadata_get(
        "instance/service-accounts/default/email"
    )


def _detect_cloud_run_url() -> Optional[str]:
    """Best-effort discovery of ``https://<service>-<num>.<region>.run.app``."""
    explicit = os.getenv("CLOUD_RUN_URL")
    if explicit:
        return explicit.rstrip("/")
    service = os.getenv("K_SERVICE")
    region = _detect_region()
    project_number = _metadata_get("project/numeric-project-id")
    if service and region and project_number:
        return f"https://{service}-{project_number}.{region}.run.app"
    return None


class CloudSchedulerService:
    """
    Manage the single daily-analysis Cloud Scheduler job for this service.

    Construction is cheap (no network IO). Each method translates to one
    Cloud Scheduler API call. Failures from the API surface as
    ``CloudSchedulerError`` with the underlying message attached.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        runtime_sa: Optional[str] = None,
        cloud_run_url: Optional[str] = None,
        job_name: Optional[str] = None,
    ) -> None:
        self.project_id = project_id or _detect_project_id()
        self.region = region or _detect_region()
        self.runtime_sa = runtime_sa or _detect_runtime_sa()
        self.cloud_run_url = (cloud_run_url or _detect_cloud_run_url() or "").rstrip("/")
        self.job_name = (
            job_name
            or os.getenv("SCHEDULE_JOB_NAME")
            or _DEFAULT_JOB_NAME
        )

        missing = [
            name
            for name, value in (
                ("project_id", self.project_id),
                ("region", self.region),
                ("runtime_sa", self.runtime_sa),
                ("cloud_run_url", self.cloud_run_url),
            )
            if not value
        ]
        if missing:
            logger.warning(
                "[CloudScheduler] missing context: %s — calls will fail until provided",
                missing,
            )

        # Lazy-imported so requirements.txt doesn't have to install
        # google-cloud-scheduler in environments that never touch this code.
        self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_configured(self) -> None:
        if not self.project_id or not self.region:
            raise CloudSchedulerNotConfigured(
                "GCP_PROJECT_ID / region not detected. Set GCP_PROJECT_ID and "
                "GCP_REGION env vars, or run inside Cloud Run (which injects "
                "K_SERVICE / K_REGION)."
            )
        if not self.runtime_sa or not self.cloud_run_url:
            raise CloudSchedulerNotConfigured(
                "Runtime SA / Cloud Run URL not detected. Set "
                "CLOUD_RUN_RUNTIME_SA / CLOUD_RUN_URL env vars or rely on "
                "metadata-server auto-detection."
            )

    def _get_client(self):
        if self._client is None:
            try:
                from google.cloud import scheduler_v1  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise CloudSchedulerNotConfigured(
                    "google-cloud-scheduler is not installed. Add "
                    "`google-cloud-scheduler>=2.13,<3` to requirements.txt."
                ) from exc
            self._client = scheduler_v1.CloudSchedulerClient()
        return self._client

    def _parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.region}"

    def _job_path(self) -> str:
        return f"{self._parent()}/jobs/{self.job_name}"

    def _build_job(self, cron: str, timezone: str, body: Dict[str, Any]):
        from google.cloud import scheduler_v1  # type: ignore

        target_url = f"{self.cloud_run_url}{_DEFAULT_TARGET_PATH}"
        oidc_audience = self.cloud_run_url

        http_target = scheduler_v1.HttpTarget(
            uri=target_url,
            http_method=scheduler_v1.HttpMethod.POST,
            headers={"Content-Type": "application/json"},
            body=json.dumps(body or _DEFAULT_BODY).encode("utf-8"),
            oidc_token=scheduler_v1.OidcToken(
                service_account_email=self.runtime_sa,
                audience=oidc_audience,
            ),
        )
        # Attempt deadline upper bound (Duration). Use 3600s as a string
        # because the proto field accepts ``google.protobuf.duration_pb2``.
        from google.protobuf import duration_pb2

        deadline = duration_pb2.Duration(seconds=_DEFAULT_ATTEMPT_DEADLINE_SECONDS)

        return scheduler_v1.Job(
            name=self._job_path(),
            description="DSA daily watchlist analysis (managed by WebUI)",
            schedule=cron,
            time_zone=timezone,
            attempt_deadline=deadline,
            http_target=http_target,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(
        self,
        cron: str,
        timezone: str,
        enabled: bool = True,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create or update the job; pause it if ``enabled`` is False."""
        self._ensure_configured()
        client = self._get_client()
        from google.api_core.exceptions import NotFound  # type: ignore

        job = self._build_job(cron=cron, timezone=timezone, body=body or _DEFAULT_BODY)

        try:
            existing = client.get_job(name=self._job_path())
            updated = client.update_job(job=job)
            logger.info("[CloudScheduler] updated job %s", self.job_name)
            current = updated
        except NotFound:
            created = client.create_job(parent=self._parent(), job=job)
            logger.info("[CloudScheduler] created job %s", self.job_name)
            current = created

        # Honor enabled=False by pausing right after upsert
        if not enabled:
            client.pause_job(name=self._job_path())
            logger.info("[CloudScheduler] paused %s (enabled=False)", self.job_name)
            current = client.get_job(name=self._job_path())
        elif _job_is_paused(current):
            client.resume_job(name=self._job_path())
            logger.info("[CloudScheduler] resumed %s (enabled=True)", self.job_name)
            current = client.get_job(name=self._job_path())

        return _job_to_dict(current)

    def status(self) -> Dict[str, Any]:
        """Return current job state, or ``{"exists": False}``."""
        self._ensure_configured()
        client = self._get_client()
        from google.api_core.exceptions import NotFound  # type: ignore

        try:
            job = client.get_job(name=self._job_path())
            data = _job_to_dict(job)
            data["exists"] = True
            return data
        except NotFound:
            return {
                "exists": False,
                "job_name": self.job_name,
                "project_id": self.project_id,
                "region": self.region,
            }

    def run_now(self) -> Dict[str, Any]:
        """Trigger an out-of-schedule run immediately."""
        self._ensure_configured()
        client = self._get_client()
        client.run_job(name=self._job_path())
        return {"ok": True, "job": self.job_name}

    def pause(self) -> Dict[str, Any]:
        self._ensure_configured()
        client = self._get_client()
        job = client.pause_job(name=self._job_path())
        return _job_to_dict(job)

    def resume(self) -> Dict[str, Any]:
        self._ensure_configured()
        client = self._get_client()
        job = client.resume_job(name=self._job_path())
        return _job_to_dict(job)

    def delete(self) -> None:
        self._ensure_configured()
        client = self._get_client()
        client.delete_job(name=self._job_path())
        logger.info("[CloudScheduler] deleted job %s", self.job_name)


def _job_is_paused(job) -> bool:
    state = getattr(job, "state", None)
    if state is None:
        return False
    # google.cloud.scheduler_v1.Job.State.PAUSED == 2
    try:
        return int(state) == 2 or str(state).endswith("PAUSED")
    except Exception:
        return False


def _job_to_dict(job) -> Dict[str, Any]:
    """Project a Job proto into a JSON-serializable dict for the WebUI."""
    state_name: Optional[str] = None
    if getattr(job, "state", None) is not None:
        try:
            state_name = job.state.name  # type: ignore[attr-defined]
        except AttributeError:
            state_name = str(job.state)

    last_attempt = getattr(job, "last_attempt_time", None)
    schedule_time = getattr(job, "schedule_time", None)
    user_update_time = getattr(job, "user_update_time", None)

    def _ts(value):
        if value is None:
            return None
        try:
            return value.isoformat()
        except AttributeError:
            return str(value)

    return {
        "job_name": (job.name.split("/")[-1] if getattr(job, "name", "") else None),
        "schedule": getattr(job, "schedule", None),
        "time_zone": getattr(job, "time_zone", None),
        "state": state_name,
        "last_attempt_time": _ts(last_attempt),
        "next_run_time": _ts(schedule_time),
        "user_update_time": _ts(user_update_time),
    }
