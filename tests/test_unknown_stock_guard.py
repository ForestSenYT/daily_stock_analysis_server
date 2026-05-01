# -*- coding: utf-8 -*-
"""Tests for the defensive ``_is_unknown_stock_input`` guard in
``StockAnalysisPipeline``.

Triggered by a real production incident where ``STOCK_LIST`` contained
``APPL`` (typo for ``AAPL``); every data source failed but the LLM still
hallucinated a report based on training-memory AAPL prices. The guard
short-circuits when realtime / historical / fundamental ALL fail,
returning ``None`` to ``AnalysisService`` instead of letting the LLM
produce misleading output.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ``src.core.pipeline`` pulls in litellm / data-source SDKs / news
# parsers transitively. CI installs all of them; lightweight dev shells
# may not. The guard under test is a pure method, so the test is
# meaningful in either env — but only if we can import the class.
try:
    from src.core.pipeline import StockAnalysisPipeline
except Exception as _import_exc:  # pragma: no cover - local-shell fallback
    pytest.skip(
        f"src.core.pipeline import failed (likely missing dev dep): {_import_exc}",
        allow_module_level=True,
    )


def _bare_pipeline(strict: bool = True) -> StockAnalysisPipeline:
    """Build a Pipeline instance with only the bits the guard needs.

    The full constructor is heavy (data fetcher, search service, agent
    factory, etc.); the guard is a pure method on Config + arguments,
    so we bypass __init__ and inject the minimum.
    """
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(strict_unknown_stock_guard=strict)
    return pipeline


def _coverage(*, has_data: bool) -> dict:
    blocks = ("valuation", "growth", "earnings", "institution")
    if has_data:
        return {block: "ok" for block in blocks}
    return {block: "failed" for block in blocks}


def _quote(price: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(price=price, name="Real Co.")


class IsUnknownStockInputTests(unittest.TestCase):
    def test_real_ticker_with_realtime_quote_passes(self) -> None:
        # Most common case — real ticker, realtime fetch succeeds.
        # Guard MUST NOT trip even with empty DB / failed fundamentals.
        p = _bare_pipeline(strict=True)
        self.assertFalse(
            p._is_unknown_stock_input(
                code="AAPL",
                stock_name="Apple Inc.",
                realtime_quote=_quote(284.5),
                fundamental_context={"coverage": _coverage(has_data=False)},
                historical_bars=None,
            ),
            "Guard tripped on a real ticker that had realtime data — would "
            "block legitimate analyses on fresh-DB cold starts.",
        )

    def test_real_ticker_with_historical_only_passes(self) -> None:
        # Realtime might fail at e.g. weekend or off-hours; if we have
        # historical bars in the DB, the ticker is real.
        p = _bare_pipeline(strict=True)
        self.assertFalse(
            p._is_unknown_stock_input(
                code="600519",
                stock_name="贵州茅台",
                realtime_quote=None,
                fundamental_context={"coverage": _coverage(has_data=False)},
                historical_bars=[object(), object()],  # any non-empty list
            )
        )

    def test_real_ticker_with_fundamental_coverage_passes(self) -> None:
        # Edge case: weekend + brand new in DB but fundamental data exists.
        p = _bare_pipeline(strict=True)
        self.assertFalse(
            p._is_unknown_stock_input(
                code="NVDA",
                stock_name="NVIDIA",
                realtime_quote=None,
                fundamental_context={"coverage": _coverage(has_data=True)},
                historical_bars=None,
            )
        )

    def test_unknown_ticker_with_all_signals_failing_trips(self) -> None:
        # The exact production incident: APPL is a typo for AAPL — no
        # data source has it. Guard MUST trip.
        p = _bare_pipeline(strict=True)
        self.assertTrue(
            p._is_unknown_stock_input(
                code="APPL",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={"coverage": _coverage(has_data=False)},
                historical_bars=None,
            )
        )

    def test_unknown_ticker_empty_fundamental_dict_trips(self) -> None:
        # ``fundamental_context`` may be ``{}`` when the fetcher pipeline
        # itself bailed before producing a coverage map.
        p = _bare_pipeline(strict=True)
        self.assertTrue(
            p._is_unknown_stock_input(
                code="XXXX",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={},
                historical_bars=None,
            )
        )

    def test_unknown_ticker_none_fundamental_trips(self) -> None:
        p = _bare_pipeline(strict=True)
        self.assertTrue(
            p._is_unknown_stock_input(
                code="XXXX",
                stock_name=None,
                realtime_quote=None,
                fundamental_context=None,
                historical_bars=None,
            )
        )

    def test_unknown_ticker_empty_list_for_historical_trips(self) -> None:
        # Empty list is falsy in Python; treat as "no historical data".
        p = _bare_pipeline(strict=True)
        self.assertTrue(
            p._is_unknown_stock_input(
                code="XXXX",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={"coverage": _coverage(has_data=False)},
                historical_bars=[],
            )
        )

    def test_not_supported_coverage_does_not_save_unknown(self) -> None:
        # ``not_supported`` is a fundamental-pipeline equivalent of
        # ``failed``: should still count as no-data, so guard trips.
        p = _bare_pipeline(strict=True)
        self.assertTrue(
            p._is_unknown_stock_input(
                code="XXXX",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={"coverage": {"valuation": "not_supported"}},
                historical_bars=None,
            )
        )

    def test_partial_fundamental_coverage_passes(self) -> None:
        # Even one block of real fundamental data → real ticker.
        p = _bare_pipeline(strict=True)
        self.assertFalse(
            p._is_unknown_stock_input(
                code="0700",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={
                    "coverage": {
                        "valuation": "ok",
                        "growth": "failed",
                        "earnings": "not_supported",
                    }
                },
                historical_bars=None,
            )
        )

    def test_disabled_flag_makes_guard_a_noop(self) -> None:
        # Power users / debugging may want the old behavior; flag must
        # honor that.
        p = _bare_pipeline(strict=False)
        self.assertFalse(
            p._is_unknown_stock_input(
                code="APPL",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={},
                historical_bars=None,
            )
        )


class GuardConfigDefaultsTests(unittest.TestCase):
    def test_config_dataclass_has_strict_field_default_true(self) -> None:
        # The cheapest regression test: the field exists on Config and
        # defaults to True so a fresh deploy gets the protection out of
        # the box.
        from dataclasses import fields
        from src.config import Config

        names = {f.name: f.default for f in fields(Config)}
        self.assertIn("strict_unknown_stock_guard", names)
        self.assertEqual(names["strict_unknown_stock_guard"], True)


class GuardLogsLoudlyTests(unittest.TestCase):
    def test_guard_logs_warning_when_tripping(self) -> None:
        p = _bare_pipeline(strict=True)
        with self.assertLogs("src.core.pipeline", level="WARNING") as cm:
            p._is_unknown_stock_input(
                code="APPL",
                stock_name=None,
                realtime_quote=None,
                fundamental_context={},
                historical_bars=None,
            )
        joined = "\n".join(cm.output)
        self.assertIn("UnknownStockGuard", joined)
        self.assertIn("APPL", joined)


if __name__ == "__main__":
    unittest.main()
