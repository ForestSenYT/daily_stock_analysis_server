# -*- coding: utf-8 -*-
"""FirstradeSyncService tests with mocked client + repo.

We never touch the real DB or vendor SDK here — everything goes
through MagicMocks. The shape we pin:
  * Disabled / not_installed / login_required / mfa_required short-circuits.
  * sync_now writes a sync_run row regardless of success.
  * Failed sync_now writes status=failed + error_payload (redacted).
  * Local reads never call the client.
  * No public method's return value contains sensitive fields.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.brokers.base import BrokerLoginResult, BrokerSnapshot
from src.services.firstrade_sync_service import FirstradeSyncService


SENSITIVE_TOKENS = (
    "username", "password", "pin", "mfa_secret", "ftat", "sid",
    "cookie", "access_token", "Authorization",
)


def _config(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        broker_firstrade_enabled=enabled,
        broker_firstrade_read_only=True,
        broker_firstrade_trading_enabled=False,
        broker_firstrade_llm_data_scope="positions_and_balances",
        broker_account_hash_salt="test-salt",
    )


def _client(*, logged_in: bool = True, login_result=None,
            verify_result=None, snapshot=None):
    c = MagicMock()
    c.is_enabled.return_value = True
    c.is_logged_in.return_value = logged_in
    c.login.return_value = login_result or BrokerLoginResult(status="ok", account_count=1)
    c.verify_mfa.return_value = verify_result or BrokerLoginResult(status="ok", account_count=1)
    c.build_snapshot.return_value = snapshot or BrokerSnapshot(
        broker="firstrade", as_of="2025-12-01T00:00:00+00:00",
    )
    return c


def _repo():
    repo = MagicMock()
    repo.save_sync_run_start.return_value = 42
    repo.save_full_snapshot.return_value = {
        "accounts": 1, "balances": 1, "positions": 5,
        "orders": 0, "transactions": 0,
    }
    repo.get_last_sync_run.return_value = None
    repo.get_latest_accounts.return_value = []
    repo.get_latest_positions.return_value = []
    repo.get_latest_orders.return_value = []
    repo.get_latest_balances.return_value = []
    repo.get_latest_transactions.return_value = []
    repo.get_latest_snapshot.return_value = {
        "broker": "firstrade", "as_of": None, "last_sync": None,
        "accounts": [], "balances": [], "positions": [],
        "orders": [], "transactions": [],
    }
    return repo


def _has_sensitive_field(payload) -> bool:
    """Recursively check every dict key for a sensitive name."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str) and k.strip().lower() in {
                "username", "password", "pin", "mfa_secret",
                "ftat", "sid", "cookie", "access_token",
                "authorization", "account",
            }:
                # The value must already be redacted; flag if not.
                if v != "***REDACTED***":
                    return True
            if _has_sensitive_field(v):
                return True
    elif isinstance(payload, list):
        for item in payload:
            if _has_sensitive_field(item):
                return True
    return False


class StatusPathTests(unittest.TestCase):
    def test_disabled_returns_not_enabled(self) -> None:
        svc = FirstradeSyncService(
            config=_config(enabled=False),
            client=_client(),
            repo=_repo(),
        )
        result = svc.get_status()
        self.assertEqual(result["status"], "not_enabled")
        self.assertFalse(result["enabled"])

    def test_status_ok_includes_last_sync(self) -> None:
        repo = _repo()
        repo.get_last_sync_run.return_value = {
            "id": 1, "status": "ok", "started_at": "2025-12-01T00:00:00",
        }
        svc = FirstradeSyncService(config=_config(), client=_client(), repo=repo)
        result = svc.get_status()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["enabled"])
        self.assertIsNotNone(result["last_sync"])


class LoginPathTests(unittest.TestCase):
    def test_disabled_short_circuits_login(self) -> None:
        c = _client()
        svc = FirstradeSyncService(config=_config(enabled=False), client=c, repo=_repo())
        result = svc.login()
        self.assertEqual(result["status"], "not_enabled")
        c.login.assert_not_called()

    def test_mfa_required_propagates(self) -> None:
        c = _client(login_result=BrokerLoginResult(status="mfa_required"))
        svc = FirstradeSyncService(config=_config(), client=c, repo=_repo())
        result = svc.login()
        self.assertEqual(result["status"], "mfa_required")

    def test_verify_mfa_rejects_empty_code(self) -> None:
        c = _client()
        svc = FirstradeSyncService(config=_config(), client=c, repo=_repo())
        result = svc.verify_mfa("   ")
        self.assertEqual(result["status"], "failed")
        c.verify_mfa.assert_not_called()

    def test_verify_mfa_session_lost_propagates(self) -> None:
        c = _client(verify_result=BrokerLoginResult(
            status="session_lost", message="Please re-login.",
        ))
        svc = FirstradeSyncService(config=_config(), client=c, repo=_repo())
        result = svc.verify_mfa("123456")
        self.assertEqual(result["status"], "session_lost")


class SyncPathTests(unittest.TestCase):
    def test_login_required_when_not_logged_in(self) -> None:
        c = _client(logged_in=False)
        svc = FirstradeSyncService(config=_config(), client=c, repo=_repo())
        result = svc.sync_now()
        self.assertEqual(result["status"], "login_required")
        # No sync_run row should be opened when we never get past the gate.
        c.build_snapshot.assert_not_called()

    def test_sync_ok_returns_counts(self) -> None:
        repo = _repo()
        c = _client()
        svc = FirstradeSyncService(config=_config(), client=c, repo=repo)
        result = svc.sync_now()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["account_count"], 1)
        self.assertEqual(result["position_count"], 5)
        repo.save_sync_run_start.assert_called_once()
        repo.save_full_snapshot.assert_called_once()
        repo.finish_sync_run.assert_called_once()
        # finish_sync_run was called with status="ok"
        kwargs = repo.finish_sync_run.call_args.kwargs
        self.assertEqual(kwargs["status"], "ok")

    def test_sync_failure_writes_failed_run(self) -> None:
        repo = _repo()
        c = _client()
        c.build_snapshot.side_effect = RuntimeError(
            "401 for url: https://invest-api.firstrade.com/foo?sid=ABC&ftat=DEF"
        )
        svc = FirstradeSyncService(config=_config(), client=c, repo=repo)
        result = svc.sync_now()
        self.assertEqual(result["status"], "failed")
        # Ensure no token leaked into the response.
        self.assertNotIn("ABC", result.get("message", ""))
        self.assertNotIn("DEF", result.get("message", ""))
        # finish_sync_run was called with status="failed" and an error payload.
        kwargs = repo.finish_sync_run.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertIsNotNone(kwargs["error_payload"])

    def test_sync_response_carries_no_sensitive_fields(self) -> None:
        repo = _repo()
        c = _client()
        svc = FirstradeSyncService(config=_config(), client=c, repo=repo)
        result = svc.sync_now()
        self.assertFalse(_has_sensitive_field(result))


class LocalReadPathTests(unittest.TestCase):
    def test_get_snapshot_never_calls_client(self) -> None:
        c = _client()
        svc = FirstradeSyncService(config=_config(), client=c, repo=_repo())
        svc.get_snapshot()
        c.build_snapshot.assert_not_called()
        c.list_accounts.assert_not_called()

    def test_get_positions_disabled_short_circuits(self) -> None:
        c = _client()
        svc = FirstradeSyncService(config=_config(enabled=False), client=c, repo=_repo())
        result = svc.get_positions()
        self.assertEqual(result["status"], "not_enabled")


if __name__ == "__main__":
    unittest.main()
