# -*- coding: utf-8 -*-
"""Read/write helper for the ``broker_sync_runs`` and ``broker_snapshots``
tables.

The repo is the **last redaction layer before disk** — every payload
is fed through :func:`brokers.base.redact_sensitive_payload` again
before it's serialized to ``payload_json``, even though the upstream
client / sync service already redacted on the way in. The defence in
depth is cheap and makes a future bug in any other layer survivable.

Read paths return plain ``dict``s shaped exactly like the agent tool
needs (camelCase / snake_case mirroring the dataclass field names).
The agent tool **never** reads ``raw_payload`` from the DB rows it
fetches; its serializer drops that field. The API endpoints do the
same.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, desc, select

from src.brokers.base import redact_sensitive_payload
from src.storage import BrokerSnapshot, BrokerSyncRun, DatabaseManager

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_dumps(payload: Any) -> str:
    """Serialize ``payload`` to JSON, with a final defensive redaction
    pass and a fallback for non-JSON-serializable values."""
    redacted = redact_sensitive_payload(payload)
    try:
        return json.dumps(redacted, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("[broker_snapshot_repo] payload not JSON-serializable: %s", exc)
        return json.dumps({"_serialization_error": str(exc)})


def _safe_loads(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


class BrokerSnapshotRepository:
    """Persistence for broker snapshots + sync runs.

    Constructor takes an optional ``DatabaseManager`` so tests can
    inject an in-memory SQLite. In production callers pass nothing
    and the singleton is reused.
    """

    DEFAULT_BROKER = "firstrade"

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Sync run lifecycle
    # ------------------------------------------------------------------

    def save_sync_run_start(
        self, *, broker: str = DEFAULT_BROKER, message: Optional[str] = None,
    ) -> int:
        """Insert a ``status='running'`` row and return its id."""
        with self.db.get_session() as session:
            row = BrokerSyncRun(
                broker=broker,
                status="running",
                message=message,
                started_at=_now_utc(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        account_count: int = 0,
        position_count: int = 0,
        order_count: int = 0,
        transaction_count: int = 0,
        message: Optional[str] = None,
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the run row with the final result. ``error_payload``,
        if provided, is redacted before serialization."""
        with self.db.get_session() as session:
            row = session.get(BrokerSyncRun, run_id)
            if row is None:
                logger.warning("[broker_snapshot_repo] missing sync run %s", run_id)
                return
            row.status = status
            row.message = message
            row.account_count = int(account_count)
            row.position_count = int(position_count)
            row.order_count = int(order_count)
            row.transaction_count = int(transaction_count)
            row.finished_at = _now_utc()
            row.error_json = _safe_dumps(error_payload) if error_payload else None
            session.commit()

    def get_last_sync_run(self, broker: str = DEFAULT_BROKER) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = (
                select(BrokerSyncRun)
                .where(BrokerSyncRun.broker == broker)
                .order_by(desc(BrokerSyncRun.started_at))
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                return None
            return self._sync_run_to_dict(row)

    @staticmethod
    def _sync_run_to_dict(row: BrokerSyncRun) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "broker": row.broker,
            "status": row.status,
            "message": row.message,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "account_count": int(row.account_count or 0),
            "position_count": int(row.position_count or 0),
            "order_count": int(row.order_count or 0),
            "transaction_count": int(row.transaction_count or 0),
            "error": _safe_loads(row.error_json) if row.error_json else None,
        }

    # ------------------------------------------------------------------
    # Snapshot writes
    # ------------------------------------------------------------------
    #
    # Each save_* helper:
    #   * coerces the input dataclass list into row dicts
    #   * runs the redaction pass on payload_json
    #   * inserts in one session
    #
    # Callers should funnel through save_full_snapshot for a "full
    # refresh"; the granular helpers exist mainly for tests + future
    # incremental syncs.

    def save_accounts(
        self, accounts: Iterable[Any], *, broker: str = DEFAULT_BROKER,
    ) -> int:
        return self._insert_rows("account", accounts, broker)

    def save_balances(
        self, balances: Iterable[Any], *, broker: str = DEFAULT_BROKER,
    ) -> int:
        return self._insert_rows("balance", balances, broker)

    def save_positions(
        self, positions: Iterable[Any], *, broker: str = DEFAULT_BROKER,
    ) -> int:
        return self._insert_rows("position", positions, broker)

    def save_orders(
        self, orders: Iterable[Any], *, broker: str = DEFAULT_BROKER,
    ) -> int:
        return self._insert_rows("order", orders, broker)

    def save_transactions(
        self, transactions: Iterable[Any], *, broker: str = DEFAULT_BROKER,
    ) -> int:
        return self._insert_rows("transaction", transactions, broker)

    def save_full_snapshot(
        self, snapshot: Any, *, broker: Optional[str] = None,
    ) -> Dict[str, int]:
        """Persist every leg of a :class:`brokers.base.BrokerSnapshot`."""
        target_broker = (
            broker or getattr(snapshot, "broker", None) or self.DEFAULT_BROKER
        )
        with self.db.get_session() as session:
            try:
                counts = {
                    "accounts": self._insert_rows_in_session(
                        session,
                        "account",
                        getattr(snapshot, "accounts", []) or [],
                        target_broker,
                    ),
                    "balances": self._insert_rows_in_session(
                        session,
                        "balance",
                        getattr(snapshot, "balances", []) or [],
                        target_broker,
                    ),
                    "positions": self._insert_rows_in_session(
                        session,
                        "position",
                        getattr(snapshot, "positions", []) or [],
                        target_broker,
                    ),
                    "orders": self._insert_rows_in_session(
                        session,
                        "order",
                        getattr(snapshot, "orders", []) or [],
                        target_broker,
                    ),
                    "transactions": self._insert_rows_in_session(
                        session,
                        "transaction",
                        getattr(snapshot, "transactions", []) or [],
                        target_broker,
                    ),
                }
                session.commit()
                return counts
            except Exception:
                session.rollback()
                raise

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------

    def get_latest_accounts(
        self, broker: str = DEFAULT_BROKER,
    ) -> List[Dict[str, Any]]:
        return self._latest_per_account_hash("account", broker=broker)

    def get_latest_balances(
        self, broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._latest_per_account_hash(
            "balance", broker=broker, account_hash=account_hash,
        )

    def get_latest_positions(
        self, broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._latest_within_window(
            "position", broker=broker, account_hash=account_hash,
        )

    def get_latest_orders(
        self, broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._latest_within_window(
            "order", broker=broker, account_hash=account_hash,
        )

    def get_latest_transactions(
        self,
        broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return self._latest_within_window(
            "transaction",
            broker=broker,
            account_hash=account_hash,
            limit=max(1, min(int(limit or 50), 500)),
        )

    def get_latest_snapshot(
        self, broker: str = DEFAULT_BROKER,
    ) -> Dict[str, Any]:
        """One-shot helper that returns the freshest as_of plus the
        snapshot leaves the agent tool needs."""
        latest_run = self.get_last_sync_run(broker)
        accounts = self.get_latest_accounts(broker)
        positions = self.get_latest_positions(broker)
        orders = self.get_latest_orders(broker)
        balances = self.get_latest_balances(broker)
        transactions = self.get_latest_transactions(broker)
        as_of = self._max_as_of([accounts, positions, orders, balances, transactions])
        return {
            "broker": broker,
            "as_of": as_of,
            "last_sync": latest_run,
            "accounts": accounts,
            "balances": balances,
            "positions": positions,
            "orders": orders,
            "transactions": transactions,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert_rows(
        self,
        snapshot_type: str,
        rows: Iterable[Any],
        broker: str,
    ) -> int:
        with self.db.get_session() as session:
            try:
                count = self._insert_rows_in_session(session, snapshot_type, rows, broker)
                session.commit()
                return count
            except Exception:
                session.rollback()
                raise

    def _insert_rows_in_session(
        self,
        session: Any,
        snapshot_type: str,
        rows: Iterable[Any],
        broker: str,
    ) -> int:
        materialized = list(rows or [])
        if not materialized:
            return 0
        for item in materialized:
            payload = self._dataclass_to_dict(item)
            # Final defensive redaction pass right before INSERT.
            redacted_payload = redact_sensitive_payload(payload)
            payload_json = json.dumps(
                redacted_payload, ensure_ascii=False, default=str,
            )
            session.add(
                BrokerSnapshot(
                    broker=broker,
                    snapshot_type=snapshot_type,
                    account_hash=str(redacted_payload.get("account_hash") or ""),
                    account_last4=str(redacted_payload.get("account_last4") or "") or None,
                    account_alias=str(redacted_payload.get("account_alias") or "") or None,
                    entity_hash=str(
                        redacted_payload.get("order_id_hash")
                        or redacted_payload.get("transaction_id_hash")
                        or ""
                    ) or None,
                    symbol=str(redacted_payload.get("symbol") or "") or None,
                    payload_json=payload_json,
                    as_of=self._coerce_dt(redacted_payload.get("as_of")) or _now_utc(),
                )
            )
        return len(materialized)

    @staticmethod
    def _dataclass_to_dict(item: Any) -> Dict[str, Any]:
        if hasattr(item, "__dataclass_fields__"):
            from dataclasses import asdict
            return asdict(item)
        if isinstance(item, dict):
            return dict(item)
        return {"value": str(item)}

    @staticmethod
    def _coerce_dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:  # pragma: no cover — defensive
            return None

    def _latest_per_account_hash(
        self,
        snapshot_type: str,
        *,
        broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return at most one row per ``account_hash`` (the freshest)
        from the **most recent sync only** (5-minute fence).

        Without the time-window filter, leftover rows from earlier
        buggy syncs (e.g. the "5 sub-accounts" regression we hit while
        debugging the all_accounts shape) would surface as live
        accounts and inflate the count. The fence anchors on the
        freshest ``as_of`` so a fresh sync's rows always win, but
        anything older than 5 minutes from that timestamp is excluded.
        """
        with self.db.get_session() as session:
            stmt = (
                select(BrokerSnapshot)
                .where(
                    and_(
                        BrokerSnapshot.broker == broker,
                        BrokerSnapshot.snapshot_type == snapshot_type,
                    )
                )
                .order_by(desc(BrokerSnapshot.as_of), desc(BrokerSnapshot.id))
            )
            if account_hash:
                stmt = stmt.where(BrokerSnapshot.account_hash == account_hash)
            rows = session.execute(stmt).scalars().all()
        if not rows:
            return []
        latest_ts = rows[0].as_of
        cutoff = latest_ts - _five_minutes()
        seen: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.as_of < cutoff:
                break
            key = row.account_hash or ""
            if key in seen:
                continue
            seen[key] = self._row_to_dict(row)
        return list(seen.values())

    def _latest_within_window(
        self,
        snapshot_type: str,
        *,
        broker: str = DEFAULT_BROKER,
        account_hash: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return the rows from the most-recent sync only, with
        per-entity deduplication so multiple consecutive syncs don't
        produce duplicate position / order / transaction rows in the
        snapshot view.

        De-dup key by ``snapshot_type``:
          * ``position``     → ``(account_hash, symbol)``
          * ``order``        → ``(account_hash, entity_hash)``  (order id)
          * ``transaction``  → ``(account_hash, entity_hash)``  (tx id)
          * ``balance``      → ``(account_hash,)``  (one balance per account)
          * fallback         → ``(account_hash, symbol or entity_hash)``

        Rows are pre-sorted ``as_of DESC, id DESC`` so the first
        occurrence of each key is the freshest one — subsequent
        occurrences (older syncs of the same entity) are dropped.

        We can't trust a fixed time window because syncs are manually
        triggered, but each sync writes ALL rows with very close
        ``as_of`` timestamps — so we pick the freshest ``as_of`` and
        return all rows of that type whose ``as_of`` falls within a
        5-minute fence (then dedupe).
        """
        with self.db.get_session() as session:
            stmt = (
                select(BrokerSnapshot)
                .where(
                    and_(
                        BrokerSnapshot.broker == broker,
                        BrokerSnapshot.snapshot_type == snapshot_type,
                    )
                )
                .order_by(desc(BrokerSnapshot.as_of), desc(BrokerSnapshot.id))
            )
            if account_hash:
                stmt = stmt.where(BrokerSnapshot.account_hash == account_hash)
            rows = session.execute(stmt).scalars().all()
        if not rows:
            return []
        latest_ts = rows[0].as_of
        # 5-minute fence — generous enough for one full sync_now() to
        # span without picking up a previous run's stragglers.
        cutoff = latest_ts - _five_minutes()

        def _dedup_key(row) -> tuple:
            ah = row.account_hash or ""
            if snapshot_type == "balance":
                return (ah,)
            if snapshot_type == "position":
                return (ah, (row.symbol or "").upper())
            if snapshot_type in ("order", "transaction"):
                return (ah, row.entity_hash or "")
            # Fallback: best-effort composite key.
            return (ah, (row.symbol or row.entity_hash or "").upper())

        seen_keys: set = set()
        out: List[Dict[str, Any]] = []
        for row in rows:
            if row.as_of < cutoff:
                break
            key = _dedup_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(self._row_to_dict(row))
            if limit and len(out) >= limit:
                break
        return out

    @staticmethod
    def _row_to_dict(row: BrokerSnapshot) -> Dict[str, Any]:
        payload = _safe_loads(row.payload_json)
        # Defensive redaction — the row was already redacted on insert,
        # but a future bug at any other layer is survivable as long as
        # this function never returns sensitive keys.
        payload = redact_sensitive_payload(payload)
        return {
            "id": int(row.id),
            "broker": row.broker,
            "snapshot_type": row.snapshot_type,
            "account_hash": row.account_hash or "",
            "account_last4": row.account_last4 or "",
            "account_alias": row.account_alias or "",
            "entity_hash": row.entity_hash or None,
            "symbol": row.symbol or None,
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "payload": payload,
        }

    @staticmethod
    def _max_as_of(buckets: List[List[Dict[str, Any]]]) -> Optional[str]:
        candidates: List[str] = []
        for bucket in buckets:
            for row in bucket:
                v = row.get("as_of")
                if v:
                    candidates.append(v)
        return max(candidates) if candidates else None


def _five_minutes():
    from datetime import timedelta
    return timedelta(minutes=5)
