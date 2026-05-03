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

    Mirrors the **real** ``firstrade==0.0.38`` API:
      * ``account_numbers`` is the canonical list of account-number
        strings (NOT ``all_accounts``, which is HTTP response wrapper
        metadata in the live SDK).
      * The read methods are side-effect-driven: calling
        ``get_positions(account=X)`` populates
        ``account_data.securities_held``; calling
        ``get_account_balances(account=X)`` populates
        ``account_data.account_balances``; etc. The method's return
        value is informational (e.g. ticker list), not the details.

    The fixture accepts the same convenience kwargs as before but
    rewires them to the side-effect contract:
      * ``accounts``: list of account-number strings (default 1)
      * ``positions``: list of detail dicts; auto-keyed by ``symbol``
      * ``balance``: detail dict (or float)
      * ``orders`` / ``transactions``: lists of detail dicts
    """
    if accounts is None:
        account_numbers = ["112233445566"]
    elif isinstance(accounts, str):
        account_numbers = [accounts]
    elif isinstance(accounts, list):
        # Backward-compat: accept either ["12345"] or [{"account": "12345"}]
        account_numbers = []
        for item in accounts:
            if isinstance(item, str):
                account_numbers.append(item)
            elif isinstance(item, dict):
                num = item.get("account") or item.get("account_number") or ""
                if num:
                    account_numbers.append(str(num))
    else:
        account_numbers = ["112233445566"]
    positions = positions or []
    orders = orders or []
    transactions = transactions or []
    balance = balance or {"cash": 100.0, "buying_power": 100.0, "total_value": 200.0}

    ft_session_instance = MagicMock(name="FTSession-instance")
    ft_session_instance.login = MagicMock(return_value=need_code)
    ft_session_instance.login_two = MagicMock(return_value=True)

    # Real SDK stores no detail dicts on construction; details only
    # appear after the corresponding side-effect method is called.
    # Use a plain class so getattr() doesn't auto-spawn MagicMock attrs
    # for ``securities_held`` etc. (which would defeat our absence
    # checks in mappers).
    class _FakeAccountData:
        pass
    account_data_instance = _FakeAccountData()
    account_data_instance.account_numbers = list(account_numbers)
    # ``all_accounts`` exists in the real SDK but as response metadata —
    # we deliberately do NOT use it as the iteration anchor anymore.
    account_data_instance.all_accounts = {"statusCode": 200}
    account_data_instance.user_info = {"sid": "<redacted>"}

    # Index positions by symbol — that's the live SDK's
    # ``securities_held`` key shape.
    sec_held = {}
    for p in positions:
        symbol = (p.get("symbol") or p.get("ticker") or "").upper()
        if symbol:
            sec_held[symbol] = p

    def _populate_securities_held(account=None, **_kw):
        """Mirror SDK side-effect: write to securities_held + return ticker list."""
        account_data_instance.securities_held = dict(sec_held)
        return list(sec_held.keys())

    def _populate_balances(account=None, **_kw):
        account_data_instance.account_balances = {account: dict(balance)}
        return dict(balance)

    def _populate_orders(account=None, **_kw):
        account_data_instance.orders = list(orders)
        return list(orders)

    def _populate_history(account=None, **_kw):
        account_data_instance.account_history = list(transactions)
        return list(transactions)

    account_data_instance.get_positions = MagicMock(side_effect=_populate_securities_held)
    account_data_instance.get_account_balances = MagicMock(side_effect=_populate_balances)
    account_data_instance.get_orders = MagicMock(side_effect=_populate_orders)
    account_data_instance.get_account_history = MagicMock(side_effect=_populate_history)

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

    def test_account_payload_does_not_leak_account_number(self) -> None:
        """Per the new vendor contract, ``account_numbers`` is a list of
        bare strings — there's no per-row payload to leak from. Confirm
        the BrokerAccount's ``raw_payload`` carries no account number
        field at all, regardless of how the test fixture is shaped."""
        accounts = self.client.list_accounts()
        for acct in accounts:
            payload = acct.raw_payload or {}
            for forbidden_key in ("account", "account_number", "accountNumber"):
                value = payload.get(forbidden_key)
                # Either the key is absent, or its value has been
                # redacted — never the raw account number.
                self.assertIn(
                    value, (None, "***REDACTED***"),
                    msg=(
                        f"raw_payload[{forbidden_key!r}] leaked "
                        f"non-redacted value {value!r}"
                    ),
                )

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


class AccountIterableShapeTests(unittest.TestCase):
    """Regression tests for the ``all_accounts`` shape handling.

    ``firstrade==0.0.38`` returns a single account-number string for a
    one-account user (NOT a list). Without explicit handling, ``list()``
    would iterate the string character-by-character and produce one
    pseudo-account per digit — manifesting as "5 accounts, 15 positions"
    for a real user with 1 account and 3 positions (5 × 3).
    """

    def test_string_collapses_to_single_account(self) -> None:
        normalized = FirstradeReadOnlyClient._normalize_accounts_iterable(
            "12345678"
        )
        self.assertEqual(normalized, ["12345678"])
        self.assertEqual(FirstradeReadOnlyClient._safe_len("12345678"), 1)

    def test_int_collapses_to_single_account(self) -> None:
        normalized = FirstradeReadOnlyClient._normalize_accounts_iterable(
            12345678
        )
        self.assertEqual(normalized, [12345678])
        self.assertEqual(FirstradeReadOnlyClient._safe_len(12345678), 1)

    def test_list_passes_through(self) -> None:
        normalized = FirstradeReadOnlyClient._normalize_accounts_iterable(
            ["111", "222"]
        )
        self.assertEqual(normalized, ["111", "222"])
        self.assertEqual(
            FirstradeReadOnlyClient._safe_len(["111", "222"]), 2
        )

    def test_dict_uses_keys_as_account_numbers(self) -> None:
        # SDK variant where {account_number: {details...}} is returned.
        normalized = FirstradeReadOnlyClient._normalize_accounts_iterable(
            {"111": {"name": "A"}, "222": {"name": "B"}}
        )
        # When values are dicts, we merge an _account_key marker so the
        # extractor can pull the number; account_number ends up either
        # in the merged dict (under _account_key) or via _first_present.
        self.assertEqual(len(normalized), 2)

    def test_none_returns_empty(self) -> None:
        self.assertEqual(
            FirstradeReadOnlyClient._normalize_accounts_iterable(None), [],
        )
        self.assertEqual(FirstradeReadOnlyClient._safe_len(None), 0)

    def test_string_account_one_account_end_to_end(self) -> None:
        """The full bug scenario: one account expressed as a bare string."""
        _install_fake_sdk(accounts="12345678")  # NOT a list — a bare string
        try:
            client = FirstradeReadOnlyClient(_make_config())
            result = client.login()
            self.assertEqual(result.status, "ok")
            # account_count must be 1, not 8.
            self.assertEqual(result.account_count, 1)
            accounts = client.list_accounts()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].account_last4, "5678")
        finally:
            _uninstall_fake_sdk()


# =====================================================================
# 4) Module-import safety
# =====================================================================

class SideEffectContractTests(unittest.TestCase):
    """Regression tests pinning the ``firstrade==0.0.38`` side-effect
    contract. The vendor's ``get_positions(account=X)`` /
    ``get_account_balances(account=X)`` / ``get_orders(account=X)``
    methods are NOT pure — they don't return detail dicts; they WRITE
    onto attributes of ``FTAccountData`` and return only a thin
    summary. If our client ever forgets to read from those side-effect
    dicts, the UI silently shows dashes everywhere — exactly the bug
    that motivated this refactor.

    Each test below exercises one phase of the contract and asserts
    real fields land on the BrokerPosition / BrokerBalance / etc.
    """

    def setUp(self) -> None:
        from src.brokers.firstrade.client import FirstradeReadOnlyClient
        self._FRC = FirstradeReadOnlyClient
        # One real account, three positions — same shape as the
        # production user we debugged this regression against.
        _install_fake_sdk(
            accounts=["67704947"],
            positions=[
                {"symbol": "AAPL", "quantity": 10, "market_value": 2800.70,
                 "avg_cost": 150.0, "last_price": 280.07,
                 "day_change": 8.72, "day_change_pct": 3.21,
                 "unrealized_pnl": 1300.7},
                {"symbol": "AVGO", "quantity": 5, "market_value": 2101.35,
                 "avg_cost": 200.0, "last_price": 420.27,
                 "day_change": 2.84, "day_change_pct": 0.68,
                 "unrealized_pnl": 1101.35},
                {"symbol": "QQQM", "quantity": 50, "market_value": 13870.50,
                 "avg_cost": 200.0, "last_price": 277.41,
                 "day_change": 2.51, "day_change_pct": 0.91,
                 "unrealized_pnl": 3870.5},
            ],
            balance={
                "cash": 5000.0,
                "buying_power": 10000.0,
                "total_value": 23772.55,
                "currency": "USD",
            },
            orders=[
                {"order_id": "ORD-1", "symbol": "AAPL",
                 "status": "filled", "side": "buy", "quantity": 10,
                 "limit_price": 275.0, "filled": 10},
            ],
        )
        self.client = self._FRC(_make_config())
        self.client.login()

    def tearDown(self) -> None:
        _uninstall_fake_sdk()

    def test_get_positions_invokes_side_effect_and_reads_details(self) -> None:
        """``get_positions(account=X)`` MUST be called as a kwarg AND
        the client MUST then read ``account_data.securities_held`` for
        the actual detail dicts."""
        sdk_account_data = self.client._sdk.account_data

        # Sanity: securities_held doesn't exist yet (the real SDK
        # creates it lazily on first ``get_positions`` call).
        self.assertFalse(hasattr(sdk_account_data, "securities_held"))

        positions = self.client.get_positions()

        # The mocked SDK populated securities_held during the call.
        self.assertTrue(hasattr(sdk_account_data, "securities_held"))
        # ``get_positions`` was invoked with account= kwarg.
        sdk_account_data.get_positions.assert_called_with(account="67704947")
        # Three real position objects with all fields populated.
        self.assertEqual(len(positions), 3)
        symbols = {p.symbol for p in positions}
        self.assertEqual(symbols, {"AAPL", "AVGO", "QQQM"})
        # Pin the AAPL row — every field non-None proves the
        # side-effect-aware read is wired correctly.
        aapl = next(p for p in positions if p.symbol == "AAPL")
        self.assertEqual(aapl.quantity, 10.0)
        self.assertEqual(aapl.market_value, 2800.70)
        self.assertEqual(aapl.last_price, 280.07)
        self.assertEqual(aapl.avg_cost, 150.0)
        self.assertEqual(aapl.day_change, 8.72)
        self.assertEqual(aapl.day_change_pct, 3.21)
        self.assertEqual(aapl.unrealized_pnl, 1300.7)

    def test_get_balances_invokes_side_effect_and_reads_dict(self) -> None:
        sdk_account_data = self.client._sdk.account_data
        self.assertFalse(hasattr(sdk_account_data, "account_balances")
                         and isinstance(getattr(sdk_account_data, "account_balances", None), dict)
                         and "67704947" in getattr(sdk_account_data, "account_balances", {}))

        balances = self.client.get_balances()
        sdk_account_data.get_account_balances.assert_called_with(account="67704947")
        self.assertEqual(len(balances), 1)
        b = balances[0]
        self.assertEqual(b.cash, 5000.0)
        self.assertEqual(b.buying_power, 10000.0)
        self.assertEqual(b.total_value, 23772.55)

    def test_account_numbers_is_iteration_anchor_not_all_accounts(self) -> None:
        """Critical regression: previously the client iterated
        ``all_accounts`` (response wrapper, 5 unrelated keys) and we
        ended up with "5 sub-accounts" for a 1-account user. The new
        code MUST anchor on ``account_numbers`` (1 entry → 1 account).
        """
        accounts = self.client.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].account_last4, "4947")

    def test_get_positions_with_no_held_attr_returns_empty(self) -> None:
        """If the SDK returns the ticker list but never populates
        ``securities_held`` (e.g. broken SDK version), positions should
        be empty rather than blowing up."""
        # Override the populating side-effect to return a ticker list
        # WITHOUT writing to securities_held.
        sdk_ad = self.client._sdk.account_data
        if hasattr(sdk_ad, "securities_held"):
            del sdk_ad.securities_held
        sdk_ad.get_positions.side_effect = lambda account=None, **_: ["AAPL"]
        positions = self.client.get_positions()
        # Symbol shows up but details are all None — defensive degrade.
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "AAPL")
        self.assertIsNone(positions[0].quantity)


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
