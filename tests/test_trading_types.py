# -*- coding: utf-8 -*-
"""Tests for src/trading/types.py — frozen dataclasses + enums."""

from __future__ import annotations

import unittest

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
    TimeInForce,
)


class OrderRequestShapeTests(unittest.TestCase):
    def test_to_dict_round_trip_preserves_all_fields(self) -> None:
        req = OrderRequest(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=10.0,
            order_type=OrderType.LIMIT,
            limit_price=200.5,
            time_in_force=TimeInForce.GTC,
            account_id=42,
            market="us",
            currency="USD",
            note="hello",
            source="agent",
            agent_session_id="sess-1",
            request_uid="uid-abc-12345678",
        )
        d = req.to_dict()
        self.assertEqual(d["symbol"], "AAPL")
        self.assertEqual(d["side"], "buy")  # enum → str
        self.assertEqual(d["order_type"], "limit")
        self.assertEqual(d["time_in_force"], "gtc")
        self.assertEqual(d["quantity"], 10.0)
        self.assertEqual(d["limit_price"], 200.5)
        self.assertEqual(d["account_id"], 42)
        self.assertEqual(d["market"], "us")
        self.assertEqual(d["currency"], "USD")
        self.assertEqual(d["note"], "hello")
        self.assertEqual(d["source"], "agent")
        self.assertEqual(d["agent_session_id"], "sess-1")
        self.assertEqual(d["request_uid"], "uid-abc-12345678")
        # Dataclass is frozen — can't mutate.
        with self.assertRaises(AttributeError):
            req.symbol = "MSFT"  # type: ignore[misc]

    def test_order_request_is_frozen_immutable(self) -> None:
        req = OrderRequest(
            symbol="AAPL", side=OrderSide.BUY, quantity=1, request_uid="u",
        )
        with self.assertRaises(AttributeError):
            req.quantity = 2  # type: ignore[misc]


class RiskAssessmentSemanticsTests(unittest.TestCase):
    def test_block_severity_implies_block_decision_when_caller_aggregates(
        self,
    ) -> None:
        """We don't auto-derive ``decision`` from flags inside the
        dataclass (that's the engine's job), but the convention is:
        any BLOCK-severity flag → decision='block'. Sanity-check
        the to_dict round-trip."""
        a = RiskAssessment(
            flags=[
                RiskFlag(
                    code=RiskFlagCode.OVERSELL,
                    severity=RiskSeverity.BLOCK,
                    message="boom",
                ),
            ],
            decision="block",
            evaluated_at="2026-05-04T00:00:00+00:00",
            config_snapshot={"trading_mode": "paper"},
        )
        d = a.to_dict()
        self.assertEqual(d["decision"], "block")
        self.assertEqual(d["flags"][0]["code"], "oversell")
        self.assertEqual(d["flags"][0]["severity"], "block")
        self.assertEqual(d["config_snapshot"]["trading_mode"], "paper")


class OrderResultShapeTests(unittest.TestCase):
    def test_full_result_round_trip(self) -> None:
        req = OrderRequest(
            symbol="AAPL", side=OrderSide.BUY, quantity=1, request_uid="u",
        )
        result = OrderResult(
            request=req,
            status=ExecutionStatus.FILLED,
            mode=ExecutionMode.PAPER,
            fill_price=199.99,
            fill_quantity=1.0,
            fill_time="2026-05-04T00:00:00+00:00",
            realised_fee=0.0,
            realised_tax=0.0,
            portfolio_trade_id=7,
            quote_payload={"source": "firstrade", "last": 199.99},
        )
        d = result.to_dict()
        self.assertEqual(d["status"], "filled")
        self.assertEqual(d["mode"], "paper")
        self.assertEqual(d["fill_price"], 199.99)
        self.assertEqual(d["portfolio_trade_id"], 7)
        self.assertEqual(d["request"]["symbol"], "AAPL")
        self.assertEqual(d["quote_payload"]["last"], 199.99)


if __name__ == "__main__":
    unittest.main()
