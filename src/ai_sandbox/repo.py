# -*- coding: utf-8 -*-
"""Repository layer for AI sandbox + training labels.

Mirrors :class:`TradeExecutionRepository` style: optional
``DatabaseManager`` injection (test-friendly), JSON payload columns,
defensive error handling on commit.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.exc import IntegrityError

from src.storage import AISandboxExecution, AITrainingLabel, DatabaseManager
from src.ai_sandbox.types import (
    AISandboxIntent,
    AISandboxResult,
    LabelKind,
    PnlHorizons,
)
from src.trading.types import ExecutionStatus

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_dumps(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("[ai_sandbox_repo] payload not JSON-serialisable: %s", exc)
        return json.dumps({"_serialisation_error": str(exc)})


def _safe_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class DuplicateSandboxRequestError(Exception):
    """Raised when a sandbox request_uid already exists."""


class DuplicateLabelError(Exception):
    """Raised when (source_kind, source_id) already has a label
    and the caller used create-only path."""


# =====================================================================
# AISandboxRepository
# =====================================================================

class AISandboxRepository:
    """Persistence for ``ai_sandbox_executions``."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    # -------- Write path --------

    def start_execution(self, intent: AISandboxIntent) -> int:
        """Insert a ``status='pending'`` row. Returns row id.
        Raises ``DuplicateSandboxRequestError`` on UNIQUE conflict."""
        if not intent.request_uid:
            raise ValueError("AISandboxIntent.request_uid is required")
        with self.db.get_session() as session:
            row = AISandboxExecution(
                request_uid=intent.request_uid,
                symbol=intent.symbol.upper(),
                side=intent.side.value,
                order_type=intent.order_type.value,
                quantity=float(intent.quantity),
                limit_price=(
                    float(intent.limit_price)
                    if intent.limit_price is not None else None
                ),
                market=intent.market,
                currency=intent.currency,
                agent_run_id=intent.agent_run_id or None,
                prompt_version=intent.prompt_version or None,
                confidence_score=(
                    float(intent.confidence_score)
                    if intent.confidence_score is not None else None
                ),
                reasoning_text=(intent.reasoning_text or None),
                model_used=intent.model_used or None,
                status=ExecutionStatus.PENDING.value,
                intent_payload_json=_safe_dumps(intent.to_dict()) or "{}",
                requested_at=_utc_now(),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise DuplicateSandboxRequestError(
                    f"request_uid={intent.request_uid!r} already exists"
                ) from exc
            session.refresh(row)
            return int(row.id)

    def finish_execution(
        self, request_uid: str, result: AISandboxResult,
    ) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(AISandboxExecution).where(
                    AISandboxExecution.request_uid == request_uid,
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "[ai_sandbox_repo] finish_execution: no row for uid=%s",
                    request_uid,
                )
                return
            row.status = result.status.value
            row.fill_price = result.fill_price
            row.fill_quantity = result.fill_quantity
            if result.fill_time:
                try:
                    # Accept ISO strings; coerce to naive UTC for SQLite parity
                    parsed = datetime.fromisoformat(
                        result.fill_time.replace("Z", "+00:00"),
                    )
                    row.fill_time = parsed.replace(tzinfo=None)
                except ValueError:
                    row.fill_time = None
            if result.risk_assessment is not None:
                row.risk_decision = result.risk_assessment.decision
                row.risk_flags_json = _safe_dumps(
                    [f.to_dict() for f in result.risk_assessment.flags]
                )
            row.error_code = result.error_code
            row.error_message = (
                result.error_message[:500] if result.error_message else None
            )
            row.quote_payload_json = _safe_dumps(result.quote_payload)
            # Strip ``intent`` from result payload — it's already in
            # the dedicated ``intent_payload_json`` column
            result_dict = result.to_dict()
            result_dict.pop("intent", None)
            row.result_payload_json = _safe_dumps(result_dict)
            session.commit()

    def update_pnl_horizons(
        self, request_uid: str, horizons: PnlHorizons,
    ) -> bool:
        """Fill in P&L horizons for an already-finished row.
        Returns True on success."""
        with self.db.get_session() as session:
            row = session.execute(
                select(AISandboxExecution).where(
                    AISandboxExecution.request_uid == request_uid,
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.pnl_horizons_json = _safe_dumps(horizons.to_dict())
            row.pnl_computed_at = _utc_now()
            session.commit()
            return True

    # -------- Read path --------

    def list_executions(
        self,
        *,
        agent_run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        prompt_version: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(AISandboxExecution).order_by(
                desc(AISandboxExecution.requested_at),
                desc(AISandboxExecution.id),
            )
            if agent_run_id:
                stmt = stmt.where(AISandboxExecution.agent_run_id == agent_run_id)
            if symbol:
                stmt = stmt.where(AISandboxExecution.symbol == symbol.upper())
            if status:
                stmt = stmt.where(AISandboxExecution.status == status)
            if prompt_version:
                stmt = stmt.where(
                    AISandboxExecution.prompt_version == prompt_version,
                )
            stmt = stmt.limit(max(1, min(int(limit or 50), 500)))
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_dict(row) for row in rows]

    def find_pending_pnl_computation(
        self,
        *,
        min_age_days: int = 1,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return filled rows whose P&L horizons haven't been computed
        yet AND that are old enough that at least the 1d horizon has
        passed. Used by the periodic P&L rollup."""
        cutoff = _utc_now() - timedelta(days=int(min_age_days))
        with self.db.get_session() as session:
            stmt = (
                select(AISandboxExecution)
                .where(
                    and_(
                        AISandboxExecution.status == ExecutionStatus.FILLED.value,
                        AISandboxExecution.pnl_horizons_json.is_(None),
                        AISandboxExecution.fill_time.isnot(None),
                        AISandboxExecution.fill_time <= cutoff,
                    )
                )
                .order_by(AISandboxExecution.fill_time)
                .limit(max(1, min(int(limit or 100), 500)))
            )
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_dict(row) for row in rows]

    def aggregate_metrics(
        self,
        *,
        since_days: Optional[int] = None,
        prompt_version: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate win-rate / P&L-style metrics over the sandbox
        history. Computed in Python (rows are typically << 10k)."""
        with self.db.get_session() as session:
            stmt = select(AISandboxExecution).where(
                AISandboxExecution.status == ExecutionStatus.FILLED.value,
            )
            if since_days is not None and since_days > 0:
                cutoff = _utc_now() - timedelta(days=int(since_days))
                stmt = stmt.where(AISandboxExecution.requested_at >= cutoff)
            if prompt_version:
                stmt = stmt.where(
                    AISandboxExecution.prompt_version == prompt_version,
                )
            if symbol:
                stmt = stmt.where(AISandboxExecution.symbol == symbol.upper())
            rows = session.execute(stmt).scalars().all()

        n_total = len(rows)
        n_filled = sum(1 for r in rows if r.fill_price is not None)
        n_with_pnl = 0
        wins_1d = wins_7d = 0
        sum_1d = 0.0
        sum_7d = 0.0
        for row in rows:
            horizons = _safe_loads(row.pnl_horizons_json) or {}
            h1 = horizons.get("horizon_1d")
            h7 = horizons.get("horizon_7d")
            if h1 is not None or h7 is not None:
                n_with_pnl += 1
            if h1 is not None:
                sum_1d += float(h1)
                # Convention: "win" = positive PnL on BUY,
                #             negative PnL on SELL
                side = (row.side or "").lower()
                if (side == "buy" and h1 > 0) or (side == "sell" and h1 < 0):
                    wins_1d += 1
            if h7 is not None:
                sum_7d += float(h7)
                side = (row.side or "").lower()
                if (side == "buy" and h7 > 0) or (side == "sell" and h7 < 0):
                    wins_7d += 1
        win_rate_1d = (wins_1d / n_with_pnl) if n_with_pnl else None
        win_rate_7d = (wins_7d / n_with_pnl) if n_with_pnl else None
        avg_pnl_1d = (sum_1d / n_with_pnl) if n_with_pnl else None
        avg_pnl_7d = (sum_7d / n_with_pnl) if n_with_pnl else None
        return {
            "total_executions": n_total,
            "filled_count": n_filled,
            "with_pnl_count": n_with_pnl,
            "win_rate_1d": win_rate_1d,
            "win_rate_7d": win_rate_7d,
            "avg_pnl_1d_pct": avg_pnl_1d,
            "avg_pnl_7d_pct": avg_pnl_7d,
            "filters": {
                "since_days": since_days,
                "prompt_version": prompt_version,
                "symbol": symbol,
            },
        }

    @staticmethod
    def _row_to_dict(row: AISandboxExecution) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "request_uid": row.request_uid,
            "symbol": row.symbol,
            "side": row.side,
            "order_type": row.order_type,
            "quantity": float(row.quantity),
            "limit_price": (
                float(row.limit_price) if row.limit_price is not None else None
            ),
            "market": row.market,
            "currency": row.currency,
            "agent_run_id": row.agent_run_id,
            "prompt_version": row.prompt_version,
            "confidence_score": (
                float(row.confidence_score)
                if row.confidence_score is not None else None
            ),
            "reasoning_text": row.reasoning_text,
            "model_used": row.model_used,
            "status": row.status,
            "risk_decision": row.risk_decision,
            "risk_flags": _safe_loads(row.risk_flags_json) or [],
            "fill_price": (
                float(row.fill_price) if row.fill_price is not None else None
            ),
            "fill_quantity": (
                float(row.fill_quantity) if row.fill_quantity is not None else None
            ),
            "fill_time": row.fill_time.isoformat() if row.fill_time else None,
            "intent_payload": _safe_loads(row.intent_payload_json) or {},
            "result_payload": _safe_loads(row.result_payload_json),
            "quote_payload": _safe_loads(row.quote_payload_json),
            "pnl_horizons": _safe_loads(row.pnl_horizons_json),
            "error_code": row.error_code,
            "error_message": row.error_message,
            "requested_at": (
                row.requested_at.isoformat() if row.requested_at else None
            ),
            "pnl_computed_at": (
                row.pnl_computed_at.isoformat()
                if row.pnl_computed_at else None
            ),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


# =====================================================================
# AITrainingLabelRepository
# =====================================================================

class AITrainingLabelRepository:
    """Persistence for ``ai_training_labels``.

    Supports upsert semantics: re-labeling a (source_kind, source_id)
    pair overwrites the previous label rather than failing — labelling
    is iterative and we want the latest verdict to win.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert_label(
        self,
        *,
        source_kind: str,
        source_id: int,
        label: LabelKind,
        outcome_text: Optional[str] = None,
        user_notes: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or replace the label for a given source row."""
        if source_kind not in ("analysis_history", "ai_sandbox"):
            raise ValueError(
                f"source_kind must be analysis_history|ai_sandbox, got {source_kind!r}"
            )
        with self.db.get_session() as session:
            existing = session.execute(
                select(AITrainingLabel).where(
                    and_(
                        AITrainingLabel.source_kind == source_kind,
                        AITrainingLabel.source_id == int(source_id),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.label = label.value
                existing.outcome_text = outcome_text
                existing.user_notes = user_notes
                existing.created_by = created_by
                existing.created_at = _utc_now()
                session.commit()
                session.refresh(existing)
                return self._row_to_dict(existing)
            row = AITrainingLabel(
                source_kind=source_kind,
                source_id=int(source_id),
                label=label.value,
                outcome_text=outcome_text,
                user_notes=user_notes,
                created_by=created_by,
                created_at=_utc_now(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._row_to_dict(row)

    def delete_label(self, *, source_kind: str, source_id: int) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(AITrainingLabel).where(
                    and_(
                        AITrainingLabel.source_kind == source_kind,
                        AITrainingLabel.source_id == int(source_id),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def get_label(
        self, *, source_kind: str, source_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(AITrainingLabel).where(
                    and_(
                        AITrainingLabel.source_kind == source_kind,
                        AITrainingLabel.source_id == int(source_id),
                    )
                )
            ).scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    def list_labels(
        self,
        *,
        source_kind: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(AITrainingLabel).order_by(
                desc(AITrainingLabel.created_at), desc(AITrainingLabel.id),
            )
            if source_kind:
                stmt = stmt.where(AITrainingLabel.source_kind == source_kind)
            if label:
                stmt = stmt.where(AITrainingLabel.label == label)
            stmt = stmt.limit(max(1, min(int(limit or 100), 1000)))
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_dict(row) for row in rows]

    def stats(self) -> Dict[str, int]:
        with self.db.get_session() as session:
            rows = session.execute(select(AITrainingLabel)).scalars().all()
        out: Dict[str, int] = {
            "total": len(rows),
            "correct": 0,
            "incorrect": 0,
            "unclear": 0,
            "from_analysis_history": 0,
            "from_ai_sandbox": 0,
        }
        for r in rows:
            if r.label in out:
                out[r.label] += 1
            if r.source_kind == "analysis_history":
                out["from_analysis_history"] += 1
            elif r.source_kind == "ai_sandbox":
                out["from_ai_sandbox"] += 1
        return out

    @staticmethod
    def _row_to_dict(row: AITrainingLabel) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "source_kind": row.source_kind,
            "source_id": int(row.source_id),
            "label": row.label,
            "outcome_text": row.outcome_text,
            "user_notes": row.user_notes,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
