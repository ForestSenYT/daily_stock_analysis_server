# -*- coding: utf-8 -*-
"""Background auto-sync for the Firstrade broker integration.

When ``BROKER_FIRSTRADE_AUTO_SYNC_ENABLED=true``, the FastAPI server
starts a daemon thread that fires ``firstrade_sync_service.sync_now()``
every ``BROKER_FIRSTRADE_AUTO_SYNC_INTERVAL_MINUTES`` minutes.

Behaviour rules:

  * **Best-effort**: any failure (session lost, vendor 5xx, etc.)
    is logged and the loop continues. The next tick attempts a fresh
    sync. Phase A only — Phase B might add exponential backoff.
  * **Login-aware**: if the broker isn't logged in, skip silently
    (the user logs in via WebUI; next tick after login picks up).
  * **Cleanly stoppable**: a ``threading.Event`` is set on shutdown;
    the loop exits within ``min(interval, 5s)``.
  * **Single-process singleton**: ``start_auto_sync()`` is idempotent.
    Multiple Cloud Run instances each run their own loop — that's
    fine because ``FirstradeSyncService.sync_now`` is internally
    serialised via its mutation lock.

This module is **safe to import** even when the broker package isn't
installed — the lazy import inside the worker keeps startup decoupled
from optional deps.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT: Optional[threading.Event] = None
_START_LOCK = threading.Lock()


def _interval_seconds(config: Any) -> float:
    minutes = int(getattr(config, "broker_firstrade_auto_sync_interval_minutes", 30))
    return float(max(1, minutes) * 60)


def _is_enabled(config: Any) -> bool:
    return (
        bool(getattr(config, "broker_firstrade_auto_sync_enabled", False))
        and bool(getattr(config, "broker_firstrade_enabled", False))
    )


def _run_one_sync() -> None:
    """Execute one sync_now() best-effort. Logs success / failure
    without re-raising."""
    try:
        from src.services.firstrade_sync_service import (
            get_firstrade_sync_service,
        )
        svc = get_firstrade_sync_service()
        if svc is None:
            return
        # ``sync_now`` already handles "not logged in" by returning a
        # structured payload — we don't need to short-circuit here.
        result = svc.sync_now()
        status = (result or {}).get("status", "unknown")
        if status == "ok":
            counts = (
                f"accounts={result.get('account_count', 0)}, "
                f"positions={result.get('position_count', 0)}, "
                f"orders={result.get('order_count', 0)}, "
                f"transactions={result.get('transaction_count', 0)}"
            )
            logger.info("[broker-auto-sync] tick ok (%s)", counts)
        else:
            logger.info(
                "[broker-auto-sync] tick skipped (status=%s, message=%s)",
                status, (result or {}).get("message"),
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("[broker-auto-sync] tick raised: %s", exc)


def _worker(stop_event: threading.Event) -> None:
    from src.config import get_config

    logger.info("[broker-auto-sync] worker thread started")
    while not stop_event.is_set():
        config = get_config()
        # Re-check enabled each tick so a runtime config flip can
        # disable the loop without a process restart.
        if not _is_enabled(config):
            logger.info(
                "[broker-auto-sync] disabled at tick start; exiting worker"
            )
            return
        interval = _interval_seconds(config)
        # Run the sync first (so a fresh container syncs immediately
        # rather than waiting for the first interval to elapse).
        _run_one_sync()
        # Sleep in small chunks so shutdown is responsive even when
        # the interval is large (e.g. 30 min).
        deadline = time.monotonic() + interval
        while not stop_event.is_set() and time.monotonic() < deadline:
            stop_event.wait(timeout=min(5.0, deadline - time.monotonic()))
    logger.info("[broker-auto-sync] worker thread stopped")


def start_auto_sync() -> bool:
    """Spin up the background worker if not already running. Idempotent.

    Returns True if a fresh worker started, False if one was already
    active OR the feature is disabled in config.
    """
    global _WORKER_THREAD, _STOP_EVENT
    from src.config import get_config

    config = get_config()
    if not _is_enabled(config):
        logger.info(
            "[broker-auto-sync] disabled (auto_sync_enabled=%s, "
            "firstrade_enabled=%s)",
            getattr(config, "broker_firstrade_auto_sync_enabled", False),
            getattr(config, "broker_firstrade_enabled", False),
        )
        return False

    with _START_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            logger.debug("[broker-auto-sync] worker already running")
            return False
        _STOP_EVENT = threading.Event()
        _WORKER_THREAD = threading.Thread(
            target=_worker,
            args=(_STOP_EVENT,),
            name="broker-auto-sync",
            daemon=True,
        )
        _WORKER_THREAD.start()
        logger.info(
            "[broker-auto-sync] started (interval=%d min)",
            int(getattr(config, "broker_firstrade_auto_sync_interval_minutes", 30)),
        )
        return True


def stop_auto_sync(*, timeout: float = 10.0) -> None:
    """Signal the worker to stop and wait up to ``timeout`` seconds."""
    global _WORKER_THREAD, _STOP_EVENT
    with _START_LOCK:
        if _STOP_EVENT is not None:
            _STOP_EVENT.set()
        worker = _WORKER_THREAD
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)


def is_running() -> bool:
    return _WORKER_THREAD is not None and _WORKER_THREAD.is_alive()
