# -*- coding: utf-8 -*-
"""Agent ``propose_trade`` tool — emit-only contract."""

from __future__ import annotations

import os
import sys
import unittest


def _set_mode(value: str) -> None:
    if value:
        os.environ["TRADING_MODE"] = value
    else:
        os.environ.pop("TRADING_MODE", None)


def _reset_config():
    """Reset only the singleton instance — do NOT delete the module
    from sys.modules, because other modules hold bound references
    to the Config class and its functions, and replacing the module
    object orphans those references (real bug we hit in cross-suite
    runs)."""
    from src.config import Config
    Config._instance = None


class AgentProposeTradeTests(unittest.TestCase):
    def setUp(self) -> None:
        _set_mode("paper")
        _reset_config()
        # Reload the tool module so it picks up the new config
        for m in list(sys.modules):
            if m.startswith("src.agent.tools.trading_tools"):
                del sys.modules[m]

    def test_emits_order_request_dict_with_required_fields(self) -> None:
        from src.agent.tools.trading_tools import _handle_propose_trade
        out = _handle_propose_trade(
            symbol="AAPL", side="buy", quantity=5,
            order_type="limit", limit_price=190.0,
            market="us",
        )
        self.assertEqual(out["status"], "proposal_emitted")
        intent = out["intent"]
        for key in (
            "symbol", "side", "quantity", "order_type", "limit_price",
            "time_in_force", "market", "currency", "note",
            "account_id", "agent_session_id", "source", "request_uid",
        ):
            self.assertIn(key, intent)
        self.assertEqual(intent["source"], "agent")
        self.assertTrue(intent["request_uid"].startswith("agent-"))

    def test_does_not_call_trading_service_submit(self) -> None:
        """The tool must not import or call the submit pipeline.
        We patch the orchestrator to fail loudly if it's reached."""
        from unittest.mock import patch

        with patch(
            "src.services.trading_service.get_trading_service",
            side_effect=AssertionError(
                "propose_trade must NOT call get_trading_service"),
        ):
            from src.agent.tools.trading_tools import _handle_propose_trade
            out = _handle_propose_trade(
                symbol="AAPL", side="buy", quantity=1,
            )
        self.assertEqual(out["status"], "proposal_emitted")

    def test_returns_not_enabled_when_trading_mode_disabled(self) -> None:
        _set_mode("disabled")
        _reset_config()
        for m in list(sys.modules):
            if m.startswith("src.agent.tools.trading_tools"):
                del sys.modules[m]
        from src.agent.tools.trading_tools import _handle_propose_trade
        out = _handle_propose_trade(symbol="AAPL", side="buy", quantity=1)
        self.assertEqual(out["status"], "not_enabled")

    def test_marks_emitted_intent_with_source_agent(self) -> None:
        from src.agent.tools.trading_tools import _handle_propose_trade
        out = _handle_propose_trade(
            symbol="MSFT", side="sell", quantity=2,
            agent_session_id="sess-123",
        )
        self.assertEqual(out["intent"]["source"], "agent")
        self.assertEqual(out["intent"]["agent_session_id"], "sess-123")


if __name__ == "__main__":
    unittest.main()
