# -*- coding: utf-8 -*-
"""TradeExecutionRepository — 4 cases on the audit table."""

from __future__ import annotations

import unittest

from src.storage import DatabaseManager
from src.trading.audit_repo import (
    DuplicateRequestUidError,
    TradeExecutionRepository,
)
from src.trading.types import (
    ExecutionMode,
    ExecutionStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    RiskAssessment,
    RiskFlag,
    RiskFlagCode,
    RiskSeverity,
)


def _make_db() -> DatabaseManager:
    DatabaseManager.reset_instance()
    return DatabaseManager(db_url="sqlite:///:memory:")


def _req(uid="uid-aapl-1", **overrides):
    base = dict(
        symbol="AAPL", side=OrderSide.BUY, quantity=1.0,
        order_type=OrderType.MARKET,
        request_uid=uid, market="us", account_id=1,
    )
    base.update(overrides)
    return OrderRequest(**base)


class AuditRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _make_db()
        self.repo = TradeExecutionRepository(db_manager=self.db)

    def test_start_then_finish_writes_pending_then_filled(self) -> None:
        req = _req()
        rid = self.repo.start_execution(req, mode="paper")
        self.assertGreater(rid, 0)
        # Pending row visible
        rows = self.repo.list_recent_executions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        # Finalise with a FILLED result
        result = OrderResult(
            request=req,
            status=ExecutionStatus.FILLED,
            mode=ExecutionMode.PAPER,
            fill_price=200.0, fill_quantity=1.0,
            portfolio_trade_id=99,
        )
        self.repo.finish_execution(req.request_uid, result)
        row = self.repo.get_by_request_uid(req.request_uid)
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["fill_price"], 200.0)
        self.assertEqual(row["portfolio_trade_id"], 99)

    def test_request_uid_unique_constraint_rejects_duplicate(self) -> None:
        req = _req(uid="dup-uid-1")
        self.repo.start_execution(req, mode="paper")
        with self.assertRaises(DuplicateRequestUidError):
            self.repo.start_execution(req, mode="paper")

    def test_recent_executions_filters_by_mode_and_orders_by_requested_at_desc(self) -> None:
        # Three rows, two paper + one live (live just to test mode filter)
        for i, mode in enumerate(["paper", "paper", "live"]):
            req = _req(uid=f"uid-batch-{i}", symbol=f"SYM{i}")
            self.repo.start_execution(req, mode=mode)
        paper_rows = self.repo.list_recent_executions(mode="paper")
        self.assertEqual(len(paper_rows), 2)
        for r in paper_rows:
            self.assertEqual(r["mode"], "paper")
        # All rows
        all_rows = self.repo.list_recent_executions()
        self.assertEqual(len(all_rows), 3)
        # Newest first (the last insert is uid-batch-2 which was live;
        # second-newest is uid-batch-1)
        self.assertEqual(all_rows[0]["request_uid"], "uid-batch-2")

    def test_daily_turnover_excludes_blocked_and_failed(self) -> None:
        # FILLED 5*100 = 500 → counts. Use quantity=5 in the request
        # because daily_turnover reads row.quantity (the requested
        # qty captured at start_execution), not fill_quantity.
        req1 = _req(uid="t1", symbol="X", quantity=5)
        self.repo.start_execution(req1, mode="paper")
        self.repo.finish_execution(req1.request_uid, OrderResult(
            request=req1, status=ExecutionStatus.FILLED,
            mode=ExecutionMode.PAPER, fill_price=100, fill_quantity=5,
        ))
        # BLOCKED 5*100 = 500 → excluded
        req2 = _req(uid="t2", symbol="X")
        self.repo.start_execution(req2, mode="paper")
        ra = RiskAssessment(
            flags=[RiskFlag(
                code=RiskFlagCode.OVERSELL,
                severity=RiskSeverity.BLOCK,
                message="boom",
            )],
            decision="block",
            evaluated_at="2026-05-04T00:00:00+00:00",
            config_snapshot={},
        )
        self.repo.finish_execution(req2.request_uid, OrderResult(
            request=req2, status=ExecutionStatus.BLOCKED,
            mode=ExecutionMode.PAPER, risk_assessment=ra,
        ))
        # FAILED 1*999 = 999 → excluded
        req3 = _req(uid="t3", symbol="X", quantity=1)
        self.repo.start_execution(req3, mode="paper")
        self.repo.finish_execution(req3.request_uid, OrderResult(
            request=req3, status=ExecutionStatus.FAILED,
            mode=ExecutionMode.PAPER, error_code="QUOTE_UNAVAILABLE",
        ))
        # Only FILLED counts → 500 (1 * 5 * 100)
        # Note: FILLED + PENDING count; here req1 has FILLED with fill_price=100, qty=5.
        total = self.repo.daily_turnover(mode="paper")
        self.assertEqual(total, 500.0)


if __name__ == "__main__":
    unittest.main()
