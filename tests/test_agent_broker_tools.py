# -*- coding: utf-8 -*-
"""Agent broker-tool tests.

Verifies:
  * The tool is conditionally registered in ``factory.get_tool_registry``
    only when ``BROKER_FIRSTRADE_ENABLED=true`` at process start.
  * The tool reads ONLY the local snapshot — never instantiates or
    calls the FirstradeReadOnlyClient.
  * ``no_snapshot`` / ``stale`` / scope-respecting projections.
  * Output never contains sensitive fields, raw account numbers, or
  * the raw_payload echoes.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.agent.tools.broker_tools import (
    ALL_BROKER_TOOLS,
    DEFAULT_MAX_AGE_SECONDS,
    _handle_get_live_broker_portfolio_snapshot,
)


def _config(enabled: bool = True, scope: str = "positions_and_balances") -> SimpleNamespace:
    return SimpleNamespace(
        broker_firstrade_enabled=enabled,
        broker_firstrade_llm_data_scope=scope,
    )


SAMPLE_SNAPSHOT = {
    "status": "ok",
    "broker": "firstrade",
    "as_of": None,  # patched per-test
    "last_sync": None,
    "accounts": [
        {
            "account_alias": "Firstrade ****1234",
            "account_last4": "1234",
            "account_hash": "abcd1234abcd1234",
            "as_of": None,
        },
    ],
    "balances": [
        {
            "account_alias": "Firstrade ****1234",
            "account_hash": "abcd1234abcd1234",
            "as_of": None,
            "payload": {
                "cash": 1000.0,
                "buying_power": 2000.0,
                "total_value": 5000.0,
                "currency": "USD",
            },
        },
    ],
    "positions": [
        {
            "account_alias": "Firstrade ****1234",
            "account_hash": "abcd1234abcd1234",
            "symbol": "AAPL",
            "as_of": None,
            "payload": {
                "symbol": "AAPL",
                "quantity": 10,
                "market_value": 1750.0,
                "avg_cost": 150.0,
                "last_price": 175.0,
                "unrealized_pnl": 250.0,
                "currency": "USD",
            },
        },
    ],
    "orders": [
        {
            "account_alias": "Firstrade ****1234",
            "account_hash": "abcd1234abcd1234",
            "symbol": "AAPL",
            "entity_hash": "ord1234567890abc",
            "as_of": None,
            "payload": {
                "order_status": "open",
                "order_side": "buy",
                "order_type": "limit",
                "order_quantity": 5,
                "filled_quantity": 0,
                "limit_price": 170,
            },
        },
    ],
    "transactions": [
        {
            "account_alias": "Firstrade ****1234",
            "account_hash": "abcd1234abcd1234",
            "entity_hash": "tx1234567890abcd",
            "symbol": "AAPL",
            "payload": {
                "transaction_type": "buy",
                "trade_date": "2025-12-01",
                "amount": -875,
                "quantity": 5,
                "currency": "USD",
            },
        },
    ],
}


class ToolDefinitionTests(unittest.TestCase):
    def test_tool_list_exposes_only_one_tool(self) -> None:
        names = [t.name for t in ALL_BROKER_TOOLS]
        self.assertEqual(names, ["get_live_broker_portfolio_snapshot"])

    def test_tool_description_mentions_read_only_and_no_login(self) -> None:
        desc = ALL_BROKER_TOOLS[0].description.lower()
        # The description is what the LLM sees; it must signal both
        # safety properties so the model self-restricts.
        self.assertIn("read-only", desc)
        self.assertIn("does not log into firstrade", desc)
        self.assertIn("does not trigger a sync", desc)


class HandlerBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config(enabled=True)

    def _patch_service(self, snapshot):
        """Patch _service() to return a mock whose get_snapshot()
        returns ``snapshot``. Returns the mock for assertions."""
        svc = MagicMock()
        svc.get_snapshot.return_value = snapshot
        return patch(
            "src.agent.tools.broker_tools._service", return_value=svc,
        ), svc

    def test_disabled_short_circuits(self) -> None:
        with patch(
            "src.agent.tools.broker_tools._config",
            return_value=_config(enabled=False),
        ):
            result = _handle_get_live_broker_portfolio_snapshot()
        self.assertEqual(result["status"], "not_enabled")

    def test_no_snapshot(self) -> None:
        empty_snapshot = {
            "status": "ok", "broker": "firstrade", "as_of": None,
            "accounts": [], "balances": [], "positions": [],
            "orders": [], "transactions": [],
        }
        ctx, svc = self._patch_service(empty_snapshot)
        with patch("src.agent.tools.broker_tools._config", return_value=self.config), ctx:
            result = _handle_get_live_broker_portfolio_snapshot()
        self.assertEqual(result["status"], "no_snapshot")
        # Tool MUST never have invoked the firstrade client — verify
        # the service mock has no calls to login/sync_now.
        self.assertFalse(svc.login.called)
        self.assertFalse(svc.sync_now.called)
        self.assertFalse(svc.verify_mfa.called)

    def test_positions_only_scope_omits_balances_and_orders(self) -> None:
        from datetime import datetime, timezone
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = datetime.now(timezone.utc).isoformat()
        ctx, _ = self._patch_service(snap)
        with patch(
            "src.agent.tools.broker_tools._config",
            return_value=_config(enabled=True, scope="positions_only"),
        ), ctx:
            result = _handle_get_live_broker_portfolio_snapshot()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope"], "positions_only")
        self.assertNotIn("balances", result)
        self.assertNotIn("orders", result)
        self.assertNotIn("transactions", result)

    def test_positions_and_balances_scope_omits_orders_and_transactions(self) -> None:
        from datetime import datetime, timezone
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = datetime.now(timezone.utc).isoformat()
        ctx, _ = self._patch_service(snap)
        with patch(
            "src.agent.tools.broker_tools._config",
            return_value=_config(enabled=True, scope="positions_and_balances"),
        ), ctx:
            result = _handle_get_live_broker_portfolio_snapshot()
        self.assertEqual(result["scope"], "positions_and_balances")
        self.assertIn("balances", result)
        self.assertNotIn("orders", result)
        self.assertNotIn("transactions", result)

    def test_full_scope_includes_orders(self) -> None:
        from datetime import datetime, timezone
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = datetime.now(timezone.utc).isoformat()
        ctx, _ = self._patch_service(snap)
        with patch(
            "src.agent.tools.broker_tools._config",
            return_value=_config(enabled=True, scope="full"),
        ), ctx:
            result = _handle_get_live_broker_portfolio_snapshot(
                include_transactions=True,
            )
        self.assertEqual(result["scope"], "full")
        self.assertIn("orders", result)
        self.assertIn("transactions", result)
        # Confirm projection: orders carry order_id_hash, NOT raw id.
        self.assertEqual(
            result["orders"][0]["order_id_hash"], "ord1234567890abc",
        )

    def test_stale_snapshot_returns_status_stale_with_data(self) -> None:
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = old_ts
        ctx, _ = self._patch_service(snap)
        with patch("src.agent.tools.broker_tools._config", return_value=self.config), ctx:
            result = _handle_get_live_broker_portfolio_snapshot(
                max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
            )
        self.assertEqual(result["status"], "stale")
        self.assertIn("warning", result)
        # Stale data is still returned for LLM reasoning.
        self.assertGreaterEqual(len(result["positions"]), 1)

    def test_response_carries_age_seconds_and_as_of_iso(self) -> None:
        from datetime import datetime, timezone
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = datetime.now(timezone.utc).isoformat()
        ctx, _ = self._patch_service(snap)
        with patch("src.agent.tools.broker_tools._config", return_value=self.config), ctx:
            result = _handle_get_live_broker_portfolio_snapshot()
        self.assertIn("as_of_iso", result)
        self.assertIn("age_seconds", result)
        self.assertIsInstance(result["age_seconds"], int)

    def test_response_never_contains_raw_payload(self) -> None:
        from datetime import datetime, timezone
        snap = dict(SAMPLE_SNAPSHOT)
        snap["as_of"] = datetime.now(timezone.utc).isoformat()
        ctx, _ = self._patch_service(snap)
        with patch(
            "src.agent.tools.broker_tools._config",
            return_value=_config(enabled=True, scope="full"),
        ), ctx:
            result = _handle_get_live_broker_portfolio_snapshot(
                include_transactions=True,
            )
        # The projection deliberately drops `payload` and `raw_payload`.
        for bucket_name in ("accounts", "balances", "positions", "orders", "transactions"):
            for row in result.get(bucket_name, []):
                self.assertNotIn("raw_payload", row)
                self.assertNotIn("payload", row)


class FactoryRegistrationTests(unittest.TestCase):
    """The factory registers broker tools ONLY when the flag is on at
    process start. Reset its module-level cache between cases so the
    test doesn't pick up state from another suite."""

    def setUp(self) -> None:
        import src.agent.factory as factory
        factory._TOOL_REGISTRY = None
        self._factory = factory

    def _build_with_flag(self, enabled: bool):
        from src.config import Config
        # We don't need a full Config here — the factory only checks
        # the broker_firstrade_enabled attribute. Patch get_config().
        fake = SimpleNamespace(broker_firstrade_enabled=enabled)
        with patch("src.config.get_config", return_value=fake):
            self._factory._TOOL_REGISTRY = None  # force rebuild
            return self._factory.get_tool_registry()

    def test_broker_tool_registered_when_flag_on(self) -> None:
        registry = self._build_with_flag(True)
        names = set(registry.list_names())
        self.assertIn("get_live_broker_portfolio_snapshot", names)

    def test_broker_tool_absent_when_flag_off(self) -> None:
        registry = self._build_with_flag(False)
        names = set(registry.list_names())
        self.assertNotIn("get_live_broker_portfolio_snapshot", names)

    def test_existing_tools_unaffected(self) -> None:
        registry = self._build_with_flag(False)
        names = set(registry.list_names())
        # A representative subset of pre-existing tools that must
        # always remain registered regardless of the broker flag.
        for required in (
            "get_realtime_quote", "get_daily_history",
            "analyze_trend", "calculate_ma",
            "list_quant_factors", "evaluate_quant_factor",
        ):
            self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
