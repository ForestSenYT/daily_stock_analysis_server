# -*- coding: utf-8 -*-
"""AI sandbox + training-labels — comprehensive test suite."""

from __future__ import annotations

import os
import unittest
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

from src.ai_sandbox.repo import (
    AISandboxRepository,
    AITrainingLabelRepository,
    DuplicateSandboxRequestError,
)
from src.ai_sandbox.types import (
    AISandboxIntent,
    AISandboxResult,
    LabelKind,
    PnlHorizons,
)
from src.storage import DatabaseManager
from src.trading.types import (
    ExecutionStatus,
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


def _intent(uid="ai-test-1", **overrides) -> AISandboxIntent:
    base = dict(
        symbol="AAPL", side=OrderSide.BUY, quantity=1.0,
        order_type=OrderType.MARKET, request_uid=uid,
        market="us",
        agent_run_id="run-A",
        prompt_version="v1",
        confidence_score=0.8,
        reasoning_text="momentum + low RSI",
        model_used="openai/gpt-4o",
    )
    base.update(overrides)
    return AISandboxIntent(**base)


# =====================================================================
# 1. Type round-trips
# =====================================================================

class TypesTests(unittest.TestCase):
    def test_intent_to_order_request_marks_source(self) -> None:
        intent = _intent()
        req = intent.to_order_request()
        self.assertEqual(req.source, "agent_sandbox")
        self.assertEqual(req.symbol, "AAPL")
        self.assertEqual(req.account_id, None)

    def test_intent_to_dict_roundtrip(self) -> None:
        intent = _intent(uid="r-1", confidence_score=0.91)
        d = intent.to_dict()
        self.assertEqual(d["side"], "buy")
        self.assertEqual(d["order_type"], "market")
        self.assertEqual(d["confidence_score"], 0.91)
        self.assertEqual(d["model_used"], "openai/gpt-4o")

    def test_pnl_horizons_to_dict(self) -> None:
        h = PnlHorizons(
            horizon_1d=1.5, horizon_7d=-0.5, computed_at="2026-05-04T00:00:00",
        )
        d = h.to_dict()
        self.assertEqual(d["horizon_1d"], 1.5)
        self.assertEqual(d["horizon_7d"], -0.5)


# =====================================================================
# 2. Sandbox repository
# =====================================================================

class SandboxRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _make_db()
        self.repo = AISandboxRepository(db_manager=self.db)

    def test_start_then_finish_writes_pending_then_filled(self) -> None:
        intent = _intent(uid="rep-1")
        rid = self.repo.start_execution(intent)
        self.assertGreater(rid, 0)
        rows = self.repo.list_executions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        result = AISandboxResult(
            intent=intent, status=ExecutionStatus.FILLED,
            fill_price=200.0, fill_quantity=1.0,
            fill_time="2026-05-04T13:00:00+00:00",
        )
        self.repo.finish_execution(intent.request_uid, result)
        rows = self.repo.list_executions()
        self.assertEqual(rows[0]["status"], "filled")
        self.assertEqual(rows[0]["fill_price"], 200.0)

    def test_duplicate_request_uid_raises(self) -> None:
        intent = _intent(uid="dup-1")
        self.repo.start_execution(intent)
        with self.assertRaises(DuplicateSandboxRequestError):
            self.repo.start_execution(intent)

    def test_list_filters_by_run_and_symbol(self) -> None:
        for i, sym in enumerate(["AAPL", "MSFT", "AAPL"]):
            self.repo.start_execution(_intent(
                uid=f"flt-{i}", symbol=sym, agent_run_id=f"r-{i//2}",
            ))
        aapl = self.repo.list_executions(symbol="AAPL")
        self.assertEqual(len(aapl), 2)
        run_zero = self.repo.list_executions(agent_run_id="r-0")
        self.assertEqual(len(run_zero), 2)

    def test_aggregate_metrics_winrate_and_avg(self) -> None:
        # Two filled BUY rows, one with +2% (win), one with -1% (loss)
        for i, (uid, h1, side) in enumerate([
            ("m-1", 2.0, OrderSide.BUY),
            ("m-2", -1.0, OrderSide.BUY),
        ]):
            intent = _intent(uid=uid, side=side)
            self.repo.start_execution(intent)
            result = AISandboxResult(
                intent=intent, status=ExecutionStatus.FILLED,
                fill_price=100.0, fill_quantity=1.0,
                fill_time="2026-05-04T00:00:00+00:00",
            )
            self.repo.finish_execution(intent.request_uid, result)
            self.repo.update_pnl_horizons(intent.request_uid, PnlHorizons(
                horizon_1d=h1, computed_at="2026-05-05T00:00:00+00:00",
            ))
        m = self.repo.aggregate_metrics()
        self.assertEqual(m["filled_count"], 2)
        self.assertEqual(m["with_pnl_count"], 2)
        # 1 of 2 wins on 1d → 0.5
        self.assertEqual(m["win_rate_1d"], 0.5)
        # avg of (+2, -1) = +0.5
        self.assertAlmostEqual(m["avg_pnl_1d_pct"], 0.5, places=4)


# =====================================================================
# 3. Sandbox service
# =====================================================================

def _sandbox_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        ai_sandbox_enabled=True,
        ai_sandbox_max_position_value=10000.0,
        ai_sandbox_max_position_pct=0.50,
        ai_sandbox_max_daily_turnover=100000.0,
        ai_sandbox_symbol_allowlist=[],
        ai_sandbox_symbol_denylist=[],
        ai_sandbox_market_hours_strict=False,
        ai_sandbox_paper_slippage_bps=10,
        ai_sandbox_paper_fee_per_trade=0.0,
        ai_sandbox_daemon_enabled=False,
        ai_sandbox_daemon_interval_minutes=60,
        ai_sandbox_daemon_watchlist=[],
        ai_sandbox_default_prompt_version="v1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class SandboxServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _make_db()
        self.repo = AISandboxRepository(db_manager=self.db)

    def test_disabled_raises(self) -> None:
        from src.services.ai_sandbox_service import (
            AISandboxService, SandboxDisabledError,
        )
        svc = AISandboxService(
            config=_sandbox_cfg(ai_sandbox_enabled=False),
            repo=self.repo,
        )
        with self.assertRaises(SandboxDisabledError):
            svc.submit(_intent())

    def test_status_payload_when_enabled(self) -> None:
        from src.services.ai_sandbox_service import AISandboxService
        svc = AISandboxService(config=_sandbox_cfg(), repo=self.repo)
        s = svc.get_status()
        self.assertEqual(s["status"], "ready")
        self.assertIn("max_position_value", s)

    def test_blocked_by_denylist(self) -> None:
        from src.services.ai_sandbox_service import AISandboxService
        svc = AISandboxService(
            config=_sandbox_cfg(ai_sandbox_symbol_denylist=["AAPL"]),
            repo=self.repo,
        )
        with patch.object(
            AISandboxService, "_estimate_price", return_value=200.0,
        ):
            out = svc.submit(_intent(uid="den-1"))
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["error_code"], "RISK_BLOCKED")

    def test_filled_with_mocked_quote(self) -> None:
        """Patch the executor's quote resolver to return a deterministic
        ask/bid; expect status=filled and fill_price ≈ ask*(1+slip)."""
        from src.services.ai_sandbox_service import AISandboxService
        svc = AISandboxService(config=_sandbox_cfg(), repo=self.repo)
        fake_quote = {
            "source": "firstrade", "symbol": "AAPL",
            "bid": 199.0, "ask": 200.0, "last": 199.5,
        }
        with patch(
            "src.trading.executors.paper.PaperExecutor._resolve_quote",
            return_value=fake_quote,
        ), patch.object(
            AISandboxService, "_fetch_broker_status", return_value=None,
        ), patch.object(
            AISandboxService, "_sandbox_daily_turnover", return_value=0.0,
        ):
            out = svc.submit(_intent(uid="fill-1", quantity=2))
        self.assertEqual(out["status"], "filled")
        # 200 * (1 + 10/10000) = 200.2
        self.assertAlmostEqual(out["fill_price"], 200.2, places=3)
        self.assertEqual(out["fill_quantity"], 2.0)

    def test_failed_when_quote_unavailable(self) -> None:
        from src.services.ai_sandbox_service import AISandboxService
        svc = AISandboxService(config=_sandbox_cfg(), repo=self.repo)
        with patch(
            "src.trading.executors.paper.PaperExecutor._resolve_quote",
            return_value=None,
        ), patch.object(
            AISandboxService, "_fetch_broker_status", return_value=None,
        ), patch.object(
            AISandboxService, "_sandbox_daily_turnover", return_value=0.0,
        ), patch.object(
            AISandboxService, "_estimate_price", return_value=None,
        ):
            out = svc.submit(_intent(uid="qun-1"))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error_code"], "QUOTE_UNAVAILABLE")

    def test_does_not_write_portfolio_trades(self) -> None:
        """A successful sandbox fill MUST NOT write to portfolio_trades.
        Hard isolation invariant."""
        from src.services.ai_sandbox_service import AISandboxService
        from src.storage import PortfolioTrade
        from sqlalchemy import select

        svc = AISandboxService(config=_sandbox_cfg(), repo=self.repo)
        with patch(
            "src.trading.executors.paper.PaperExecutor._resolve_quote",
            return_value={"bid": 100, "ask": 101, "last": 100.5,
                           "source": "firstrade", "symbol": "AAPL"},
        ), patch.object(
            AISandboxService, "_fetch_broker_status", return_value=None,
        ), patch.object(
            AISandboxService, "_sandbox_daily_turnover", return_value=0.0,
        ):
            svc.submit(_intent(uid="iso-1"))
        with self.db.get_session() as session:
            count = session.execute(select(PortfolioTrade)).scalars().all()
        self.assertEqual(
            len(count), 0,
            "Sandbox fills MUST NOT touch portfolio_trades",
        )


