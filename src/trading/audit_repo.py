# -*- coding: utf-8 -*-
"""Repository for the ``trade_executions`` audit table.

Mirrors :class:`BrokerSnapshotRepository` style: takes an optional
``DatabaseManager`` for test injection, falls back to the singleton
in production. Two-phase write pattern:

  1. ``start_execution(req)`` writes a row with status='pending' and
     the full request payload — happens BEFORE the risk engine runs
     so even a crashed RiskEngine leaves a forensic trail.

  2. ``finish_execution(request_uid, result)`` updates the row with
     the final status, fill details, risk flags, and result payload.

Lookup helpers are used by the WebUI panel and the RiskEngine's
daily-turnover rollup query.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, func, select
from sqlalchemy.exc import IntegrityError

from src.storage import DatabaseManager, TradeExecution
from src.trading.types import (
    ExecutionStatus,
    OrderRequest,
    OrderResult,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_dumps(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("[trade_audit] payload not JSON-serialisable: %s", exc)
        return json.dumps({"_serialisation_error": str(exc)})


def _safe_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class DuplicateRequestUidError(Exception):
    """Raised on idempotency-key collision — caller should map to 409."""


class TradeExecutionRepository:
    """Persistence for the audit table. Single-writer per process via
    SQLAlchemy session; the orchestrator's ``_mutation_lock`` makes it
    thread-safe at the call-site level.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def start_execution(self, request: OrderRequest, *, mode: str) -> int:
        """Insert a ``status='pending'`` row for the request.

        Returns the inserted row id. Raises ``DuplicateRequestUidError``
        if ``request.request_uid`` is not unique (idempotency violated)."""
        if not request.request_uid:
            raise ValueError("OrderRequest.request_uid is required for audit")
        with self.db.get_session() as session:
            existing = session.execute(
                select(TradeExecution.id).where(
                    TradeExecution.request_uid == request.request_uid,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise DuplicateRequestUidError(
                    f"request_uid={request.request_uid!r} already exists "
                    f"(audit row id={existing})"
                )
            row = TradeExecution(
                request_uid=request.request_uid,
                mode=mode,
                source=request.source or "ui",
                symbol=request.symbol.upper(),
                side=request.side.value,
                order_type=request.order_type.value,
                quantity=float(request.quantity),
                limit_price=(
                    float(request.limit_price)
                    if request.limit_price is not None else None
                ),
                account_id=request.account_id,
                market=request.market,
                currency=request.currency,
                status=ExecutionStatus.PENDING.value,
                request_payload_json=_safe_dumps(request.to_dict()) or "{}",
                agent_session_id=request.agent_session_id,
                requested_at=_utc_now(),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise DuplicateRequestUidError(
                    f"request_uid={request.request_uid!r} already exists"
                ) from exc
            session.refresh(row)
            return int(row.id)

    def finish_execution(self, request_uid: str, result: OrderResult) -> None:
        """Update the row identified by ``request_uid`` with the final
        outcome from the executor or risk engine."""
        with self.db.get_session() as session:
            row = session.execute(
                select(TradeExecution).where(
                    TradeExecution.request_uid == request_uid,
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "[trade_audit] finish_execution: no pending row for "
                    "request_uid=%s; skipping",
                    request_uid,
                )
                return
            row.status = result.status.value
            row.fill_price = result.fill_price
            row.fill_quantity = result.fill_quantity
            row.realised_fee = float(result.realised_fee or 0.0)
            row.realised_tax = float(result.realised_tax or 0.0)
            row.portfolio_trade_id = result.portfolio_trade_id
            row.error_code = result.error_code
            row.error_message = (
                result.error_message[:500] if result.error_message else None
            )
            if result.risk_assessment is not None:
                row.risk_decision = result.risk_assessment.decision
                row.risk_flags_json = _safe_dumps(
                    [f.to_dict() for f in result.risk_assessment.flags]
                )
            # Strip the request key from the result payload — we already
            # have ``request_payload_json`` in its own column. Keeps the
            # row size down and makes joins easier.
            result_dict = result.to_dict()
            result_dict.pop("request", None)
            row.result_payload_json = _safe_dumps(result_dict)
            row.finished_at = _utc_now()
            session.commit()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def list_recent_executions(
        self,
        *,
        mode: Optional[str] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(TradeExecution).order_by(
                desc(TradeExecution.requested_at), desc(TradeExecution.id),
            )
            if mode:
                stmt = stmt.where(TradeExecution.mode == mode)
            if account_id is not None:
                stmt = stmt.where(TradeExecution.account_id == account_id)
            if symbol:
                stmt = stmt.where(TradeExecution.symbol == symbol.upper())
            if status:
                stmt = stmt.where(TradeExecution.status == status)
            stmt = stmt.limit(max(1, min(int(limit or 50), 500)))
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_dict(row) for row in rows]

    def get_by_request_uid(self, request_uid: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(TradeExecution).where(
                    TradeExecution.request_uid == request_uid,
                )
            ).scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    def daily_turnover(
        self,
        *,
        as_of: Optional[date] = None,
        mode: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> float:
        """Sum of ``quantity * (fill_price | limit_price)`` for all
        non-blocked, non-failed rows on the given UTC day. Used by the
        RiskEngine to enforce ``trading_max_daily_turnover``."""
        target_day = as_of or _utc_now().date()
        day_start = datetime.combine(target_day, datetime.min.time())
        day_end = datetime.combine(target_day, datetime.max.time())
        with self.db.get_session() as session:
            stmt = (
                select(TradeExecution)
                .where(
                    and_(
                        TradeExecution.requested_at >= day_start,
                        TradeExecution.requested_at <= day_end,
                        TradeExecution.status.in_(
                            (ExecutionStatus.PENDING.value, ExecutionStatus.FILLED.value)
                        ),
                    )
                )
            )
            if mode:
                stmt = stmt.where(TradeExecution.mode == mode)
            if account_id is not None:
                stmt = stmt.where(TradeExecution.account_id == account_id)
            rows = session.execute(stmt).scalars().all()
        total = 0.0
        for row in rows:
            qty = float(row.quantity or 0)
            # Prefer the realised fill price; fall back to limit_price for
            # pending limits — generous so the engine never *under*-counts
            # turnover at decision time.
            price = (
                float(row.fill_price)
                if row.fill_price is not None
                else float(row.limit_price)
                if row.limit_price is not None
                else 0.0
            )
            total += qty * price
        return round(total, 6)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: TradeExecution) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "request_uid": row.request_uid,
            "mode": row.mode,
            "source": row.source,
            "symbol": row.symbol,
            "side": row.side,
            "order_type": row.order_type,
            "quantity": float(row.quantity),
            "limit_price": (
                float(row.limit_price) if row.limit_price is not None else None
            ),
            "account_id": int(row.account_id) if row.account_id is not None else None,
            "market": row.market,
            "currency": row.currency,
            "status": row.status,
            "risk_decision": row.risk_decision,
            "risk_flags": _safe_loads(row.risk_flags_json) or [],
            "fill_price": (
                float(row.fill_price) if row.fill_price is not None else None
            ),
            "fill_quantity": (
                float(row.fill_quantity) if row.fill_quantity is not None else None
            ),
            "realised_fee": float(row.realised_fee or 0.0),
            "realised_tax": float(row.realised_tax or 0.0),
            "portfolio_trade_id": (
                int(row.portfolio_trade_id)
                if row.portfolio_trade_id is not None else None
            ),
            "request_payload": _safe_loads(row.request_payload_json) or {},
            "result_payload": _safe_loads(row.result_payload_json),
            "error_code": row.error_code,
            "error_message": row.error_message,
            "agent_session_id": row.agent_session_id,
            "requested_at": (
                row.requested_at.isoformat() if row.requested_at else None
            ),
            "finished_at": (
                row.finished_at.isoformat() if row.finished_at else None
            ),
            "created_at": (
                row.created_at.isoformat() if row.created_at else None
            ),
        }
