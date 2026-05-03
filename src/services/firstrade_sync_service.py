# -*- coding: utf-8 -*-
"""High-level orchestration for the Firstrade read-only integration.

The service is the public face the API endpoints, the WebUI panel, and
the agent tool reach for. Below it sit:

  * :class:`brokers.firstrade.client.FirstradeReadOnlyClient` — the
    only place that touches the unofficial vendor SDK. Singleton.
  * :class:`repositories.broker_snapshot_repo.BrokerSnapshotRepository`
    — single redaction-enforcing write path to SQLite.

Above it sit only callers that wrap structured ``Dict[str, Any]``
responses; they never see raw vendor exceptions, never see credentials
or full account numbers, never call any trading code path.

Design invariants:

* ``FirstradeSyncService`` is a process-wide singleton so the
  ``FTSession`` survives between ``login()`` and ``verify_mfa(code)``.
* ``threading.Lock`` serialises ``sync_now()`` so two concurrent
  WebUI clicks don't double-write snapshots.
* Every public method returns a ``Dict[str, Any]`` with a stable
  ``status`` field (``ok`` / ``not_enabled`` / ``not_installed`` /
  ``login_required`` / ``mfa_required`` / ``session_lost`` /
  ``failed``); the API layer maps these to HTTP status codes.
* Failed syncs still write a ``status='failed'`` row so the operator
  can see why without grepping logs.
* Reads that come from local snapshots never hit the vendor SDK — even
  when the user is logged in.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from src.brokers.base import redact_sensitive_payload
from src.brokers.firstrade.client import FirstradeReadOnlyClient

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scrub_message(message: Optional[str]) -> Optional[str]:
    """Belt-and-suspenders: even though the client already sanitises
    its exception strings, every API-facing message goes through the
    redactor one more time before it leaves the service."""
    if not message:
        return message
    payload = redact_sensitive_payload({"_": message})
    return payload.get("_") if isinstance(payload, dict) else message


# =====================================================================
# Service singleton
# =====================================================================

_SERVICE_SINGLETON: Optional["FirstradeSyncService"] = None
_SINGLETON_LOCK = threading.Lock()


def get_firstrade_sync_service() -> "FirstradeSyncService":
    """Return the process-level :class:`FirstradeSyncService`.

    A singleton is required because the underlying client holds the
    ``FTSession``, which must survive across the two-step MFA flow.
    """
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is not None:
        return _SERVICE_SINGLETON
    with _SINGLETON_LOCK:
        if _SERVICE_SINGLETON is None:
            _SERVICE_SINGLETON = FirstradeSyncService()
    return _SERVICE_SINGLETON


def _reset_firstrade_sync_service_for_tests() -> None:
    """Hook used by tests to inject a fresh singleton between cases.

    Production code MUST NOT call this — the singleton owns the
    half-logged-in MFA session.
    """
    global _SERVICE_SINGLETON
    with _SINGLETON_LOCK:
        _SERVICE_SINGLETON = None


# =====================================================================
# Service
# =====================================================================

class FirstradeSyncService:
    """Coordinates Firstrade login + sync + local read."""

    BROKER_NAME = "firstrade"

    def __init__(
        self,
        config: Any = None,
        client: Optional[FirstradeReadOnlyClient] = None,
        repo: Optional[Any] = None,
    ) -> None:
        self._config = config or self._resolve_config()
        self._client = client or FirstradeReadOnlyClient(self._config)
        self._repo = repo  # lazily instantiated to keep imports light when disabled
        # Two-call MFA flow + sync_now must not interleave.
        self._mutation_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Status & gate checks
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        if not self._is_enabled():
            return {
                "status": "not_enabled",
                "broker": self.BROKER_NAME,
                "enabled": False,
                "message": "Set BROKER_FIRSTRADE_ENABLED=true to enable Firstrade read-only sync.",
            }
        try:
            repo = self._get_repo()
        except _ImportFailed as exc:
            return {
                "status": "not_installed",
                "broker": self.BROKER_NAME,
                "enabled": True,
                "message": str(exc),
            }
        last_sync = repo.get_last_sync_run(self.BROKER_NAME)
        return {
            "status": "ok",
            "broker": self.BROKER_NAME,
            "enabled": True,
            "logged_in": bool(self._client.is_logged_in()),
            "read_only": bool(getattr(self._config, "broker_firstrade_read_only", True)),
            "last_sync": last_sync,
            "llm_data_scope": getattr(
                self._config, "broker_firstrade_llm_data_scope",
                "positions_and_balances",
            ),
        }

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> Dict[str, Any]:
        if not self._is_enabled():
            return self._not_enabled_response()
        with self._mutation_lock:
            result = self._client.login()
            return {
                "status": result.status,
                "broker": self.BROKER_NAME,
                "message": _scrub_message(result.message),
                "account_count": int(result.account_count or 0),
            }

    def verify_mfa(self, code: str) -> Dict[str, Any]:
        if not self._is_enabled():
            return self._not_enabled_response()
        if not code or not str(code).strip():
            return {
                "status": "failed",
                "broker": self.BROKER_NAME,
                "message": "Verification code is required.",
            }
        with self._mutation_lock:
            result = self._client.verify_mfa(str(code).strip())
            return {
                "status": result.status,
                "broker": self.BROKER_NAME,
                "message": _scrub_message(result.message),
                "account_count": int(result.account_count or 0),
            }

    def logout(self) -> Dict[str, Any]:
        with self._mutation_lock:
            self._client.logout()
        return {"status": "ok", "broker": self.BROKER_NAME, "message": "Logged out."}

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_now(self, *, date_range: str = "today") -> Dict[str, Any]:
        """Pull a full snapshot from Firstrade into the local repo.

        Always writes a ``broker_sync_runs`` row — successful or
        failed — so the WebUI / API has a consistent record of "when
        was the last attempt" for diagnostics.
        """
        if not self._is_enabled():
            return self._not_enabled_response()
        try:
            repo = self._get_repo()
        except _ImportFailed as exc:
            return {
                "status": "not_installed",
                "broker": self.BROKER_NAME,
                "message": str(exc),
            }

        if not self._client.is_logged_in():
            return {
                "status": "login_required",
                "broker": self.BROKER_NAME,
                "message": "Login first via POST /api/v1/broker/firstrade/login.",
            }

        with self._mutation_lock:
            run_id = repo.save_sync_run_start(broker=self.BROKER_NAME)
            try:
                snapshot = self._client.build_snapshot(date_range=date_range)
                counts = repo.save_full_snapshot(snapshot, broker=self.BROKER_NAME)
            except Exception as exc:  # noqa: BLE001 — boundary
                from src.brokers.firstrade.client import _sanitize_exception
                clean = _sanitize_exception(exc)
                logger.warning("[firstrade] sync_now failed: %s", clean)
                repo.finish_sync_run(
                    run_id,
                    status="failed",
                    message=clean,
                    error_payload={"error": clean},
                )
                return {
                    "status": "failed",
                    "broker": self.BROKER_NAME,
                    "message": clean,
                }

            repo.finish_sync_run(
                run_id,
                status="ok",
                account_count=counts.get("accounts", 0),
                position_count=counts.get("positions", 0),
                order_count=counts.get("orders", 0),
                transaction_count=counts.get("transactions", 0),
                message="Sync completed.",
            )
            return {
                "status": "ok",
                "broker": self.BROKER_NAME,
                "as_of": snapshot.as_of,
                "account_count": counts.get("accounts", 0),
                "balance_count": counts.get("balances", 0),
                "position_count": counts.get("positions", 0),
                "order_count": counts.get("orders", 0),
                "transaction_count": counts.get("transactions", 0),
            }

    # ------------------------------------------------------------------
    # Local snapshot reads (never hit the vendor SDK)
    # ------------------------------------------------------------------

    def get_accounts(self) -> Dict[str, Any]:
        return self._read_local(
            lambda repo: {"items": repo.get_latest_accounts(self.BROKER_NAME)},
        )

    def get_positions(self, account_hash: Optional[str] = None) -> Dict[str, Any]:
        return self._read_local(
            lambda repo: {
                "items": repo.get_latest_positions(
                    self.BROKER_NAME, account_hash=account_hash,
                ),
            },
        )

    def get_orders(self, account_hash: Optional[str] = None) -> Dict[str, Any]:
        return self._read_local(
            lambda repo: {
                "items": repo.get_latest_orders(
                    self.BROKER_NAME, account_hash=account_hash,
                ),
            },
        )

    def get_transactions(
        self, account_hash: Optional[str] = None, limit: int = 50,
    ) -> Dict[str, Any]:
        return self._read_local(
            lambda repo: {
                "items": repo.get_latest_transactions(
                    self.BROKER_NAME,
                    account_hash=account_hash,
                    limit=limit,
                ),
            },
        )

    def get_snapshot(self) -> Dict[str, Any]:
        """Convenience: full snapshot (accounts + balances + positions
        + orders + transactions) for the agent tool / WebUI."""
        return self._read_local(
            lambda repo: repo.get_latest_snapshot(self.BROKER_NAME),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_local(self, fn) -> Dict[str, Any]:
        if not self._is_enabled():
            return self._not_enabled_response()
        try:
            repo = self._get_repo()
        except _ImportFailed as exc:
            return {
                "status": "not_installed",
                "broker": self.BROKER_NAME,
                "message": str(exc),
            }
        try:
            data = fn(repo) or {}
        except Exception as exc:  # noqa: BLE001 — boundary
            logger.warning("[firstrade] local read failed: %s", exc)
            return {
                "status": "failed",
                "broker": self.BROKER_NAME,
                "message": "Failed to read local broker snapshot.",
            }
        return {
            "status": "ok",
            "broker": self.BROKER_NAME,
            **data,
        }

    def _is_enabled(self) -> bool:
        return bool(getattr(self._config, "broker_firstrade_enabled", False))

    def _resolve_config(self) -> Any:
        from src.config import get_config
        return get_config()

    def _get_repo(self):
        if self._repo is not None:
            return self._repo
        try:
            from src.repositories.broker_snapshot_repo import BrokerSnapshotRepository
        except ImportError as exc:  # pragma: no cover — defensive
            raise _ImportFailed(str(exc)) from exc
        self._repo = BrokerSnapshotRepository()
        return self._repo

    def _not_enabled_response(self) -> Dict[str, Any]:
        return {
            "status": "not_enabled",
            "broker": self.BROKER_NAME,
            "message": "Set BROKER_FIRSTRADE_ENABLED=true to enable Firstrade read-only sync.",
        }


class _ImportFailed(RuntimeError):
    """Raised when the optional broker dependency / repository module
    fails to import — surfaced as ``not_installed`` upstream."""
