# -*- coding: utf-8 -*-
"""TradingExecutionService — orchestration tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.storage import DatabaseManager
from src.trading.audit_repo import TradeExecutionRepository
from src.trading.types import (
    OrderRequest,
    OrderSide,
    OrderType,
)


def _cfg(mode="paper", **overrides):
    base = dict(
        trading_mode=mode,
        trading_paper_slippage_bps=5,
        trading_paper_fee_per_trade=0.0,
        trading_max_position_value=10000.0,
        trading_max_position_pct=0.10,
        trading_max_daily_turnover=50000.0,
        trading_symbol_allowlist=[],
        trading_symbol_denylist=[],
        trading_market_hours_strict=False,  # don't depend on clock
        trading_notification_enabled=False,
        trading_paper_account_id=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _req(uid="svc-test-1", **overrides):
    base = dict(
        symbol="AAPL", side=OrderSide.BUY, quantity=1,
        order_type=OrderType.MARKET, request_uid=uid,
        market="us", account_id=1,
    )
    base.update(overrides)
    return OrderRequest(**base)


def _make_repo():
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    return TradeExecutionRepository(db_manager=db)


class TradingServiceTests(unittest.TestCase):
    def test_disabled_mode_raises(self) -> None:
        from src.services.trading_service import (
            TradingDisabledError,
            TradingExecutionService,
        )
        svc = TradingExecutionService(
            config=_cfg(mode="disabled"),
            audit_repo=_make_repo(),
        )
        with self.assertRaises(TradingDisabledError):
            svc.submit(_req())
        self.assertEqual(svc.get_status()["status"], "disabled")

    def test_live_mode_returns_failed_with_not_implemented(self) -> None:
        from src.services.trading_service import TradingExecutionService
        svc = TradingExecutionService(
            config=_cfg(mode="live"), audit_repo=_make_repo(),
        )
        # Live executor raises NotImplementedError at construction;
        # service catches and emits FAILED + LIVE_NOT_IMPLEMENTED.
        with patch("src.services.trading_service."
                   "TradingExecutionService._fetch_portfolio_snapshot",
                   return_value=None), \
             patch("src.services.trading_service."
                   "TradingExecutionService._fetch_broker_status",
                   return_value=None), \
             patch("src.services.trading_service."
                   "TradingExecutionService._estimate_price",
                   return_value=200.0):
            out = svc.submit(_req(uid="live-test"))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error_code"], "LIVE_NOT_IMPLEMENTED")

    def test_blocked_request_writes_audit_skips_executor(self) -> None:
        from src.services.trading_service import TradingExecutionService
        # Force a block via denylist
        svc = TradingExecutionService(
            config=_cfg(trading_symbol_denylist=["AAPL"]),
            audit_repo=_make_repo(),
        )
        with patch("src.services.trading_service."
                   "TradingExecutionService._fetch_portfolio_snapshot",
                   return_value=None), \
             patch("src.services.trading_service."
                   "TradingExecutionService._fetch_broker_status",
                   return_value=None), \
             patch("src.services.trading_service."
                   "TradingExecutionService._estimate_price",
                   return_value=200.0):
            out = svc.submit(_req(uid="blk-test"))
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["error_code"], "RISK_BLOCKED")

    def test_status_payload_shape_for_paper_mode(self) -> None:
        from src.services.trading_service import TradingExecutionService
        svc = TradingExecutionService(
            config=_cfg(), audit_repo=_make_repo(),
        )
        status = svc.get_status()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["mode"], "paper")
        self.assertEqual(status["paper_account_id"], 1)
        self.assertIn("max_position_value", status)
        self.assertIn("symbol_allowlist", status)


if __name__ == "__main__":
    unittest.main()
