# -*- coding: utf-8 -*-
"""High-level orchestrator for the trading framework.

Wires together: audit_repo → RiskEngine → Executor → notification.
Singleton via :func:`get_trading_service` (mirrors
``firstrade_sync_service.get_firstrade_sync_service``).

The submit pipeline is wrapped in a ``threading.Lock`` so two
concurrent requests for distinct ``request_uid`` serialise — keeps
the daily-turnover rollup race-free without needing a transactional
read-modify-write at the SQL layer.

Notification flooding is mitigated by a token-bucket rate limiter
(default 30 events/minute). When the bucket empties, audit rows
still write but notifications are skipped; a single summary
``[notification suppressed: rate limit]`` event is emitted at the
end of the burst.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import get_config
from src.trading.audit_repo import (
    DuplicateRequestUidError,
    TradeExecutionRepository,
)
from src.trading.executors import get_executor
from src.trading.risk_engine import RiskEngine
from src.trading.types import (
    ExecutionMode,
    ExecutionStatus,
    OrderRequest,
    OrderResult,
    RiskAssessment,
    RiskFlag,
    RiskFlagCode,
    RiskSeverity,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =====================================================================
# Notification rate limiter
# =====================================================================

class _NotificationRateLimiter:
    """Naive token-bucket. ``capacity`` events per ``window_seconds``.

    Thread-safe via a per-instance lock.
    """

    def __init__(self, capacity: int = 30, window_seconds: float = 60.0) -> None:
        self._capacity = max(1, int(capacity))
        self._window = float(window_seconds)
        self._tokens = float(self._capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._suppressed_count = 0

    def consume(self) -> bool:
        """Return True if the caller may notify; False if suppressed."""
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._last_refill)
            # Refill at capacity / window_seconds tokens per second
            refill = elapsed * (self._capacity / self._window)
            self._tokens = min(self._capacity, self._tokens + refill)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            self._suppressed_count += 1
            return False

    def take_suppressed_count(self) -> int:
        """Return + reset the count of suppressed events. Caller can
        emit a single summary event."""
        with self._lock:
            count = self._suppressed_count
            self._suppressed_count = 0
            return count


# =====================================================================
# Service
# =====================================================================

class TradingDisabledError(RuntimeError):
    """Raised when ``trading_mode='disabled'`` and a method that needs
    enabled mode is called. Maps to HTTP 503 in the API layer."""


class TradingExecutionService:
    """One per process. Construct via :func:`get_trading_service`."""

    def __init__(
        self,
        *,
        config: Any = None,
        audit_repo: Optional[TradeExecutionRepository] = None,
    ) -> None:
        self._config = config or get_config()
        self._audit_repo = audit_repo or TradeExecutionRepository()
        self._mutation_lock = threading.Lock()
        self._notification_limiter = _NotificationRateLimiter(
            capacity=30, window_seconds=60.0,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        cfg = self._config
        mode = getattr(cfg, "trading_mode", "disabled")
        if mode == "disabled":
            return {
                "status": "disabled",
                "mode": "disabled",
                "message": (
                    "Trading framework is disabled. "
                    "Set TRADING_MODE=paper to enable simulated execution."
                ),
            }
        return {
            "status": "ready",
            "mode": mode,
            "paper_account_id": getattr(cfg, "trading_paper_account_id", None),
            "max_position_value": getattr(cfg, "trading_max_position_value", 0.0),
            "max_position_pct": getattr(cfg, "trading_max_position_pct", 0.0),
            "max_daily_turnover": getattr(cfg, "trading_max_daily_turnover", 0.0),
            "symbol_allowlist": list(getattr(cfg, "trading_symbol_allowlist", []) or []),
            "symbol_denylist": list(getattr(cfg, "trading_symbol_denylist", []) or []),
            "market_hours_strict": bool(getattr(cfg, "trading_market_hours_strict", True)),
            "notification_enabled": bool(getattr(cfg, "trading_notification_enabled", True)),
        }

    def submit(self, request: OrderRequest) -> Dict[str, Any]:
        """Run the full submit pipeline and return the result dict
        ready for API serialisation."""
        cfg = self._config
        mode_str = getattr(cfg, "trading_mode", "disabled")
        if mode_str == "disabled":
            raise TradingDisabledError(
                "Trading framework is disabled. Set TRADING_MODE=paper."
            )
        try:
            mode = ExecutionMode(mode_str)
        except ValueError:
            raise TradingDisabledError(f"Unknown TRADING_MODE={mode_str!r}")

        with self._mutation_lock:
            return self._submit_locked(request, mode=mode)

    def _submit_locked(self, request: OrderRequest, *, mode: ExecutionMode) -> Dict[str, Any]:
        # (a) Audit row — start
        try:
            self._audit_repo.start_execution(request, mode=mode.value)
        except DuplicateRequestUidError:
            # Map to a structured failed result. The API layer turns
            # this into 409.
            failed = OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=mode,
                error_code="DUPLICATE_REQUEST_UID",
                error_message=(
                    f"request_uid={request.request_uid!r} already submitted; "
                    "audit row exists."
                ),
            )
            # Don't write a second audit row — original wins.
            return failed.to_dict()

        # (b) Risk engine
        try:
            assessment = self._evaluate_risk(request, mode=mode)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception("[trading-service] risk engine failed")
            result = OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=mode,
                error_code="RISK_ENGINE_ERROR",
                error_message=str(exc)[:240],
            )
            self._finalise(request, result)
            return result.to_dict()

        # (c) Block short-circuit
        if assessment.decision == "block":
            result = OrderResult(
                request=request,
                status=ExecutionStatus.BLOCKED,
                mode=mode,
                risk_assessment=assessment,
                error_code="RISK_BLOCKED",
                error_message=self._summarise_block_flags(assessment.flags),
            )
            self._finalise(request, result)
            return result.to_dict()

        # (d) Dispatch to executor
        try:
            executor = get_executor(mode, config=self._config)
        except NotImplementedError as exc:
            result = OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=mode,
                risk_assessment=assessment,
                error_code="LIVE_NOT_IMPLEMENTED",
                error_message=str(exc)[:240],
            )
            self._finalise(request, result)
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[trading-service] executor construction failed")
            result = OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=mode,
                risk_assessment=assessment,
                error_code="EXECUTOR_INIT_FAILED",
                error_message=str(exc)[:240],
            )
            self._finalise(request, result)
            return result.to_dict()

        # (e) Submit
        try:
            result = executor.submit(request, risk_assessment=assessment)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[trading-service] executor.submit raised")
            result = OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=mode,
                risk_assessment=assessment,
                error_code="EXECUTOR_RAISED",
                error_message=str(exc)[:240],
            )

        self._finalise(request, result)
        return result.to_dict()

    def list_recent_executions(
        self,
        *,
        mode: Optional[str] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        rows = self._audit_repo.list_recent_executions(
            mode=mode, account_id=account_id, symbol=symbol,
            status=status, limit=limit,
        )
        return {"items": rows, "count": len(rows)}

    def preview_risk(self, request: OrderRequest) -> Dict[str, Any]:
        """Run RiskEngine.evaluate WITHOUT persisting an audit row.
        Useful for the WebUI's "preview" button before a submit."""
        cfg = self._config
        mode_str = getattr(cfg, "trading_mode", "disabled")
        if mode_str == "disabled":
            raise TradingDisabledError("Trading framework is disabled.")
        try:
            mode = ExecutionMode(mode_str)
        except ValueError:
            raise TradingDisabledError(f"Unknown TRADING_MODE={mode_str!r}")
        assessment = self._evaluate_risk(request, mode=mode)
        return assessment.to_dict()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate_risk(
        self,
        request: OrderRequest,
        *,
        mode: ExecutionMode,
    ) -> RiskAssessment:
        engine = RiskEngine(self._config)
        portfolio_snapshot = self._fetch_portfolio_snapshot(request)
        broker_status = self._fetch_broker_status()
        daily_turnover = self._audit_repo.daily_turnover(mode=mode.value)
        estimated_price = self._estimate_price(request)
        return engine.evaluate(
            request,
            portfolio_snapshot=portfolio_snapshot,
            broker_status=broker_status,
            daily_turnover_so_far=daily_turnover,
            estimated_price=estimated_price,
        )

    def _fetch_portfolio_snapshot(
        self,
        request: OrderRequest,
    ) -> Optional[Dict[str, Any]]:
        try:
            from src.services.portfolio_service import PortfolioService
            account_id = (
                request.account_id
                if request.account_id is not None
                else getattr(self._config, "trading_paper_account_id", None)
            )
            if account_id is None:
                return None
            svc = PortfolioService()
            snapshot = svc.get_portfolio_snapshot(account_id=int(account_id))
            return snapshot
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[trading-service] portfolio snapshot unavailable: %s", exc,
            )
            return None

    def _fetch_broker_status(self) -> Optional[Dict[str, Any]]:
        try:
            from src.services.firstrade_sync_service import (
                get_firstrade_sync_service,
            )
            svc = get_firstrade_sync_service()
            return svc.get_status() if svc else None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[trading-service] broker status unavailable: %s", exc,
            )
            return None

    def _estimate_price(self, request: OrderRequest) -> Optional[float]:
        """Best-effort price estimate for the risk engine. Tries
        Firstrade quote, then data-provider chain. Returns None if
        nothing is available — the risk engine downgrades certain
        checks to WARNING in that case."""
        # 1. Firstrade
        try:
            from src.services.firstrade_sync_service import (
                get_firstrade_sync_service,
            )
            svc = get_firstrade_sync_service()
            q = svc.get_quote(request.symbol) if svc else None
            if q:
                last = q.get("last") or q.get("ask") or q.get("bid")
                if last is not None:
                    return float(last)
        except Exception:  # noqa: BLE001
            pass
        # 2. Data provider chain
        try:
            from data_provider import DataFetcherManager
            mgr = DataFetcherManager()
            rt = mgr.get_realtime_quote(request.symbol, log_final_failure=False)
            if rt is not None:
                price = (
                    getattr(rt, "price", None)
                    or getattr(rt, "last", None)
                    or getattr(rt, "close", None)
                )
                return float(price) if price is not None else None
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _summarise_block_flags(flags: List[RiskFlag]) -> str:
        block_msgs = [
            f.message for f in flags if f.severity == RiskSeverity.BLOCK
        ]
        if not block_msgs:
            return "blocked by risk engine"
        return "; ".join(block_msgs[:3])

    def _finalise(self, request: OrderRequest, result: OrderResult) -> None:
        # Audit update — best-effort
        try:
            self._audit_repo.finish_execution(request.request_uid, result)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[trading-service] audit finalise failed for uid=%s: %s",
                request.request_uid, exc,
            )
        # Notification — also best-effort (must not break the flow)
        if getattr(self._config, "trading_notification_enabled", True):
            self._notify(request, result)

    def _notify(self, request: OrderRequest, result: OrderResult) -> None:
        if not self._notification_limiter.consume():
            return  # silently dropped; summary emitted later
        try:
            self._send_notification(request, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[trading-service] notification failed: %s", exc)

    def _send_notification(
        self,
        request: OrderRequest,
        result: OrderResult,
    ) -> None:
        """Wrap the existing pluggable senders.

        Imported lazily so unit tests don't need the notification
        config wired up. If ``NotificationService`` isn't available
        in this build, log and move on.
        """
        try:
            from src.notification import NotificationService
        except ImportError:
            return
        try:
            svc = NotificationService()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[trading-service] NotificationService init failed: %s", exc,
            )
            return
        title = (
            f"[Trading {result.mode.value.upper()}] "
            f"{request.side.value.upper()} {request.symbol} × {request.quantity} "
            f"→ {result.status.value}"
        )
        body_lines = [
            f"Symbol: {request.symbol}",
            f"Side: {request.side.value}",
            f"Quantity: {request.quantity}",
            f"Order type: {request.order_type.value}",
            f"Limit price: {request.limit_price}",
            f"Mode: {result.mode.value}",
            f"Status: {result.status.value}",
        ]
        if result.fill_price is not None:
            body_lines.append(f"Fill price: {result.fill_price}")
        if result.error_code:
            body_lines.append(f"Error: {result.error_code} {result.error_message or ''}")
        if result.risk_assessment is not None:
            body_lines.append(f"Risk decision: {result.risk_assessment.decision}")
        try:
            # Prefer the broadcast / send method names commonly used in
            # the existing service. Different builds may expose either
            # ``send`` or ``send_message``; we try both.
            send = (
                getattr(svc, "send", None)
                or getattr(svc, "send_message", None)
                or getattr(svc, "broadcast", None)
            )
            if callable(send):
                send(title=title, content="\n".join(body_lines))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[trading-service] notification dispatch failed: %s", exc,
            )


# =====================================================================
# Module-level singleton accessor
# =====================================================================

_INSTANCE: Optional[TradingExecutionService] = None
_INSTANCE_LOCK = threading.Lock()


def get_trading_service() -> TradingExecutionService:
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = TradingExecutionService()
    return _INSTANCE


def reset_trading_service() -> None:
    """Test helper. Drops the cached singleton."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
