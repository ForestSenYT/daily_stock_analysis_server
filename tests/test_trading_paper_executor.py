# -*- coding: utf-8 -*-
"""PaperExecutor — 8 cases covering fill price logic + fallback."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from src.trading.executors.paper import PaperExecutor, _NotFillable
from src.trading.types import (
    ExecutionMode,
    ExecutionStatus,
    OrderRequest,
    OrderSide,
    OrderType,
)


def _cfg(**overrides):
    base = dict(
        trading_mode="paper",
        trading_paper_slippage_bps=5,
        trading_paper_fee_per_trade=0.0,
        trading_paper_account_id=42,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _req(**overrides):
    base = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
        request_uid=f"u-{overrides.get('symbol', 'AAPL')}-paper-test",
        market="us",
        account_id=42,
    )
    base.update(overrides)
    return OrderRequest(**base)


class _FakePortfolioService:
    """Stand-in for the real PortfolioService — captures the call."""

    def __init__(self, *, raises: Optional[Exception] = None) -> None:
        self.raises = raises
        self.last_call: Optional[Dict[str, Any]] = None

    def record_trade(self, **kwargs) -> Dict[str, Any]:
        if self.raises:
            raise self.raises
        self.last_call = kwargs
        return {"id": 7}


def _fake_quote(*, bid=199.0, ask=200.0, last=199.5):
    return {
        "source": "firstrade", "symbol": "AAPL",
        "bid": bid, "ask": ask, "last": last,
        "high": 201.0, "low": 198.0,
    }


class PaperExecutorFillPriceTests(unittest.TestCase):
    def _executor_with_quote(self, quote: Optional[Dict[str, Any]],
                             *, fake_svc: Optional[Any] = None,
                             cfg=None) -> PaperExecutor:
        ex = PaperExecutor(config=cfg or _cfg())
        ex._resolve_quote = MagicMock(return_value=quote)  # type: ignore[method-assign]
        return ex

    def test_buy_market_fills_at_ask_plus_slippage(self) -> None:
        ex = self._executor_with_quote(_fake_quote(bid=199, ask=200))
        fake = _FakePortfolioService()
        with patch("src.services.portfolio_service.PortfolioService", lambda: fake):
            result = ex.submit(_req(side=OrderSide.BUY, quantity=10))
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        # 200 * (1 + 5/10000) = 200.1
        self.assertAlmostEqual(result.fill_price, 200.1, places=4)
        self.assertEqual(fake.last_call["price"], result.fill_price)
        self.assertEqual(fake.last_call["source"], "paper")

    def test_sell_market_fills_at_bid_minus_slippage(self) -> None:
        ex = self._executor_with_quote(_fake_quote(bid=199, ask=200))
        fake = _FakePortfolioService()
        with patch("src.services.portfolio_service.PortfolioService", lambda: fake):
            result = ex.submit(_req(side=OrderSide.SELL, quantity=10))
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        # 199 * (1 - 5/10000) = 198.9005
        self.assertAlmostEqual(result.fill_price, 198.9005, places=4)

    def test_buy_limit_does_not_fill_when_ask_above_limit_returns_failed(self) -> None:
        ex = self._executor_with_quote(_fake_quote(bid=199, ask=210))
        result = ex.submit(_req(
            side=OrderSide.BUY, order_type=OrderType.LIMIT, limit_price=200,
        ))
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "LIMIT_NOT_REACHABLE")

    def test_sell_limit_fills_at_bid_when_bid_at_or_above_limit(self) -> None:
        ex = self._executor_with_quote(_fake_quote(bid=205, ask=206))
        fake = _FakePortfolioService()
        with patch("src.services.portfolio_service.PortfolioService", lambda: fake):
            result = ex.submit(_req(
                side=OrderSide.SELL, order_type=OrderType.LIMIT, limit_price=200,
            ))
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        # max(bid=205, limit=200) = 205
        self.assertEqual(result.fill_price, 205.0)


class PaperExecutorFallbackTests(unittest.TestCase):
    def test_falls_back_to_data_provider_when_firstrade_quote_none(self) -> None:
        """Firstrade returns None → data_provider returns valid quote
        → executor still fills successfully."""
        ex = PaperExecutor(config=_cfg())
        # Patch the provider chain to return our fake quote
        ex._resolve_quote = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_quote(bid=99, ask=100, last=99.5),
        )
        fake = _FakePortfolioService()
        with patch("src.services.portfolio_service.PortfolioService", lambda: fake):
            result = ex.submit(_req(quantity=2))
        self.assertEqual(result.status, ExecutionStatus.FILLED)

    def test_returns_failed_with_quote_unavailable_when_all_sources_none(self) -> None:
        ex = PaperExecutor(config=_cfg())
        ex._resolve_quote = MagicMock(return_value=None)  # type: ignore[method-assign]
        result = ex.submit(_req())
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "QUOTE_UNAVAILABLE")


class PaperExecutorPersistenceTests(unittest.TestCase):
    def test_persists_paper_trade_with_source_tag_and_uid_dedup(self) -> None:
        ex = PaperExecutor(config=_cfg())
        ex._resolve_quote = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_quote(),
        )
        fake = _FakePortfolioService()
        with patch("src.services.portfolio_service.PortfolioService", lambda: fake):
            req = _req(request_uid="my-uid-12345")
            ex.submit(req)
        self.assertEqual(fake.last_call["source"], "paper")
        self.assertEqual(fake.last_call["trade_uid"], "my-uid-12345")
        self.assertTrue(fake.last_call["note"].startswith("[paper]"))

    def test_does_not_import_firstrade_order_module_under_any_branch(self) -> None:
        """Static AST guard: parse paper.py and assert no Import or
        ImportFrom node references ``firstrade.order`` /
        ``firstrade.trade``. Docstrings and comments mentioning these
        names in NEGATIVE form (i.e. documenting the invariant) are
        explicitly allowed."""
        import ast
        from pathlib import Path
        tree = ast.parse(Path("src/trading/executors/paper.py").read_text(encoding="utf-8"))
        forbidden_modules = {"firstrade.order", "firstrade.trade"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in forbidden_modules:
                    self.fail(
                        f"paper.py imports {module!r} — read-only "
                        "invariant violated"
                    )
                # ``from firstrade import order`` shape:
                if module == "firstrade":
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name, {"order", "trade"},
                            msg=f"paper.py imports firstrade.{alias.name}",
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        self.fail(
                            f"paper.py imports {alias.name!r} — read-only "
                            "invariant violated"
                        )


if __name__ == "__main__":
    unittest.main()
