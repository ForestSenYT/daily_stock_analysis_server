# -*- coding: utf-8 -*-
"""Unit tests for the Firstrade read-only client.

All tests mock the vendor SDK module — **zero real network calls**.
The shape we test:
  * ``not_enabled`` / ``not_installed`` short-circuits.
  * Successful login + MFA flow.
  * Account masking + hashing (account_alias / account_last4 /
    account_hash).
  * Sensitive payload redaction (sid, ftat, cookie, password, mfa_secret).
  * Mapper resilience to vendor field-name drift.
"""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.brokers.base import (
    redact_sensitive_payload,
    hash_account_number,
    mask_account_number,
)
from src.brokers.firstrade.client import (
    FirstradeReadOnlyClient,
    _sanitize_exception,
)


# =====================================================================
# Helpers
# =====================================================================

def _make_config(*, enabled: bool = True, salt: str = "test-salt-123",
                 trading_enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        broker_firstrade_enabled=enabled,
        broker_firstrade_read_only=True,
        broker_firstrade_trading_enabled=trading_enabled,
        broker_firstrade_username="user",
        broker_firstrade_password="pw",
        broker_firstrade_pin="",
        broker_firstrade_email="",
        broker_firstrade_phone="",
        broker_firstrade_mfa_secret="",
        broker_firstrade_profile_path="./test-data/firstrade",
        broker_firstrade_save_session=False,
        broker_firstrade_sync_interval_seconds=60,
        broker_firstrade_sync_market_hours_only=True,
        broker_firstrade_llm_data_scope="positions_and_balances",
        broker_account_hash_salt=salt,
    )


def _install_fake_sdk(*, need_code: bool = False, accounts=None,
                      positions=None, balance=None, orders=None,
                      transactions=None):
    """Inject a stub ``firstrade.account`` module into sys.modules.

    Returns the (firstrade_module, FTSession_mock, FTAccountData_mock)
    tuple so individual tests can assert on the mocks.
    """
    accounts = accounts if accounts is not None else [{"account": "112233445566"}]
    positions = positions or []
    orders = orders or []
    transactions = transactions or []
    balance = balance or {"cash": 100.0, "buying_power": 100.0, "total_value": 200.0}

    ft_session_instance = MagicMock(name="FTSession-instance")
    ft_session_instance.login = MagicMock(return_value=need_code)
    ft_session_instance.login_two = MagicMock(return_value=True)

    account_data_instance = MagicMock(name="FTAccountData-instance")
    account_data_instance.all_accounts = accounts
    account_data_instance.get_account_balance = MagicMock(return_value=balance)
    account_data_instance.get_positions = MagicMock(return_value=positions)
    account_data_instance.get_orders = MagicMock(return_value=orders)
    account_data_instance.get_history = MagicMock(return_value=transactions)

    FTSession = MagicMock(return_value=ft_session_instance)
    FTAccountData = MagicMock(return_value=account_data_instance)

    fake_account_mod = types.ModuleType("firstrade.account")
    fake_account_mod.FTSession = FTSession  # type: ignore[attr-defined]
    fake_account_mod.FTAccountData = FTAccountData  # type: ignore[attr-defined]

    fake_root = types.ModuleType("firstrade")
    fake_root.account = fake_account_mod  # type: ignore[attr-defined]

    sys.modules["firstrade"] = fake_root
    sys.modules["firstrade.account"] = fake_account_mod

    return fake_account_mod, FTSession, FTAccountData


def _uninstall_fake_sdk():
    sys.modules.pop("firstrade", None)
    sys.modules.pop("firstrade.account", None)


# =====================================================================
# 1) Helpers (mask / hash / redact / exception sanitiser)
# =====================================================================