# =====================================================================
# 4. Training labels
# =====================================================================

class LabelsRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _make_db()
        self.repo = AITrainingLabelRepository(db_manager=self.db)

    def test_upsert_creates_then_overwrites(self) -> None:
        first = self.repo.upsert_label(
            source_kind="analysis_history", source_id=42,
            label=LabelKind.CORRECT, outcome_text="rallied 5% in 3 days",
        )
        self.assertEqual(first["label"], "correct")
        # Re-label same row → overwrites
        second = self.repo.upsert_label(
            source_kind="analysis_history", source_id=42,
            label=LabelKind.INCORRECT, outcome_text="actually fell",
        )
        self.assertEqual(second["label"], "incorrect")
        self.assertEqual(second["outcome_text"], "actually fell")
        # Same row id (UPDATE not INSERT)
        self.assertEqual(first["id"], second["id"])

    def test_invalid_source_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.upsert_label(
                source_kind="garbage", source_id=1,
                label=LabelKind.UNCLEAR,
            )

    def test_list_filters_by_kind_and_label(self) -> None:
        self.repo.upsert_label(
            source_kind="ai_sandbox", source_id=1, label=LabelKind.CORRECT,
        )
        self.repo.upsert_label(
            source_kind="ai_sandbox", source_id=2, label=LabelKind.INCORRECT,
        )
        self.repo.upsert_label(
            source_kind="analysis_history", source_id=10, label=LabelKind.CORRECT,
        )
        sandbox_only = self.repo.list_labels(source_kind="ai_sandbox")
        self.assertEqual(len(sandbox_only), 2)
        correct_only = self.repo.list_labels(label="correct")
        self.assertEqual(len(correct_only), 2)

    def test_stats_aggregation(self) -> None:
        self.repo.upsert_label(
            source_kind="ai_sandbox", source_id=1, label=LabelKind.CORRECT,
        )
        self.repo.upsert_label(
            source_kind="ai_sandbox", source_id=2, label=LabelKind.INCORRECT,
        )
        self.repo.upsert_label(
            source_kind="analysis_history", source_id=3, label=LabelKind.UNCLEAR,
        )
        stats = self.repo.stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["correct"], 1)
        self.assertEqual(stats["incorrect"], 1)
        self.assertEqual(stats["unclear"], 1)
        self.assertEqual(stats["from_ai_sandbox"], 2)
        self.assertEqual(stats["from_analysis_history"], 1)

    def test_delete_removes_row(self) -> None:
        self.repo.upsert_label(
            source_kind="ai_sandbox", source_id=99, label=LabelKind.CORRECT,
        )
        self.assertTrue(self.repo.delete_label(
            source_kind="ai_sandbox", source_id=99,
        ))
        self.assertFalse(self.repo.delete_label(
            source_kind="ai_sandbox", source_id=99,
        ))
        self.assertIsNone(self.repo.get_label(
            source_kind="ai_sandbox", source_id=99,
        ))


if __name__ == "__main__":
    unittest.main()
