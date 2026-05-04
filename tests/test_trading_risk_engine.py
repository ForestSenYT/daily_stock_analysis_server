# -*- coding: utf-8 -*-
"""Risk engine — 14 hard tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from src.trading.risk_engine import RiskEngine
from src.trading.types import (
    OrderRequest,
    OrderSide,
    OrderType,
    RiskFlagCode,
    RiskSeverity,
)


def _cfg(**overrides):
    base = dict(
        trading_mode="paper",
        trading_max_position_value=10000.0,
        trading_max_position_pct=0.10,
        trading_max_daily_turnover=50000.0,
        trading_symbol_allowlist=[],
        trading_symbol_denylist=[],
        trading_market_hours_strict=True,
        trading_paper_slippage_bps=5,
        trading_paper_fee_per_trade=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _req(**overrides):
    base = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
        request_uid="u-test-12345",
        market="us",
    )
    base.update(overrides)
    return OrderRequest(**base)


# Within US market open window (use Tuesday 17:00 UTC).
_OPEN_TUESDAY = datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc)
# Saturday — every market closed.
_CLOSED_SAT = datetime(2026, 5, 9, 17, 0, tzinfo=timezone.utc)


class RiskEngineTests(unittest.TestCase):
    def test_accepts_well_formed_buy_when_all_thresholds_pass(self) -> None:
        engine = RiskEngine(_cfg())
        out = engine.evaluate(
            _req(quantity=5),
            estimated_price=100.0,
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "allow")

    def test_blocks_when_quantity_zero_or_negative(self) -> None:
        engine = RiskEngine(_cfg())
        out = engine.evaluate(_req(quantity=0), now=_OPEN_TUESDAY)
        self.assertEqual(out.decision, "block")
        self.assertIn(
            RiskFlagCode.INVALID_PARAMETERS,
            [f.code for f in out.flags],
        )

    def test_blocks_when_limit_order_missing_limit_price(self) -> None:
        engine = RiskEngine(_cfg())
        out = engine.evaluate(
            _req(order_type=OrderType.LIMIT, limit_price=None),
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "block")
        invalid = [f for f in out.flags if f.code == RiskFlagCode.INVALID_PARAMETERS]
        self.assertTrue(any("limit_price" in f.message for f in invalid))

    def test_blocks_when_symbol_not_in_allowlist(self) -> None:
        engine = RiskEngine(_cfg(trading_symbol_allowlist=["MSFT"]))
        out = engine.evaluate(_req(symbol="AAPL"), estimated_price=100, now=_OPEN_TUESDAY)
        self.assertEqual(out.decision, "block")
        self.assertIn(
            RiskFlagCode.SYMBOL_NOT_ALLOWED,
            [f.code for f in out.flags],
        )

    def test_blocks_when_symbol_in_denylist_even_if_in_allowlist(self) -> None:
        engine = RiskEngine(_cfg(
            trading_symbol_allowlist=["AAPL"],
            trading_symbol_denylist=["AAPL"],
        ))
        out = engine.evaluate(_req(symbol="AAPL"), estimated_price=100, now=_OPEN_TUESDAY)
        self.assertEqual(out.decision, "block")
        # Denylist wins
        self.assertIn(
            RiskFlagCode.SYMBOL_DENYLISTED,
            [f.code for f in out.flags],
        )

    def test_blocks_buy_exceeding_max_position_value(self) -> None:
        engine = RiskEngine(_cfg(trading_max_position_value=500))
        out = engine.evaluate(
            _req(quantity=10),
            estimated_price=100.0,  # 10*100 = 1000 > 500
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "block")
        self.assertIn(
            RiskFlagCode.POSITION_SIZE_EXCEEDED,
            [f.code for f in out.flags],
        )

    def test_blocks_buy_exceeding_max_position_pct(self) -> None:
        engine = RiskEngine(_cfg(
            trading_max_position_value=999_999_999,  # don't trip absolute
            trading_max_position_pct=0.10,
        ))
        out = engine.evaluate(
            _req(quantity=10),
            estimated_price=100.0,  # notional 1000
            portfolio_snapshot={"total_equity": 5000},  # 1000/5000 = 20% > 10%
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "block")
        self.assertIn(
            RiskFlagCode.POSITION_PCT_EXCEEDED,
            [f.code for f in out.flags],
        )

    def test_skips_position_size_check_for_sell_side(self) -> None:
        engine = RiskEngine(_cfg(trading_max_position_value=500))
        out = engine.evaluate(
            _req(side=OrderSide.SELL, quantity=10),
            estimated_price=100,
            portfolio_snapshot={"positions": [
                {"symbol": "AAPL", "quantity": 100},
            ]},
            now=_OPEN_TUESDAY,
        )
        # Sell isn't blocked by position-size limits
        position_size = [f for f in out.flags if f.code == RiskFlagCode.POSITION_SIZE_EXCEEDED]
        self.assertEqual(position_size, [])

    def test_blocks_sell_exceeding_held_quantity_oversell(self) -> None:
        engine = RiskEngine(_cfg())
        out = engine.evaluate(
            _req(side=OrderSide.SELL, quantity=20),
            estimated_price=100,
            portfolio_snapshot={"positions": [
                {"symbol": "AAPL", "quantity": 5},
            ]},
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "block")
        self.assertIn(RiskFlagCode.OVERSELL, [f.code for f in out.flags])

    def test_blocks_when_daily_turnover_exceeds_cap_via_audit_rollup(self) -> None:
        engine = RiskEngine(_cfg(trading_max_daily_turnover=1000))
        out = engine.evaluate(
            _req(quantity=10),
            estimated_price=100,  # 1000 notional
            daily_turnover_so_far=900,  # already 900; +1000 = 1900 > 1000
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "block")
        self.assertIn(
            RiskFlagCode.DAILY_TURNOVER_EXCEEDED,
            [f.code for f in out.flags],
        )

    def test_blocks_when_market_closed_strict_mode(self) -> None:
        engine = RiskEngine(_cfg(trading_market_hours_strict=True))
        out = engine.evaluate(_req(), estimated_price=100, now=_CLOSED_SAT)
        self.assertEqual(out.decision, "block")
        market_flags = [f for f in out.flags if f.code == RiskFlagCode.MARKET_CLOSED]
        self.assertTrue(any(f.severity == RiskSeverity.BLOCK for f in market_flags))

    def test_warns_but_allows_when_market_closed_strict_disabled(self) -> None:
        engine = RiskEngine(_cfg(trading_market_hours_strict=False))
        out = engine.evaluate(_req(), estimated_price=100, now=_CLOSED_SAT)
        self.assertEqual(out.decision, "allow")
        market_flags = [f for f in out.flags if f.code == RiskFlagCode.MARKET_CLOSED]
        self.assertTrue(any(f.severity == RiskSeverity.INFO for f in market_flags))

    def test_emits_info_flag_when_broker_logged_out_in_paper_mode(self) -> None:
        engine = RiskEngine(_cfg())
        out = engine.evaluate(
            _req(),
            estimated_price=100,
            broker_status={"logged_in": False, "broker": "firstrade"},
            now=_OPEN_TUESDAY,
        )
        self.assertEqual(out.decision, "allow")  # info-only, not block
        broker_flags = [f for f in out.flags if f.code == RiskFlagCode.BROKER_NOT_LOGGED_IN]
        self.assertEqual(len(broker_flags), 1)
        self.assertEqual(broker_flags[0].severity, RiskSeverity.INFO)

    def test_config_snapshot_captures_all_active_thresholds(self) -> None:
        cfg = _cfg(
            trading_max_position_value=1234.0,
            trading_max_daily_turnover=5678.0,
            trading_symbol_allowlist=["AAPL", "MSFT"],
        )
        engine = RiskEngine(cfg)
        out = engine.evaluate(_req(), estimated_price=100, now=_OPEN_TUESDAY)
        snap = out.config_snapshot
        self.assertEqual(snap["trading_max_position_value"], 1234.0)
        self.assertEqual(snap["trading_max_daily_turnover"], 5678.0)
        self.assertEqual(snap["trading_symbol_allowlist"], ["AAPL", "MSFT"])
        self.assertEqual(snap["trading_mode"], "paper")


if __name__ == "__main__":
    unittest.main()