class HelperTests(unittest.TestCase):
    def test_mask_account_number_keeps_only_last4(self) -> None:
        last4, alias = mask_account_number("123456789012")
        self.assertEqual(last4, "9012")
        self.assertEqual(alias, "Firstrade ****9012")

    def test_mask_account_number_handles_dashes(self) -> None:
        last4, alias = mask_account_number("123-456-1234")
        self.assertEqual(last4, "1234")
        self.assertEqual(alias, "Firstrade ****1234")

    def test_hash_account_number_is_stable(self) -> None:
        a = hash_account_number("112233", "salt-1")
        b = hash_account_number("112233", "salt-1")
        c = hash_account_number("112233", "salt-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)

    def test_hash_account_number_requires_salt(self) -> None:
        with self.assertRaises(ValueError):
            hash_account_number("112233", "")

    def test_redact_strips_known_keys(self) -> None:
        payload = {
            "username": "alice",
            "password": "p",
            "pin": "1234",
            "mfa_secret": "JBSWY3DPEHPK3PXP",
            "ftat": "tok",
            "sid": "session",
            "cookie": "c",
            "Authorization": "Bearer x",
            "account": "112233",
            "account_number": "123",
            "nested": {
                "ACCESS-TOKEN": "boop",
                "fine": "ok",
            },
            "items": [{"cookies": ["c"], "symbol": "AAPL"}],
        }
        cleaned = redact_sensitive_payload(payload)
        self.assertEqual(cleaned["username"], "***REDACTED***")
        self.assertEqual(cleaned["password"], "***REDACTED***")
        self.assertEqual(cleaned["pin"], "***REDACTED***")
        self.assertEqual(cleaned["mfa_secret"], "***REDACTED***")
        self.assertEqual(cleaned["ftat"], "***REDACTED***")
        self.assertEqual(cleaned["sid"], "***REDACTED***")
        self.assertEqual(cleaned["cookie"], "***REDACTED***")
        self.assertEqual(cleaned["Authorization"], "***REDACTED***")
        self.assertEqual(cleaned["account"], "***REDACTED***")
        self.assertEqual(cleaned["account_number"], "***REDACTED***")
        self.assertEqual(cleaned["nested"]["ACCESS-TOKEN"], "***REDACTED***")
        self.assertEqual(cleaned["nested"]["fine"], "ok")
        self.assertEqual(cleaned["items"][0]["cookies"], "***REDACTED***")
        self.assertEqual(cleaned["items"][0]["symbol"], "AAPL")

    def test_sanitize_exception_strips_query_tokens(self) -> None:
        msg = (
            "401 Unauthorized for url: https://invest-api.firstrade.com/v1/foo"
            "?sid=ABCDEFG&ftat=XYZTOKEN&token=secret"
        )
        cleaned = _sanitize_exception(RuntimeError(msg))
        self.assertNotIn("ABCDEFG", cleaned)
        self.assertNotIn("XYZTOKEN", cleaned)
        self.assertNotIn("secret", cleaned)
        self.assertIn("RuntimeError", cleaned)


# =====================================================================
# 2) Client login flow
# =====================================================================

class ClientLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _make_config()

    def tearDown(self) -> None:
        _uninstall_fake_sdk()

    def test_disabled_returns_not_enabled(self) -> None:
        client = FirstradeReadOnlyClient(_make_config(enabled=False))
        result = client.login()
        self.assertEqual(result.status, "not_enabled")

    def test_missing_sdk_returns_not_installed(self) -> None:
        # Make sure the module is absent.
        _uninstall_fake_sdk()
        client = FirstradeReadOnlyClient(self.config)
        result = client.login()
        self.assertEqual(result.status, "not_installed")

    def test_login_ok(self) -> None:
        _install_fake_sdk(need_code=False)
        client = FirstradeReadOnlyClient(self.config)
        result = client.login()
        self.assertEqual(result.status, "ok")
        self.assertTrue(client.is_logged_in())

    def test_login_returns_mfa_required(self) -> None:
        _install_fake_sdk(need_code=True)
        client = FirstradeReadOnlyClient(self.config)
        result = client.login()
        self.assertEqual(result.status, "mfa_required")
        self.assertFalse(client.is_logged_in())  # account_data not yet set
        # Singleton must persist the half-logged-in session.
        self.assertIsNotNone(client._sdk)
        self.assertIsNotNone(client._sdk.session)
        self.assertIsNone(client._sdk.account_data)

    def test_verify_mfa_completes_session(self) -> None:
        _install_fake_sdk(need_code=True)
        client = FirstradeReadOnlyClient(self.config)
        client.login()
        result = client.verify_mfa("123456")
        self.assertEqual(result.status, "ok")
        self.assertTrue(client.is_logged_in())

    def test_verify_mfa_without_login_returns_session_lost(self) -> None:
        _install_fake_sdk(need_code=False)
        client = FirstradeReadOnlyClient(self.config)
        # Skip login(); call verify_mfa cold.
        result = client.verify_mfa("123456")
        self.assertEqual(result.status, "session_lost")


# =====================================================================
# 3) Account / position / order / transaction shape
# =====================================================================

class ClientReadPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _make_config()
        _install_fake_sdk(
            accounts=[
                {"account": "112233445566"},
                {"account": "999888777666", "name": "Roth IRA"},
            ],
            positions=[
                {"symbol": "AAPL", "quantity": 10, "market_value": 1750.5,
                 "avg_cost": 150.0, "last_price": 175.05, "unrealized_pnl": 250.5},
            ],
            balance={"cash": 1234.56, "buying_power": 2469.12, "total_value": 5000.0},
            orders=[
                {"order_id": "ORDER-XYZ", "symbol": "AAPL",
                 "status": "open", "side": "buy", "quantity": 5, "limit_price": 170},
            ],
            transactions=[
                {"transaction_id": "TX-1", "symbol": "AAPL",
                 "type": "buy", "trade_date": "2025-12-01", "amount": -875},
            ],
        )
        self.client = FirstradeReadOnlyClient(self.config)
        self.client.login()

    def tearDown(self) -> None:
        _uninstall_fake_sdk()

    def test_list_accounts_masks_account_number(self) -> None:
        accounts = self.client.list_accounts()
        self.assertEqual(len(accounts), 2)
        for acct in accounts:
            self.assertNotIn("112233445566", acct.account_alias)
            self.assertNotIn("999888777666", acct.account_alias)
            self.assertTrue(acct.account_alias.startswith("Firstrade ****"))
            self.assertEqual(len(acct.account_hash), 16)
            self.assertEqual(len(acct.account_last4), 4)

    def test_account_payload_redacts_account_field(self) -> None:
        accounts = self.client.list_accounts()
        for acct in accounts:
            self.assertEqual(acct.raw_payload.get("account"), "***REDACTED***")

    def test_get_positions_carries_account_alias(self) -> None:
        # Ensure the account_map is populated.
        self.client.list_accounts()
        positions = self.client.get_positions()
        self.assertGreaterEqual(len(positions), 1)
        for p in positions:
            self.assertEqual(p.symbol, "AAPL")
            self.assertEqual(p.quantity, 10.0)
            self.assertTrue(p.account_alias.startswith("Firstrade ****"))
            # account number must NOT leak into the dataclass field.
            self.assertNotIn("112233445566", p.account_alias)
            self.assertNotIn("999888777666", p.account_alias)

    def test_get_balances_normalizes_floats(self) -> None:
        self.client.list_accounts()
        balances = self.client.get_balances()
        self.assertGreaterEqual(len(balances), 1)
        for b in balances:
            self.assertEqual(b.cash, 1234.56)
            self.assertEqual(b.buying_power, 2469.12)
            self.assertEqual(b.total_value, 5000.0)

    def test_get_orders_hashes_order_id(self) -> None:
        self.client.list_accounts()
        orders = self.client.get_orders()
        self.assertGreaterEqual(len(orders), 1)
        for o in orders:
            # The plaintext order id must NEVER appear in the output.
            self.assertNotEqual(o.order_id_hash, "ORDER-XYZ")
            self.assertEqual(len(o.order_id_hash), 16)

    def test_get_transactions_hashes_transaction_id(self) -> None:
        self.client.list_accounts()
        txs = self.client.get_transactions(date_range="today")
        self.assertGreaterEqual(len(txs), 1)
        for t in txs:
            self.assertNotEqual(t.transaction_id_hash, "TX-1")
            self.assertEqual(len(t.transaction_id_hash), 16)

    def test_invalid_date_range_falls_back_to_today(self) -> None:
        self.client.list_accounts()
        # Should not raise, should not surface vendor exception.
        result = self.client.get_transactions(date_range="garbage")
        self.assertIsInstance(result, list)


# =====================================================================
# 4) Module-import safety
# =====================================================================

class ImportSafetyTests(unittest.TestCase):
    def test_no_order_module_imported(self) -> None:
        """Ensure the read-only client module does NOT import
        firstrade.order at module load time."""
        # Force a re-import in a sub-namespace to inspect the import
        # graph without polluting the test suite.
        import importlib

        # Remove any cached modules for a clean reload.
        for mod_name in list(sys.modules):
            if mod_name.startswith("firstrade"):
                sys.modules.pop(mod_name, None)
        importlib.reload(
            __import__("src.brokers.firstrade.client", fromlist=["_dummy"])
        )
        # The vendor 'firstrade' package should NOT be imported as a
        # side effect of reloading our client.
        self.assertNotIn("firstrade", sys.modules)
        self.assertNotIn("firstrade.order", sys.modules)


if __name__ == "__main__":
    unittest.main()
